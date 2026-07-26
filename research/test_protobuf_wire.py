"""Tests for the schema-free research wire helpers."""

import unittest

from research.protobuf_wire import (
    WireField,
    append_nested_varint,
    decode_tfs,
    encode_tfs,
    parse_wire,
    serialize_wire,
)


class ProtobufWireTests(unittest.TestCase):
    def test_round_trip_preserves_unknown_fields(self) -> None:
        fields = [
            WireField(1, 0, 28),
            WireField(3, 2, b"\x12\x03MSP"),
            WireField(16, 2, b"\x08\x01"),
        ]
        encoded = serialize_wire(fields)

        self.assertEqual(serialize_wire(parse_wire(encoded)), encoded)

    def test_tfs_uses_urlsafe_unpadded_base64(self) -> None:
        raw = b"\xff\xff\xff"

        self.assertEqual(encode_tfs(raw), "____")
        self.assertEqual(decode_tfs("____"), raw)

    def test_nested_mutation_preserves_siblings(self) -> None:
        info = serialize_wire(
            [
                WireField(1, 0, 28),
                WireField(3, 2, serialize_wire([WireField(2, 2, b"2026-08-09")])),
                WireField(19, 0, 2),
            ]
        )

        mutated = append_nested_varint(info, 3, 5, 0)
        outer = parse_wire(mutated)
        leg = parse_wire(outer[1].value)  # type: ignore[arg-type]

        self.assertEqual(outer[0], WireField(1, 0, 28))
        self.assertEqual(outer[2], WireField(19, 0, 2))
        self.assertEqual(leg[-1], WireField(5, 0, 0))


if __name__ == "__main__":
    unittest.main()
