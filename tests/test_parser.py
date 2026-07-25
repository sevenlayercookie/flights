"""Unit tests for Google Flights response parsing."""

import json
import unittest

from fast_flights.parser import parse_js


def _single_flight() -> list:
    segment = [None] * 22
    segment[3] = "MSP"
    segment[4] = "Minneapolis-Saint Paul International Airport"
    segment[5] = "Denver International Airport"
    segment[6] = "DEN"
    segment[8] = [7, 0]
    segment[10] = [8, 15]
    segment[11] = 135
    segment[17] = "Airbus A320"
    segment[20] = [2026, 7, 26]
    segment[21] = [2026, 7, 26]

    extras = [None] * 9
    extras[7] = 100_000
    extras[8] = 120_000

    flight = [None] * 23
    flight[0] = "A"
    flight[1] = ["Delta"]
    flight[2] = [segment]
    flight[22] = extras
    return flight


def _script(entries: list) -> str:
    payload = [None] * 8
    payload[3] = [entries]
    payload[7] = [None, [[], []]]
    return f"data:{json.dumps(payload)},ignored"


class ParserTests(unittest.TestCase):
    def test_unpriced_rows_produce_an_empty_result(self) -> None:
        unpriced = [_single_flight(), [[], "booking-token"]]

        results = parse_js(_script([unpriced]))

        self.assertEqual(results, [])
        self.assertEqual(results.metadata.airlines, [])
        self.assertEqual(results.metadata.alliances, [])

    def test_unpriced_rows_are_skipped_without_hiding_priced_rows(self) -> None:
        unpriced = [_single_flight(), [[], "unpriced-token"]]
        priced = [_single_flight(), [[None, 250], "priced-token"]]

        results = parse_js(_script([unpriced, priced]))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].price, 250)
        self.assertEqual(results[0].flights[0].from_airport.code, "MSP")
        self.assertEqual(results[0].flights[0].to_airport.code, "DEN")


if __name__ == "__main__":
    unittest.main()
