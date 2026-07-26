"""Small schema-free protobuf wire inspector and mutator for ``tfs`` research."""

from __future__ import annotations

import argparse
import base64
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class WireField:
    number: int
    wire_type: int
    value: int | bytes


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid or truncated varint")


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varints must be nonnegative")
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def parse_wire(data: bytes) -> list[WireField]:
    fields: list[WireField] = []
    offset = 0
    while offset < len(data):
        tag, offset = decode_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("field number zero is invalid")
        if wire_type == 0:
            value, offset = decode_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("truncated fixed64 field")
            value, offset = data[offset:end], end
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated length-delimited field")
            value, offset = data[offset:end], end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("truncated fixed32 field")
            value, offset = data[offset:end], end
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        fields.append(WireField(number, wire_type, value))
    return fields


def serialize_field(field: WireField) -> bytes:
    encoded = bytearray(encode_varint((field.number << 3) | field.wire_type))
    if field.wire_type == 0:
        assert isinstance(field.value, int)
        encoded.extend(encode_varint(field.value))
    elif field.wire_type == 2:
        assert isinstance(field.value, bytes)
        encoded.extend(encode_varint(len(field.value)))
        encoded.extend(field.value)
    elif field.wire_type in (1, 5):
        assert isinstance(field.value, bytes)
        expected = 8 if field.wire_type == 1 else 4
        if len(field.value) != expected:
            raise ValueError(f"wire type {field.wire_type} needs {expected} bytes")
        encoded.extend(field.value)
    else:
        raise ValueError(f"unsupported wire type {field.wire_type}")
    return bytes(encoded)


def serialize_wire(fields: list[WireField]) -> bytes:
    return b"".join(serialize_field(field) for field in fields)


def decode_tfs(value: str) -> bytes:
    normalized = value.replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    return base64.b64decode(normalized)


def encode_tfs(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def append_varint(data: bytes, number: int, value: int) -> bytes:
    return data + serialize_field(WireField(number, 0, value))


def append_nested_varint(
    data: bytes, parent_number: int, number: int, value: int
) -> bytes:
    fields = parse_wire(data)
    changed = False
    output: list[WireField] = []
    for field in fields:
        if field.number == parent_number and field.wire_type == 2:
            assert isinstance(field.value, bytes)
            output.append(
                WireField(
                    field.number,
                    field.wire_type,
                    append_varint(field.value, number, value),
                )
            )
            changed = True
        else:
            output.append(field)
    if not changed:
        raise ValueError(f"parent field {parent_number} not found")
    return serialize_wire(output)


def _render(fields: list[WireField], depth: int) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for field in fields:
        item: dict[str, Any] = {
            "number": field.number,
            "wire_type": field.wire_type,
        }
        if isinstance(field.value, int):
            item["value"] = field.value
        else:
            item["hex"] = field.value.hex()
            try:
                text = field.value.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text and text.isprintable():
                item["text"] = text
            if depth:
                try:
                    nested = parse_wire(field.value)
                except ValueError:
                    nested = []
                if nested:
                    item["nested"] = _render(nested, depth - 1)
        rendered.append(item)
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tfs")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--append-info-varint", nargs=2, type=int)
    parser.add_argument("--append-leg-varint", nargs=2, type=int)
    args = parser.parse_args()

    data = decode_tfs(args.tfs)
    if args.append_info_varint:
        data = append_varint(data, *args.append_info_varint)
    if args.append_leg_varint:
        data = append_nested_varint(data, 3, *args.append_leg_varint)

    result = {
        "bytes": len(data),
        "fields": _render(parse_wire(data), args.depth),
        "tfs": encode_tfs(data),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
