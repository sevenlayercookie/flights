from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import jwt
from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.exceptions import FlightsNotFound
from jwt import PyJWKClient
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8000"))
OIDC_ISSUER = os.getenv(
    "OIDC_ISSUER",
    "https://issuer.example.com",
).rstrip("/")
OAUTH_RESOURCE = os.getenv(
    "OAUTH_RESOURCE",
    "http://localhost:8000/mcp",
).rstrip("/")
OAUTH_RESOURCE_ALIASES = {
    value.strip().rstrip("/")
    for value in os.getenv("OAUTH_RESOURCE_ALIASES", "").split(",")
    if value.strip()
}
OAUTH_RESOURCES = {OAUTH_RESOURCE, *OAUTH_RESOURCE_ALIASES}
OAUTH_SCOPE = os.getenv("OAUTH_SCOPE", "flights.read")
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", "65536"))
SEARCHES_PER_MINUTE = int(os.getenv("SEARCHES_PER_MINUTE", "20"))
MAX_CONCURRENT_SEARCHES = int(os.getenv("MAX_CONCURRENT_SEARCHES", "4"))

_resource_url = urlsplit(OAUTH_RESOURCE)
_resource_origin = f"{_resource_url.scheme}://{_resource_url.netloc}"
PROTECTED_RESOURCE_URL = os.getenv(
    "OAUTH_RESOURCE_METADATA_URL",
    f"{_resource_origin}/.well-known/oauth-protected-resource/mcp",
)
AUTH_CHALLENGE = (
    f'Bearer resource_metadata="{PROTECTED_RESOURCE_URL}", '
    f'scope="{OAUTH_SCOPE}"'
)

AIRPORT_CODE = re.compile(r"^[A-Z]{3}$")
AIRLINE_CODE = re.compile(r"^[A-Z0-9]{2}$")
ALLIANCE_CODES = {"SKYTEAM", "STAR_ALLIANCE", "ONEWORLD"}
LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
StopLimit = Literal[0, 1, 2]

JWKS_CLIENT = PyJWKClient(
    f"{OIDC_ISSUER}/.well-known/jwks.json",
    cache_keys=True,
    cache_jwk_set=True,
    headers={
        "Accept": "application/json",
        "User-Agent": "fast-flights-mcp/1.0",
    },
    lifespan=300,
    timeout=5,
)

mcp = FastMCP(
    "fast-flights",
    instructions=(
        "Search live Google Flights prices. Prices can change at any time and "
        "may exclude seat-selection and other ancillary fees. Baggage filters "
        "use Google's estimated partner-supplied fees."
    ),
    host=HOST,
    port=PORT,
    json_response=True,
    stateless_http=True,
)


def _bag_count(value: int, field: str) -> int:
    if not 0 <= value <= 9:
        raise ValueError(f"{field} must be between 0 and 9")
    return value


def _hour(value: int | None, field: str) -> int | None:
    if value is not None and not 0 <= value <= 23:
        raise ValueError(f"{field} must be between 0 and 23")
    return value


def _nonnegative_minutes(value: int | None, field: str) -> int | None:
    if value is not None and value < 0:
        raise ValueError(f"{field} cannot be negative")
    return value


def _positive_int(value: int | None, field: str) -> int | None:
    if value is not None and value < 1:
        raise ValueError(f"{field} must be positive")
    return value


def _ordered_range(
    start: int | None,
    end: int | None,
    start_field: str,
    end_field: str,
) -> None:
    if start is not None and end is not None and start > end:
        raise ValueError(f"{start_field} cannot be greater than {end_field}")


def _baggage_note(carry_on_bags: int, checked_bags: int) -> str:
    if carry_on_bags or checked_bags:
        return (
            "Prices reflect Google Flights' estimated fees for the requested "
            "carry-on and checked bags. Estimates may exclude some taxes or "
            "fees charged later."
        )
    return (
        "No baggage-price filter was requested, so displayed fares may exclude "
        "carry-on or checked-bag fees."
    )


def _parse_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD format") from exc
    if parsed < date.today():
        raise ValueError(f"{field} cannot be in the past")
    return parsed


def _airport(value: str, field: str) -> str:
    code = value.strip().upper()
    if not AIRPORT_CODE.fullmatch(code):
        raise ValueError(f"{field} must be a three-letter IATA airport code")
    return code


def _airport_filters(
    values: list[str] | None,
    field: str,
) -> list[str] | None:
    if values is None:
        return None
    if not values:
        raise ValueError(f"{field} must contain at least one airport")
    if len(values) > 20:
        raise ValueError(f"{field} supports at most 20 airports")

    airports: list[str] = []
    for raw_value in values:
        airport = _airport(raw_value, field)
        if airport not in airports:
            airports.append(airport)
    return airports


def _airline_filters(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    if not values:
        raise ValueError("airlines must contain at least one airline or alliance")
    if len(values) > 20:
        raise ValueError("airlines supports at most 20 filters")

    filters: list[str] = []
    for raw_value in values:
        value = raw_value.strip().upper().replace("-", "_").replace(" ", "_")
        if not AIRLINE_CODE.fullmatch(value) and value not in ALLIANCE_CODES:
            raise ValueError(
                "each airlines entry must be a two-character IATA airline code "
                "or SKYTEAM, STAR_ALLIANCE, or ONEWORLD"
            )
        if value not in filters:
            filters.append(value)
    return filters


def _max_stops(value: StopLimit | None, field: str) -> StopLimit | None:
    if value not in (None, 0, 1, 2):
        raise ValueError(f"{field} must be 0, 1, 2, or omitted")
    return value


def _simple_datetime(value: Any) -> str:
    year, month, day = value.date
    hour = value.time[0] if value.time and value.time[0] is not None else 0
    minute = value.time[1] if len(value.time) > 1 else 0
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}"


def _flight_result(result: Any) -> dict[str, Any]:
    segments = [
        {
            "from": {
                "code": segment.from_airport.code,
                "name": segment.from_airport.name,
            },
            "to": {
                "code": segment.to_airport.code,
                "name": segment.to_airport.name,
            },
            "departure_local": _simple_datetime(segment.departure),
            "arrival_local": _simple_datetime(segment.arrival),
            "duration_minutes": segment.duration,
            "aircraft": segment.plane_type,
        }
        for segment in result.flights
    ]
    return {
        "price": result.price,
        "airline_code": result.type,
        "airlines": result.airlines,
        "stops": max(0, len(segments) - 1),
        "segments": segments,
        "carbon": {
            "emission_grams": result.carbon.emission,
            "typical_on_route_grams": result.carbon.typical_on_route,
        },
    }


def _result_metadata(results: Any) -> dict[str, list[dict[str, str]]]:
    metadata = getattr(results, "metadata", None)
    if metadata is None:
        return {"airlines": [], "alliances": []}
    return {
        "airlines": [
            {"code": airline.code, "name": airline.name}
            for airline in metadata.airlines
        ],
        "alliances": [
            {"code": alliance.code, "name": alliance.name}
            for alliance in metadata.alliances
        ],
    }


@mcp.tool(
    annotations={
        "title": "Search live flight prices",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    meta={
        "securitySchemes": [
            {"type": "oauth2", "scopes": [OAUTH_SCOPE]},
        ]
    },
)
def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str | None = None,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: Literal[
        "economy", "premium-economy", "business", "first"
    ] = "economy",
    currency: str = "USD",
    language: str = "en",
    max_stops: StopLimit | None = None,
    outbound_max_stops: StopLimit | None = None,
    return_max_stops: StopLimit | None = None,
    airlines: list[str] | None = None,
    outbound_earliest_departure_hour: int | None = None,
    outbound_latest_departure_hour: int | None = None,
    outbound_earliest_arrival_hour: int | None = None,
    outbound_latest_arrival_hour: int | None = None,
    return_earliest_departure_hour: int | None = None,
    return_latest_departure_hour: int | None = None,
    return_earliest_arrival_hour: int | None = None,
    return_latest_arrival_hour: int | None = None,
    max_duration_minutes: int | None = None,
    connecting_airports: list[str] | None = None,
    min_layover_minutes: int | None = None,
    max_layover_minutes: int | None = None,
    less_emissions_only: bool = False,
    max_price: int | None = None,
    carry_on_bags: int = 0,
    checked_bags: int = 0,
    hide_separate_and_self_transfer: bool = False,
    exclude_basic_economy: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Search current Google Flights prices for a one-way or round trip.

    Use three-letter IATA airport codes and ISO dates (YYYY-MM-DD). A return
    date makes the query round-trip; omit it for one-way. Prices are for all
    requested passengers. Hours use local airport time on a 0-23 clock.

    Airlines, duration, connection-airport, layover, and emissions filters
    represent Google Flights' search-wide UI filters and are serialized on the
    first flight leg. Time filters can be set separately for each leg.
    """
    origin_code = _airport(origin, "origin")
    destination_code = _airport(destination, "destination")
    if origin_code == destination_code:
        raise ValueError("origin and destination must be different")

    outbound = _parse_date(departure_date, "departure_date")
    inbound = _parse_date(return_date, "return_date") if return_date else None
    if inbound and inbound <= outbound:
        raise ValueError("return_date must be after departure_date")

    passenger_counts = (adults, children, infants_in_seat, infants_on_lap)
    if any(count < 0 for count in passenger_counts):
        raise ValueError("passenger counts cannot be negative")
    if adults < 1:
        raise ValueError("at least one adult is required")
    if sum(passenger_counts) > 9:
        raise ValueError("Google Flights supports at most nine passengers")
    if infants_on_lap > adults:
        raise ValueError("each lap infant requires an adult")

    default_stops = _max_stops(max_stops, "max_stops")
    outbound_stops = _max_stops(outbound_max_stops, "outbound_max_stops")
    inbound_stops = _max_stops(return_max_stops, "return_max_stops")
    if not inbound and return_max_stops is not None:
        raise ValueError("return_max_stops requires return_date")
    effective_outbound_stops = (
        outbound_stops if outbound_stops is not None else default_stops
    )
    effective_return_stops = (
        inbound_stops if inbound_stops is not None else default_stops
    )

    outbound_earliest_departure_hour = _hour(
        outbound_earliest_departure_hour,
        "outbound_earliest_departure_hour",
    )
    outbound_latest_departure_hour = _hour(
        outbound_latest_departure_hour,
        "outbound_latest_departure_hour",
    )
    outbound_earliest_arrival_hour = _hour(
        outbound_earliest_arrival_hour,
        "outbound_earliest_arrival_hour",
    )
    outbound_latest_arrival_hour = _hour(
        outbound_latest_arrival_hour,
        "outbound_latest_arrival_hour",
    )
    return_earliest_departure_hour = _hour(
        return_earliest_departure_hour,
        "return_earliest_departure_hour",
    )
    return_latest_departure_hour = _hour(
        return_latest_departure_hour,
        "return_latest_departure_hour",
    )
    return_earliest_arrival_hour = _hour(
        return_earliest_arrival_hour,
        "return_earliest_arrival_hour",
    )
    return_latest_arrival_hour = _hour(
        return_latest_arrival_hour,
        "return_latest_arrival_hour",
    )
    _ordered_range(
        outbound_earliest_departure_hour,
        outbound_latest_departure_hour,
        "outbound_earliest_departure_hour",
        "outbound_latest_departure_hour",
    )
    _ordered_range(
        outbound_earliest_arrival_hour,
        outbound_latest_arrival_hour,
        "outbound_earliest_arrival_hour",
        "outbound_latest_arrival_hour",
    )
    _ordered_range(
        return_earliest_departure_hour,
        return_latest_departure_hour,
        "return_earliest_departure_hour",
        "return_latest_departure_hour",
    )
    _ordered_range(
        return_earliest_arrival_hour,
        return_latest_arrival_hour,
        "return_earliest_arrival_hour",
        "return_latest_arrival_hour",
    )
    return_time_values = (
        return_earliest_departure_hour,
        return_latest_departure_hour,
        return_earliest_arrival_hour,
        return_latest_arrival_hour,
    )
    if not inbound and any(value is not None for value in return_time_values):
        raise ValueError("return time filters require return_date")

    max_duration = _positive_int(
        max_duration_minutes,
        "max_duration_minutes",
    )
    min_layover = _nonnegative_minutes(
        min_layover_minutes,
        "min_layover_minutes",
    )
    max_layover = _nonnegative_minutes(
        max_layover_minutes,
        "max_layover_minutes",
    )
    _ordered_range(
        min_layover,
        max_layover,
        "min_layover_minutes",
        "max_layover_minutes",
    )
    price_limit = _positive_int(max_price, "max_price")
    airline_filters = _airline_filters(airlines)
    connection_filters = _airport_filters(
        connecting_airports,
        "connecting_airports",
    )
    carry_on_count = _bag_count(carry_on_bags, "carry_on_bags")
    checked_count = _bag_count(checked_bags, "checked_bags")

    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    currency_code = currency.strip().upper()
    if not re.fullmatch(r"^[A-Z]{3}$", currency_code):
        raise ValueError("currency must be a three-letter ISO currency code")
    language_tag = language.strip()
    if len(language_tag) > 35 or not LANGUAGE_TAG.fullmatch(language_tag):
        raise ValueError("language must be a supported BCP 47 language tag")

    flights = [
        FlightQuery(
            date=outbound.isoformat(),
            from_airport=origin_code,
            to_airport=destination_code,
            max_stops=effective_outbound_stops,
            airlines=airline_filters,
            earliest_departure_hour=outbound_earliest_departure_hour,
            latest_departure_hour=outbound_latest_departure_hour,
            earliest_arrival_hour=outbound_earliest_arrival_hour,
            latest_arrival_hour=outbound_latest_arrival_hour,
            max_duration_minutes=max_duration,
            connecting_airports=connection_filters,
            min_layover_minutes=min_layover,
            max_layover_minutes=max_layover,
            less_emissions_only=less_emissions_only,
        )
    ]
    trip = "one-way"
    if inbound:
        trip = "round-trip"
        flights.append(
            FlightQuery(
                date=inbound.isoformat(),
                from_airport=destination_code,
                to_airport=origin_code,
                max_stops=effective_return_stops,
                earliest_departure_hour=return_earliest_departure_hour,
                latest_departure_hour=return_latest_departure_hour,
                earliest_arrival_hour=return_earliest_arrival_hour,
                latest_arrival_hour=return_latest_arrival_hour,
            )
        )

    query = create_query(
        flights=flights,
        seat=cabin,
        trip=trip,
        passengers=Passengers(
            adults=adults,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
        ),
        language=language_tag,
        currency=currency_code,
        max_price=price_limit,
        carry_on_bags=carry_on_count,
        checked_bags=checked_count,
        hide_separate_and_self_transfer=hide_separate_and_self_transfer,
        exclude_basic_economy=exclude_basic_economy,
    )

    query_payload = {
        "origin": origin_code,
        "destination": destination_code,
        "departure_date": outbound.isoformat(),
        "return_date": inbound.isoformat() if inbound else None,
        "trip": trip,
        "cabin": cabin,
        "currency": currency_code,
        "language": language_tag,
        "passengers": {
            "adults": adults,
            "children": children,
            "infants_in_seat": infants_in_seat,
            "infants_on_lap": infants_on_lap,
        },
        "outbound_max_stops": effective_outbound_stops,
        "return_max_stops": effective_return_stops if inbound else None,
        "airlines": airline_filters,
        "outbound_time": {
            "earliest_departure_hour": outbound_earliest_departure_hour,
            "latest_departure_hour": outbound_latest_departure_hour,
            "earliest_arrival_hour": outbound_earliest_arrival_hour,
            "latest_arrival_hour": outbound_latest_arrival_hour,
        },
        "return_time": (
            {
                "earliest_departure_hour": return_earliest_departure_hour,
                "latest_departure_hour": return_latest_departure_hour,
                "earliest_arrival_hour": return_earliest_arrival_hour,
                "latest_arrival_hour": return_latest_arrival_hour,
            }
            if inbound
            else None
        ),
        "max_duration_minutes": max_duration,
        "connecting_airports": connection_filters,
        "min_layover_minutes": min_layover,
        "max_layover_minutes": max_layover,
        "less_emissions_only": less_emissions_only,
        "max_price": price_limit,
        "carry_on_bags": carry_on_count,
        "checked_bags": checked_count,
        "hide_separate_and_self_transfer": hide_separate_and_self_transfer,
        "exclude_basic_economy": exclude_basic_economy,
    }

    try:
        results = get_flights(query)
    except FlightsNotFound as exc:
        return {
            "query": query_payload,
            "results": [],
            "metadata": {"airlines": [], "alliances": []},
            "message": str(exc),
            "baggage_note": _baggage_note(carry_on_count, checked_count),
            "google_flights_url": query.url(),
        }

    return {
        "query": query_payload,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "Google Flights via fast-flights",
        "price_note": (
            "Round-trip prices are total fares for all requested passengers, "
            "paired with Google's lowest compatible return option."
            if inbound
            else "One-way prices are total fares for all requested passengers."
        ),
        "baggage_note": _baggage_note(carry_on_count, checked_count),
        "results": [_flight_result(item) for item in results[:limit]],
        "metadata": _result_metadata(results),
        "google_flights_url": query.url(),
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse(
        {
            "status": "ok",
            "service": "fast-flights-mcp",
            "transport": "streamable-http",
            "endpoint": "/mcp",
        }
    )


@mcp.custom_route(
    "/.well-known/oauth-protected-resource",
    methods=["GET"],
)
@mcp.custom_route(
    "/.well-known/oauth-protected-resource/mcp",
    methods=["GET"],
)
async def oauth_protected_resource(_: Request) -> Response:
    return JSONResponse(
        {
            "resource": OAUTH_RESOURCE,
            "authorization_servers": [OIDC_ISSUER],
            "scopes_supported": [OAUTH_SCOPE],
            "bearer_methods_supported": ["header"],
        }
    )


def verify_access_token(token: str) -> dict[str, Any]:
    if len(token) > 16_384:
        raise ValueError("access token is too large")
    signing_key = JWKS_CLIENT.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=sorted(OAUTH_RESOURCES),
        issuer=OIDC_ISSUER,
        leeway=30,
        options={"require": ["aud", "exp", "iss", "sub"]},
    )

    raw_scope = claims.get("scope", "")
    if isinstance(raw_scope, str):
        scopes = set(raw_scope.split())
    elif isinstance(raw_scope, list):
        scopes = {str(item) for item in raw_scope}
    else:
        scopes = set()
    if OAUTH_SCOPE not in scopes:
        raise ValueError("access token lacks the required scope")
    return cast(dict[str, Any], claims)


def _is_search_tool_call(body: bytes) -> bool:
    if not body:
        return False
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("method") == "tools/call"
        and isinstance(payload.get("params"), dict)
        and payload["params"].get("name") == "search_flights"
    )


class SearchLimiter:
    def __init__(self, per_minute: int, concurrent: int) -> None:
        self.per_minute = per_minute
        self.timestamps: dict[str, deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()
        self.concurrent = asyncio.Semaphore(concurrent)

    async def reserve(self, subject: str) -> tuple[bool, int]:
        now = time.monotonic()
        async with self.lock:
            timestamps = self.timestamps[subject]
            while timestamps and timestamps[0] <= now - 60:
                timestamps.popleft()
            if len(timestamps) >= self.per_minute:
                retry_after = max(1, int(60 - (now - timestamps[0])))
                return False, retry_after
            timestamps.append(now)

        try:
            await asyncio.wait_for(self.concurrent.acquire(), timeout=0.05)
        except TimeoutError:
            return False, 2
        return True, 0

    def release(self) -> None:
        self.concurrent.release()


SEARCH_LIMITER = SearchLimiter(SEARCHES_PER_MINUTE, MAX_CONCURRENT_SEARCHES)


async def _json_response(
    send: Any,
    status: int,
    payload: dict[str, Any],
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    response_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *(headers or []),
    ]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": response_headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


class OAuthSecurityMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            await _json_response(
                send,
                401,
                {"error": "authentication_required"},
                [(b"www-authenticate", AUTH_CHALLENGE.encode())],
            )
            return

        try:
            claims = await asyncio.to_thread(verify_access_token, token)
        except Exception:
            challenge = (
                f"{AUTH_CHALLENGE}, error=\"invalid_token\", "
                'error_description="The access token is invalid or expired"'
            )
            await _json_response(
                send,
                401,
                {"error": "invalid_token"},
                [(b"www-authenticate", challenge.encode())],
            )
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > MAX_REQUEST_BYTES:
                await _json_response(
                    send,
                    413,
                    {"error": "request_too_large"},
                )
                return
            if not message.get("more_body", False):
                break

        request_sent = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal request_sent
            if request_sent:
                return {"type": "http.disconnect"}
            request_sent = True
            return {
                "type": "http.request",
                "body": bytes(body),
                "more_body": False,
            }

        reserved = False
        if _is_search_tool_call(bytes(body)):
            allowed, retry_after = await SEARCH_LIMITER.reserve(str(claims["sub"]))
            if not allowed:
                await _json_response(
                    send,
                    429,
                    {"error": "rate_limited"},
                    [(b"retry-after", str(retry_after).encode())],
                )
                return
            reserved = True

        try:
            await self.app(scope, replay_receive, send)
        finally:
            if reserved:
                SEARCH_LIMITER.release()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        OAuthSecurityMiddleware(mcp.streamable_http_app()),
        host=HOST,
        port=PORT,
        log_level="info",
        access_log=False,
    )
