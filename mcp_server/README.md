# fast-flights MCP server

This directory is intentionally kept on the downstream `agent/mcp-server`
branch. The upstream-oriented search-parameter branch contains only changes to
the `fast_flights` package.

The server exposes one read-only `search_flights` tool over streamable HTTP. It
uses this branch's structured query API, including:

- airline/alliance and per-leg stop filters
- outbound and return departure/arrival time ranges
- maximum trip duration
- required connecting airports and layover duration
- lower-emissions-only results
- maximum price and carry-on/checked-bag counts
- basic-economy and separate-ticket/self-transfer exclusions

## Configuration

Copy `.env.example` to `.env` and replace the example URLs:

```bash
cd mcp_server
cp .env.example .env
docker compose -f compose.example.yaml up --build
```

`OIDC_ISSUER` must be an HTTPS OpenID Connect issuer that signs access tokens
with RS256 and publishes a JWKS document. `OAUTH_RESOURCE` must be the exact
public MCP URL used as the token audience. The expected scope defaults to
`flights.read`.

The example publishes the server only on host loopback at
`http://127.0.0.1:18000/mcp`. Put an HTTPS reverse proxy or tunnel in front of
it for remote clients. Keep proxy credentials and provider-specific deployment
files outside the repository.

The protected-resource metadata endpoints and `/health` are public. `/mcp`
fails closed unless the request has a valid bearer token with the configured
issuer, audience, expiry, and scope.

## Development

From the repository root, with the MCP dependencies installed:

```bash
python -m unittest mcp_server.test_server -v
```

No access tokens, tunnel credentials, HAR files, captured requests, or local
environment files belong on this branch.
