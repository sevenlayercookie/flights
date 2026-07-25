import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fast_flights import FlightsResponseError
from primp import ConnectError

from mcp_server import server


class ServerTests(unittest.TestCase):
    def test_safe_placeholder_configuration(self) -> None:
        self.assertEqual(server.OIDC_ISSUER, "https://issuer.example.com")
        self.assertEqual(server.OAUTH_RESOURCE, "http://localhost:8000/mcp")
        self.assertEqual(
            server.PROTECTED_RESOURCE_URL,
            "http://localhost:8000/.well-known/oauth-protected-resource/mcp",
        )

    def test_search_call_detection(self) -> None:
        self.assertTrue(
            server._is_search_tool_call(
                b'{"jsonrpc":"2.0","method":"tools/call",'
                b'"params":{"name":"search_flights"}}'
            )
        )
        self.assertFalse(
            server._is_search_tool_call(
                b'{"jsonrpc":"2.0","method":"tools/list","params":{}}'
            )
        )
        self.assertFalse(server._is_search_tool_call(b"not json"))

    def test_filter_validation(self) -> None:
        self.assertEqual(
            server._airline_filters(
                ["dl", "B6", "star alliance", "STAR-ALLIANCE", "oneworld"]
            ),
            ["DL", "B6", "STAR_ALLIANCE", "ONEWORLD"],
        )
        self.assertEqual(
            server._airport_filters(["den", "ORD", "DEN"], "connections"),
            ["DEN", "ORD"],
        )
        self.assertEqual(server._hour(23, "hour"), 23)

        with self.assertRaisesRegex(ValueError, "two-character IATA"):
            server._airline_filters(["Delta"])
        with self.assertRaisesRegex(ValueError, "between 0 and 23"):
            server._hour(24, "hour")
        with self.assertRaisesRegex(ValueError, "cannot be greater"):
            server._ordered_range(5, 4, "minimum", "maximum")

    def test_extended_filters_reach_fast_flights_query(self) -> None:
        captured: dict = {}

        class Results(list):
            metadata = SimpleNamespace(airlines=[], alliances=[])

        real_create_query = server.create_query

        def capture_query(**kwargs):
            query = real_create_query(**kwargs)
            captured.update(kwargs)
            captured["query"] = query
            return query

        with (
            patch.object(server, "create_query", side_effect=capture_query),
            patch.object(server, "get_flights", return_value=Results()),
        ):
            result = server.search_flights(
                origin="MSP",
                destination="SLC",
                departure_date="2099-12-06",
                return_date="2099-12-12",
                max_stops=1,
                outbound_max_stops=0,
                airlines=["dl", "skyteam"],
                outbound_earliest_departure_hour=7,
                outbound_latest_departure_hour=18,
                outbound_earliest_arrival_hour=10,
                outbound_latest_arrival_hour=22,
                return_earliest_departure_hour=8,
                return_latest_departure_hour=19,
                return_earliest_arrival_hour=11,
                return_latest_arrival_hour=23,
                max_duration_minutes=720,
                connecting_airports=["den", "ord"],
                min_layover_minutes=60,
                max_layover_minutes=240,
                less_emissions_only=True,
                max_price=900,
                carry_on_bags=1,
                checked_bags=2,
                hide_separate_and_self_transfer=True,
                exclude_basic_economy=True,
            )

        outbound, inbound = captured["flights"]
        self.assertEqual(outbound.max_stops, 0)
        self.assertEqual(inbound.max_stops, 1)
        self.assertEqual(outbound.airlines, ["DL", "SKYTEAM"])
        self.assertEqual(outbound.earliest_departure_hour, 7)
        self.assertEqual(outbound.latest_departure_hour, 18)
        self.assertEqual(outbound.earliest_arrival_hour, 10)
        self.assertEqual(outbound.latest_arrival_hour, 22)
        self.assertEqual(outbound.max_duration_minutes, 720)
        self.assertEqual(outbound.connecting_airports, ["DEN", "ORD"])
        self.assertEqual(outbound.min_layover_minutes, 60)
        self.assertEqual(outbound.max_layover_minutes, 240)
        self.assertTrue(outbound.less_emissions_only)
        self.assertEqual(inbound.earliest_departure_hour, 8)
        self.assertEqual(inbound.latest_departure_hour, 19)
        self.assertEqual(inbound.earliest_arrival_hour, 11)
        self.assertEqual(inbound.latest_arrival_hour, 23)

        info = captured["query"].pb()
        self.assertEqual(info.max_price, 900)
        self.assertEqual(info.baggage.carry_on_bags, 1)
        self.assertEqual(info.baggage.checked_bags, 2)
        self.assertTrue(info.hide_separate_and_self_transfer)
        self.assertTrue(info.exclude_basic_economy)

        self.assertEqual(result["query"]["max_price"], 900)
        self.assertEqual(result["query"]["connecting_airports"], ["DEN", "ORD"])
        self.assertIn("estimated fees", result["baggage_note"])

    def test_return_filters_require_round_trip(self) -> None:
        with self.assertRaisesRegex(ValueError, "require return_date"):
            server.search_flights(
                origin="MSP",
                destination="SLC",
                departure_date="2099-12-06",
                return_earliest_departure_hour=8,
            )

    def test_upstream_response_and_network_errors_are_structured(self) -> None:
        cases = (
            (
                FlightsResponseError("payload changed"),
                "upstream_response",
            ),
            (
                ConnectError("connection failed"),
                "upstream_unavailable",
            ),
        )
        for exception, error_type in cases:
            with (
                self.subTest(error_type=error_type),
                patch.object(server, "get_flights", side_effect=exception),
            ):
                result = server.search_flights(
                    origin="MSP",
                    destination="DEN",
                    departure_date="2099-12-06",
                )

                self.assertEqual(result["results"], [])
                self.assertEqual(result["error"]["type"], error_type)
                self.assertIn("retry", result["message"])

    def test_rate_limiter_enforces_window(self) -> None:
        async def check() -> None:
            limiter = server.SearchLimiter(per_minute=1, concurrent=1)
            allowed, _ = await limiter.reserve("subject")
            self.assertTrue(allowed)
            limiter.release()
            allowed, retry_after = await limiter.reserve("subject")
            self.assertFalse(allowed)
            self.assertGreaterEqual(retry_after, 1)

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
