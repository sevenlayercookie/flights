# Google Flights protobuf exploration

Snapshot date: 2026-07-26

This directory is deliberately separate from the package. The observations are
reverse-engineering evidence, not a claim that the protocol is stable or fully
decoded.

## Newly confirmed structure

### Location type and multi-airport searches

The UI emits an additional field in each `Airport` message:

```protobuf
enum LocationKind {
  LOCATION_KIND_UNSPECIFIED = 0;
  AIRPORT = 1;
  CITY = 2;
}

message AirportLocation {
  LocationKind kind = 1;
  string identifier = 2;
}
```

- An airport is encoded as `{1: 1, 2: "MSP"}`.
- A city is encoded with kind `2` and a Google Knowledge Graph MID, not the
  familiar metro code. NYC became `{1: 2, 2: "/m/02_286"}` and LON became
  `{1: 2, 2: "/m/04jpl"}`.
- The absent/zero kind remains accepted for the simple IATA strings currently
  emitted by fast-flights.
- A multi-origin MSP + ORD search repeats `FlightData` field 13 twice. Thus the
  production schema's singular `from_airport` declaration is sufficient for its
  present API, but does not model the web UI's multi-airport wire format.
- A multi-destination DEN + SLC search independently confirmed the symmetric
  representation: it repeats `FlightData` field 14.

### Sort is a second protobuf

The UI adds a separate `tfu` parameter after it canonicalizes a search:

```protobuf
message SortEnvelope {
  SortOptions options = 2;
}

message SortOptions {
  int32 order = 1;
  int32 unknown_2 = 2;
  int32 unknown_3 = 3;
}
```

- Top flights: `tfu=EgYIABAAGAA`, or `{2: {1: 0, 2: 0, 3: 0}}`.
- Price: `tfu=EgYIAhAAGAA`, or `{2: {1: 2, 2: 0, 3: 0}}`.
- Values 1 and 3 for `order` produced no result controls, so they are not
  additional usable sorts in this experiment.
- Setting `unknown_2` to 1 still produced the normal top-flights UI.
- Setting `unknown_3` to 1 produced no result controls. Its meaning remains
  unknown.

Sort therefore cannot be added solely as another field in the existing `tfs`
message; `Query.params()` would need optional `tfu` support.

### Canonical UI envelope

After any filter change, the UI rewrote the small fast-flights query to include:

```text
Info.1  = 28
Info.2  = 2
Info.14 = 1
Info.16 = {1: -1}  # int64-style ten-byte encoding
Airport.1 = 1      # for literal IATA airports
```

The current meanings of `Info.1`, `Info.2`, `Info.14`, and `Info.16` remain
unknown. Controlled mutations found:

- `Info.1` values 0, 1, 27, 28, and 29 had identical measured results.
- `Info.2` absent/0 and 2 returned results; values 1 and 3 returned an empty
  result group. The UI consistently emits 2.
- `Info.14` values 0 through 3 had identical measured results.
- `Info.16.1` values 0, 1, 2, and -1 had identical measured results.

These may be version, source, or UI-state fields rather than user-facing flight
filters. Their semantics are not decoded.

## UI-to-wire confirmations

| UI change | Wire change |
| --- | --- |
| Nonstop only | `FlightData.5 = 0` |
| Oneworld alliance | `FlightData.6 += "ONEWORLD"` |
| Earliest departure 9 AM | `FlightData.8 = 9`; UI also emitted fields 9–11 with current endpoint defaults |
| Five-hour maximum duration | `FlightData.12 = 300` |
| Minimum 60-minute layover | `FlightData.17 = 60` |
| Less emissions only | packed `FlightData.19 = [1]` |
| Price slider near $400 | `Info.12 = 390` (the UI snapped to its current discrete step) |
| One carry-on bag | `Info.13 = {2: 1, 3: 0}` |
| Hide separate/self-transfer tickets | `Info.17 = 1` |
| Economy excluding Basic | `Info.25 = 1` |
| Sort by price | `tfu.2.1 = 2`; `tfs` did not carry the sort |

The alliance observation means callers can currently pass the special string
`"ONEWORLD"` through the existing `airlines` list, although a typed alliance
API would be clearer.

## Schema-free mutation matrix

The baseline was a UI-canonical MSP–DEN one-way search for 2026-08-09. Both
baseline measurements returned 18 raw rows, 16 priced rows, and the same $174
lowest price and first three itineraries.

Each of these unknown gaps was tested as both varint `1` and a
length-delimited submessage `{1: 1}`:

- `FlightData`: 1, 3, 4, 7, 16, 20
- `Info`: 4, 5, 6, 7, 10, 11, 15, 18, 20, 21, 22, 23, 24, 26

Every one returned the same measured signature as the repeat baseline. This
does **not** prove the fields are unused. It only shows that Google ignored
those particular wire types and values for this route and search mode.

Changing `Airport.1` from airport kind 1 to values 2–4 while retaining the IATA
identifier produced empty results. The separate NYC–LON UI capture explains
kind 2: it is valid for cities but requires a Knowledge Graph MID.

## Encoding behavior

Canonical UI values use URL-safe Base64 without padding. This matters when a
protobuf contains bytes that become `/` or `+` under ordinary Base64. The UI's
`Info.16.1 = -1` made that distinction observable:

- URL-safe, unpadded re-encoding returned the normal 16 priced rows.
- Ordinary Base64 re-encoding of the same bytes returned an empty result group
  in this experiment.

The present production message does not emit the canonical `Info.16` value, so
this is not by itself evidence of a current fast-flights defect. Any future
implementation that adopts the fuller UI envelope should use URL-safe,
unpadded encoding.

## Tools

`protobuf_wire.py` decodes, round-trips, and mutates wire fields without a
schema. `inspect_google_flights_ui.py` drives an installed Chrome build and
captures `tfs` values after UI changes. `probe_unknown_fields.py` runs the
rate-limited read-only mutation matrix and prints only aggregate result
signatures; it never writes raw HTML, cookies, headers, or request tokens.

Example:

```bash
python research/protobuf_wire.py "$TFS"
python research/inspect_google_flights_ui.py --click "Stops, Not selected" \
  --click-text "Nonstop only"
python research/probe_unknown_fields.py --delay 0.35
```

The browser inspector requires Playwright but uses the already-installed Chrome
binary, so it does not need a Playwright browser download.
