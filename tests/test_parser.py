"""Unit tests for Google Flights response parsing."""

import json
import unittest
from typing import Any

from fast_flights import FlightsNotFound, FlightsResponseError
from fast_flights.parser import parse, parse_js


def _single_flight() -> list[Any]:
    segment: list[Any] = [None] * 22
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

    extras: list[Any] = [None] * 9
    extras[7] = 100_000
    extras[8] = 120_000

    flight: list[Any] = [None] * 23
    flight[0] = "A"
    flight[1] = ["Delta"]
    flight[2] = [segment]
    flight[22] = extras
    return flight


def _payload(result_group: Any) -> list[Any]:
    payload: list[Any] = [None] * 8
    payload[3] = result_group
    payload[7] = [None, [[["*A", "Star Alliance"]], [["DL", "Delta"]]]]
    return payload


def _script(result_group: Any) -> str:
    return f"data:{json.dumps(_payload(result_group))},ignored"


def _priced_row(price: int = 250) -> list[Any]:
    return [_single_flight(), [[None, price], "priced-token"]]


class ParserTests(unittest.TestCase):
    def test_top_level_null_result_group_produces_empty_result(self) -> None:
        results = parse_js(_script(None))

        self.assertEqual(results, [])
        self.assertEqual(results.metadata.airlines[0].code, "DL")
        self.assertEqual(results.metadata.alliances[0].code, "*A")

    def test_nested_null_or_empty_result_group_produces_empty_result(self) -> None:
        for result_group in ([None], []):
            with self.subTest(result_group=result_group):
                results = parse_js(_script(result_group))

                self.assertEqual(results, [])
                self.assertEqual(results.metadata.airlines[0].name, "Delta")

    def test_unpriced_rows_produce_empty_result(self) -> None:
        unpriced = [_single_flight(), [[], "booking-token"]]

        results = parse_js(_script([[unpriced]]))

        self.assertEqual(results, [])
        self.assertEqual(results.metadata.airlines[0].code, "DL")

    def test_unpriced_rows_are_skipped_without_hiding_priced_rows(self) -> None:
        unpriced = [_single_flight(), [[], "unpriced-token"]]

        results = parse_js(_script([[unpriced, _priced_row()]]))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].price, 250)
        self.assertEqual(results[0].flights[0].from_airport.code, "MSP")
        self.assertEqual(results[0].flights[0].to_airport.code, "DEN")

    def test_malformed_row_is_skipped_when_other_rows_are_usable(self) -> None:
        results = parse_js(_script([[["malformed"], _priced_row(275)]]))

        self.assertEqual([flight.price for flight in results], [275])

    def test_all_malformed_rows_raise_response_error(self) -> None:
        with self.assertRaisesRegex(
            FlightsResponseError,
            "all Google Flights itinerary rows were malformed",
        ):
            parse_js(_script([[["malformed"], {"unexpected": "row"}]]))

    def test_missing_metadata_and_carbon_values_are_optional(self) -> None:
        payload = _payload([[_priced_row()]])
        payload[7] = None
        flight = payload[3][0][0][0]
        flight[22] = None

        results = parse_js(f"data:{json.dumps(payload)},ignored")

        self.assertEqual(results.metadata.airlines, [])
        self.assertEqual(results.metadata.alliances, [])
        self.assertIsNone(results[0].carbon.emission)
        self.assertIsNone(results[0].carbon.typical_on_route)

    def test_missing_or_invalid_data_envelope_raises_response_error(self) -> None:
        cases = (
            "no marker",
            "data:not-json",
            'data:{"not":"a list"}',
            "data:[]",
            "data:[null,null,null,42]",
            'data:[null,null,null,["not rows"]]',
        )
        for script in cases:
            with self.subTest(script=script):
                with self.assertRaises(FlightsResponseError):
                    parse_js(script)

    def test_missing_data_script_raises_response_error(self) -> None:
        with self.assertRaisesRegex(
            FlightsResponseError,
            "did not contain Google Flights data",
        ):
            parse("<html><body>No flight data</body></html>")

    def test_google_error_response_raises_flights_not_found(self) -> None:
        with self.assertRaises(FlightsNotFound):
            parse_js("errorHasStatus: true")

    def test_json_shaped_payloads_never_leak_builtin_shape_errors(self) -> None:
        values: tuple[Any, ...] = (
            None,
            True,
            1,
            "value",
            {},
            [],
            [None],
            [[], {}],
            {"nested": [None, "value"]},
        )
        for root in values:
            for result_group in values:
                payload = root
                if isinstance(root, list):
                    payload = [*root, None, None, result_group]
                with self.subTest(root=root, result_group=result_group):
                    try:
                        parse_js(f"data:{json.dumps(payload)}")
                    except (FlightsNotFound, FlightsResponseError):
                        pass


if __name__ == "__main__":
    unittest.main()
