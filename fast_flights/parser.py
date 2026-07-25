# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

import json
from collections.abc import Sequence
from typing import Any, Literal, overload

from selectolax.lexbor import LexborHTMLParser

from .exceptions import FlightsNotFound, FlightsResponseError
from .model import (
    Airline,
    Airport,
    Alliance,
    CarbonEmission,
    Flights,
    JsMetadata,
    SimpleDatetime,
    SingleFlight,
)


class ResultList(list[Flights]):
    """Searched flights list, with metadata attached."""

    metadata: JsMetadata


def parse(html: str) -> ResultList:
    parser = LexborHTMLParser(html)

    # find js
    script = parser.css_first(r"script.ds\:1")
    if script is None:
        raise FlightsResponseError("response did not contain Google Flights data")

    script_text = script.text()
    if not script_text:
        raise FlightsResponseError("Google Flights data script was empty")
    return parse_js(script_text)


def _sequence(value: Any) -> Sequence[Any] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return None


def _item(value: Any, index: int) -> Any:
    sequence = _sequence(value)
    if sequence is None or index >= len(sequence):
        return None
    return sequence[index]


def _metadata(payload: Sequence[Any]) -> JsMetadata:
    metadata = _item(_item(payload, 7), 1)
    alliances_data = _sequence(_item(metadata, 0)) or ()
    airlines_data = _sequence(_item(metadata, 1)) or ()
    alliances: list[Alliance] = []
    airlines: list[Airline] = []

    for entry in alliances_data:
        code, name = _item(entry, 0), _item(entry, 1)
        if isinstance(code, str) and isinstance(name, str):
            alliances.append(Alliance(code=code, name=name))

    for entry in airlines_data:
        code, name = _item(entry, 0), _item(entry, 1)
        if isinstance(code, str) and isinstance(name, str):
            airlines.append(Airline(code=code, name=name))

    return JsMetadata(alliances=alliances, airlines=airlines)


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FlightsResponseError(f"invalid {field} in itinerary")
    return value


@overload
def _integer_tuple(
    value: Any,
    length: Literal[2],
    field: str,
) -> tuple[int, int]: ...


@overload
def _integer_tuple(
    value: Any,
    length: Literal[3],
    field: str,
) -> tuple[int, int, int]: ...


def _integer_tuple(
    value: Any,
    length: Literal[2, 3],
    field: str,
) -> tuple[int, ...]:
    sequence = _sequence(value)
    if sequence is None or len(sequence) != length:
        raise FlightsResponseError(f"invalid {field} in itinerary")
    return tuple(_integer(part, field) for part in sequence)


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise FlightsResponseError(f"invalid {field} in itinerary")
    return value


def _parse_segment(segment: Any) -> SingleFlight:
    if _sequence(segment) is None:
        raise FlightsResponseError("invalid flight segment")

    departure = SimpleDatetime(
        date=_integer_tuple(_item(segment, 20), 3, "departure date"),
        time=_integer_tuple(_item(segment, 8), 2, "departure time"),
    )
    arrival = SimpleDatetime(
        date=_integer_tuple(_item(segment, 21), 3, "arrival date"),
        time=_integer_tuple(_item(segment, 10), 2, "arrival time"),
    )
    plane_type = _item(segment, 17)
    if plane_type is not None and not isinstance(plane_type, str):
        plane_type = None

    return SingleFlight(
        from_airport=Airport(
            code=_required_string(_item(segment, 3), "departure airport code"),
            name=_required_string(_item(segment, 4), "departure airport name"),
        ),
        to_airport=Airport(
            code=_required_string(_item(segment, 6), "arrival airport code"),
            name=_required_string(_item(segment, 5), "arrival airport name"),
        ),
        departure=departure,
        arrival=arrival,
        duration=_integer(_item(segment, 11), "duration"),
        plane_type=plane_type,
    )


def _optional_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _parse_itinerary(row: Any) -> Flights | None:
    if _sequence(row) is None:
        raise FlightsResponseError("invalid itinerary row")

    price_groups = _sequence(_item(row, 1))
    if price_groups is None:
        raise FlightsResponseError("invalid price groups in itinerary")
    if not price_groups:
        return None

    price_data = _sequence(_item(price_groups, 0))
    if price_data is None:
        raise FlightsResponseError("invalid price data in itinerary")
    if not price_data:
        return None
    if len(price_data) < 2:
        raise FlightsResponseError("incomplete price data in itinerary")

    price = _item(price_data, 1)
    if price is None:
        # Google may return otherwise valid itinerary rows without a price,
        # particularly when no fare satisfies a filter. Such rows are not
        # actionable search results.
        return None
    price = _integer(price, "price")

    flight = _item(row, 0)
    if _sequence(flight) is None:
        raise FlightsResponseError("invalid flight data in itinerary")

    typ = _required_string(_item(flight, 0), "flight type")
    raw_airlines = _sequence(_item(flight, 1))
    if raw_airlines is None:
        raise FlightsResponseError("invalid airlines in itinerary")
    airlines = [airline for airline in raw_airlines if isinstance(airline, str)]

    raw_segments = _sequence(_item(flight, 2))
    if not raw_segments:
        raise FlightsResponseError("itinerary did not contain flight segments")
    segments = [_parse_segment(segment) for segment in raw_segments]

    extras = _item(flight, 22)
    return Flights(
        type=typ,
        price=price,
        airlines=airlines,
        flights=segments,
        carbon=CarbonEmission(
            typical_on_route=_optional_integer(_item(extras, 8)),
            emission=_optional_integer(_item(extras, 7)),
        ),
    )


# Data discovery by @kftang, huge shout out!
def parse_js(js: str) -> ResultList:
    if "errorHasStatus: true" in js:
        raise FlightsNotFound("no flights found; received error")
    if "data:" not in js:
        raise FlightsResponseError("Google Flights data marker was missing")

    raw_data = js.split("data:", 1)[1].lstrip()
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw_data)
    except (json.JSONDecodeError, TypeError) as exc:
        raise FlightsResponseError("Google Flights data was not valid JSON") from exc

    payload_sequence = _sequence(payload)
    if payload_sequence is None:
        raise FlightsResponseError("Google Flights payload was not a list")
    if len(payload_sequence) <= 3:
        raise FlightsResponseError("Google Flights payload was incomplete")

    flights = ResultList()
    flights.metadata = _metadata(payload_sequence)

    result_group = _item(payload_sequence, 3)
    if result_group is None:
        return flights
    if _sequence(result_group) is None:
        raise FlightsResponseError("Google Flights result group was malformed")

    rows = _item(result_group, 0)
    if rows is None:
        return flights
    rows_sequence = _sequence(rows)
    if rows_sequence is None:
        raise FlightsResponseError("Google Flights itinerary rows were malformed")

    malformed_rows = 0
    recognized_rows = 0
    for row in rows_sequence:
        try:
            parsed = _parse_itinerary(row)
        except FlightsResponseError:
            malformed_rows += 1
            continue
        recognized_rows += 1
        if parsed is not None:
            flights.append(parsed)

    if rows_sequence and not recognized_rows and malformed_rows:
        raise FlightsResponseError("all Google Flights itinerary rows were malformed")

    return flights
