"""Unit tests for Google Flights query serialization."""

import unittest
from typing import Any

from fast_flights import FlightQuery, Passengers, create_query
from fast_flights.pb.flights_pb2 import Emissions, Info


class QueryingTests(unittest.TestCase):
    def test_default_query_does_not_add_optional_filters(self) -> None:
        query = create_query(
            flights=[
                FlightQuery(
                    date="2099-01-02",
                    from_airport="MSP",
                    to_airport="SLC",
                )
            ]
        )

        info = Info.FromString(query.to_bytes())
        flight = info.data[0]

        self.assertFalse(info.HasField("max_price"))
        self.assertFalse(info.HasField("baggage"))
        self.assertFalse(info.HasField("hide_separate_and_self_transfer"))
        self.assertFalse(info.HasField("exclude_basic_economy"))
        self.assertFalse(flight.HasField("earliest_departure_hour"))
        self.assertFalse(flight.HasField("latest_departure_hour"))
        self.assertFalse(flight.HasField("earliest_arrival_hour"))
        self.assertFalse(flight.HasField("latest_arrival_hour"))
        self.assertFalse(flight.HasField("max_duration_minutes"))
        self.assertFalse(flight.HasField("min_layover_minutes"))
        self.assertFalse(flight.HasField("max_layover_minutes"))
        self.assertEqual(list(flight.connecting_airports), [])
        self.assertEqual(list(flight.emissions), [])

    def test_serializes_per_leg_filters(self) -> None:
        query = create_query(
            flights=[
                FlightQuery(
                    date="2099-01-02",
                    from_airport="MSP",
                    to_airport="SLC",
                    earliest_departure_hour=7,
                    latest_departure_hour=18,
                    earliest_arrival_hour=10,
                    latest_arrival_hour=23,
                    max_duration_minutes=720,
                    connecting_airports=["DEN", "ORD"],
                    min_layover_minutes=60,
                    max_layover_minutes=240,
                    less_emissions_only=True,
                )
            ]
        )

        flight = Info.FromString(query.to_bytes()).data[0]

        self.assertEqual(flight.earliest_departure_hour, 7)
        self.assertEqual(flight.latest_departure_hour, 18)
        self.assertEqual(flight.earliest_arrival_hour, 10)
        self.assertEqual(flight.latest_arrival_hour, 23)
        self.assertEqual(flight.max_duration_minutes, 720)
        self.assertEqual(list(flight.connecting_airports), ["DEN", "ORD"])
        self.assertEqual(flight.min_layover_minutes, 60)
        self.assertEqual(flight.max_layover_minutes, 240)
        self.assertEqual(list(flight.emissions), [Emissions.LESS_EMISSIONS])

    def test_serializes_whole_search_filters(self) -> None:
        query = create_query(
            flights=[
                FlightQuery(
                    date="2099-01-02",
                    from_airport="MSP",
                    to_airport="SLC",
                )
            ],
            currency="USD",
            max_price=500,
            carry_on_bags=1,
            checked_bags=2,
            hide_separate_and_self_transfer=True,
            exclude_basic_economy=True,
        )

        info = Info.FromString(query.to_bytes())

        self.assertEqual(info.max_price, 500)
        self.assertEqual(info.baggage.carry_on_bags, 1)
        self.assertEqual(info.baggage.checked_bags, 2)
        self.assertTrue(info.hide_separate_and_self_transfer)
        self.assertTrue(info.exclude_basic_economy)

    def test_rejects_invalid_per_leg_filter_values(self) -> None:
        invalid_arguments: tuple[dict[str, Any], ...] = (
            {"date": "not-a-date"},
            {"from_airport": ""},
            {"to_airport": "MSP"},
            {"airlines": "DL"},
            {"earliest_departure_hour": -1},
            {"latest_departure_hour": 24},
            {"earliest_arrival_hour": 18, "latest_arrival_hour": 6},
            {"max_duration_minutes": -1},
            {"min_layover_minutes": 90, "max_layover_minutes": 30},
            {"connecting_airports": [""]},
            {"less_emissions_only": 1},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                parameters: dict[str, Any] = {
                    "date": "2099-01-02",
                    "from_airport": "MSP",
                    "to_airport": "SLC",
                }
                parameters.update(arguments)
                with self.assertRaises((TypeError, ValueError)):
                    FlightQuery(**parameters)

    def test_rejects_invalid_search_wide_filter_values(self) -> None:
        flight = FlightQuery(
            date="2099-01-02",
            from_airport="MSP",
            to_airport="SLC",
        )
        invalid_arguments: tuple[dict[str, Any], ...] = (
            {"max_price": -1},
            {"max_price": 2_147_483_648},
            {"carry_on_bags": -1},
            {"carry_on_bags": None},
            {"checked_bags": 1.5},
            {"hide_separate_and_self_transfer": 1},
            {"exclude_basic_economy": "yes"},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises((TypeError, ValueError)):
                    create_query(flights=[flight], **arguments)

    def test_rejects_invalid_passenger_counts_and_empty_searches(self) -> None:
        invalid_passengers: tuple[dict[str, Any], ...] = (
            {},
            {"adults": -1},
            {"adults": 10},
            {"adults": 1, "infants_on_lap": 2},
            {"adults": 1.5},
        )
        for arguments in invalid_passengers:
            with self.subTest(arguments=arguments):
                with self.assertRaises((TypeError, ValueError)):
                    Passengers(**arguments)

        with self.assertRaisesRegex(ValueError, "at least one flight query"):
            create_query(flights=[])


if __name__ == "__main__":
    unittest.main()
