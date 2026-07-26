"""Probe unknown Google Flights protobuf fields with read-only search requests.

The output contains only aggregate result signatures. Raw HTML, cookies, headers,
and request tokens are deliberately neither printed nor written to disk.
"""

# pyright: reportMissingImports=false

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from primp import Client
from selectolax.lexbor import LexborHTMLParser

from research.protobuf_wire import (
    WireField,
    decode_tfs,
    encode_tfs,
    parse_wire,
    serialize_wire,
)


BASELINE_TFS = (
    "CBwQAhoeEgoyMDI2LTA4LTA5agcIARIDTVNQcgcIARIDREVO"
    "QAFIAXABggELCP___________wGYAQI"
)
SEARCH_URL = "https://www.google.com/travel/flights"


@dataclass(frozen=True)
class Mutation:
    name: str
    apply: Callable[[bytes], bytes]


def _replace_varint(data: bytes, number: int, value: int) -> bytes:
    fields = [
        field
        for field in parse_wire(data)
        if not (field.number == number and field.wire_type == 0)
    ]
    fields.append(WireField(number, 0, value))
    return serialize_wire(fields)


def _replace_bytes(data: bytes, number: int, value: bytes) -> bytes:
    fields = [field for field in parse_wire(data) if field.number != number]
    fields.append(WireField(number, 2, value))
    return serialize_wire(fields)


def _replace_nested_varint(
    data: bytes, parent_number: int, number: int, value: int
) -> bytes:
    output: list[WireField] = []
    found = False
    for field in parse_wire(data):
        if field.number == parent_number and field.wire_type == 2:
            assert isinstance(field.value, bytes)
            output.append(
                WireField(
                    parent_number,
                    2,
                    _replace_varint(field.value, number, value),
                )
            )
            found = True
        else:
            output.append(field)
    if not found:
        raise ValueError(f"parent field {parent_number} not found")
    return serialize_wire(output)


def _replace_nested_bytes(
    data: bytes, parent_number: int, number: int, value: bytes
) -> bytes:
    output: list[WireField] = []
    found = False
    for field in parse_wire(data):
        if field.number == parent_number and field.wire_type == 2:
            assert isinstance(field.value, bytes)
            output.append(
                WireField(
                    parent_number,
                    2,
                    _replace_bytes(field.value, number, value),
                )
            )
            found = True
        else:
            output.append(field)
    if not found:
        raise ValueError(f"parent field {parent_number} not found")
    return serialize_wire(output)


def _replace_airport_type(
    data: bytes, airport_field: int, value: int
) -> bytes:
    output: list[WireField] = []
    for info_field in parse_wire(data):
        if info_field.number != 3 or info_field.wire_type != 2:
            output.append(info_field)
            continue
        assert isinstance(info_field.value, bytes)
        leg_output: list[WireField] = []
        for leg_field in parse_wire(info_field.value):
            if leg_field.number != airport_field or leg_field.wire_type != 2:
                leg_output.append(leg_field)
                continue
            assert isinstance(leg_field.value, bytes)
            leg_output.append(
                WireField(
                    airport_field,
                    2,
                    _replace_varint(leg_field.value, 1, value),
                )
            )
        output.append(WireField(3, 2, serialize_wire(leg_output)))
    return serialize_wire(output)


def _replace_info16_value(data: bytes, value: int) -> bytes:
    output: list[WireField] = []
    for field in parse_wire(data):
        if field.number == 16 and field.wire_type == 2:
            assert isinstance(field.value, bytes)
            output.append(WireField(16, 2, _replace_varint(field.value, 1, value)))
        else:
            output.append(field)
    return serialize_wire(output)


def mutations() -> list[Mutation]:
    cases: list[Mutation] = []
    for field, baseline, values in (
        (1, 28, (0, 1, 27, 29)),
        (2, 2, (0, 1, 3)),
        (14, 1, (0, 2, 3)),
    ):
        for value in values:
            cases.append(
                Mutation(
                    f"info.{field}={value} (baseline {baseline})",
                    lambda data, f=field, v=value: _replace_varint(data, f, v),
                )
            )

    for value in (0, 1, 2, (1 << 64) - 1):
        cases.append(
            Mutation(
                f"info.16.1={value}",
                lambda data, v=value: _replace_info16_value(data, v),
            )
        )

    for airport_field, label in ((13, "from"), (14, "to")):
        for value in (0, 2, 3, 4):
            cases.append(
                Mutation(
                    f"leg.{label}_airport.1={value}",
                    lambda data, f=airport_field, v=value: _replace_airport_type(
                        data, f, v
                    ),
                )
            )

    for field in (1, 3, 4, 7, 16, 20):
        cases.append(
            Mutation(
                f"leg.unknown_{field}=1",
                lambda data, f=field: _replace_nested_varint(data, 3, f, 1),
            )
        )
        cases.append(
            Mutation(
                f"leg.unknown_{field}=message(1=1)",
                lambda data, f=field: _replace_nested_bytes(
                    data, 3, f, b"\x08\x01"
                ),
            )
        )

    for field in (4, 5, 6, 7, 10, 11, 15, 18, 20, 21, 22, 23, 24, 26):
        cases.append(
            Mutation(
                f"info.unknown_{field}=1",
                lambda data, f=field: _replace_varint(data, f, 1),
            )
        )
        cases.append(
            Mutation(
                f"info.unknown_{field}=message(1=1)",
                lambda data, f=field: _replace_bytes(data, f, b"\x08\x01"),
            )
        )
    return cases


def _signature(client: Client, data: bytes) -> dict[str, Any]:
    response = client.get(
        SEARCH_URL,
        params={"tfs": encode_tfs(data), "hl": "en", "curr": "USD"},
    )
    html = response.text
    result: dict[str, Any] = {
        "http": response.status_code,
        "invalid_argument": "INVALID_ARGUMENT" in html,
    }
    try:
        script = LexborHTMLParser(html).css_first(r"script.ds\:1")
        if script is None:
            raise ValueError("missing data script")
        _, marker, raw_data = script.text().partition("data:")
        if not marker:
            raise ValueError("missing data marker")
        payload, _ = json.JSONDecoder().raw_decode(raw_data)
        result_group = payload[3] if isinstance(payload, list) and len(payload) > 3 else None
        rows = (
            result_group[0]
            if isinstance(result_group, list) and result_group
            else []
        ) or []
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        result["parse"] = f"{type(error).__name__}: {error}"
        return result

    result["rows"] = len(rows) if isinstance(rows, list) else None
    flights: list[dict[str, Any]] = []
    for row in rows:
        try:
            price = row[1][0][1]
            if not isinstance(price, int):
                continue
        except (IndexError, TypeError):
            continue

        summary: dict[str, Any] = {"price": price}
        try:
            flight = row[0]
            segments = flight[2]
            summary.update(
                {
                    "airlines": [
                        airline for airline in flight[1] if isinstance(airline, str)
                    ],
                    "legs": len(segments),
                    "minutes": sum(
                        segment[11]
                        for segment in segments
                        if isinstance(segment, list)
                        and len(segment) > 11
                        and isinstance(segment[11], int)
                    ),
                }
            )
        except (IndexError, TypeError):
            pass
        flights.append(summary)

    result["parse"] = "ok"
    result["count"] = len(flights)
    result["lowest_price"] = min(
        (flight["price"] for flight in flights), default=None
    )
    result["sample"] = flights[:3]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    baseline = decode_tfs(BASELINE_TFS)
    client = Client(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        cookie_store=True,
    )
    output: list[dict[str, Any]] = [
        {"mutation": "baseline", **_signature(client, baseline)}
    ]
    cases = mutations()
    if args.limit is not None:
        cases = cases[: args.limit]
    for mutation in cases:
        time.sleep(args.delay)
        output.append(
            {
                "mutation": mutation.name,
                **_signature(client, mutation.apply(baseline)),
            }
        )
    time.sleep(args.delay)
    output.append({"mutation": "baseline-repeat", **_signature(client, baseline)})
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
