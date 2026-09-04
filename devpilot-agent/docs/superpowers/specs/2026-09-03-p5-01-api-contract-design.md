# P5-01 API Contract Layer Design

## Context

DevPilot currently exposes its localhost control plane under `/v1`. The
domain manager and route handlers already enforce the important approval,
path, and idempotency rules, but the HTTP surface does not yet match the
frozen plan's `/api/v1` prefix or provide a consistent request-trace field.
FastAPI's default validation response also differs from the project's stable
error contract.

## Goal

Add a thin HTTP contract adapter that makes the existing control-plane
behaviour consumable by a future console and nanobot adapter:

- expose the current supported routes under `/api/v1`;
- preserve `/v1` as a compatibility surface during the migration;
- include a server-generated or validated `request_id` in every JSON response;
- map request validation and unexpected application errors to the stable
  `{error: {code, message}, request_id}` shape without leaking secrets or
  tracebacks;
- keep OpenAPI discoverable and add contract tests for the public guarantees.

## Non-goals

- no React or nanobot WebUI changes;
- no SQLite schema or domain-state changes;
- no new domain project/task/SSE resources beyond the frozen plan's existing
  control-plane surface; the P5-03 console may consume the existing project
  read/registration routes;
- no public authentication or non-loopback binding;
- no command strings, absolute project paths, free-form environment values, or
  secret values added to the HTTP contract.

## Interface

The canonical HTTP prefix becomes `/api/v1`. Every currently supported
control-plane route is registered at that prefix, while the existing `/v1`
route remains available as a compatibility alias with identical safe
behaviour. “Identical” means both prefixes use the same response adapter,
status mapping, request-ID contract, and redaction/path-removal rules; local
filesystem coordinates are not restored on the compatibility alias.
The health check remains `/healthz`; OpenAPI remains `/openapi.json` and
`/docs`.

Each response that is JSON includes a `request_id`. For successful responses,
the existing payload remains unchanged except for this additive field. For
errors, the response is:

```json
{
  "error": {"code": "STABLE_CODE", "message": "safe human-readable message"},
  "request_id": "req_..."
}
```

Clients may send `X-Request-ID` only when it matches a bounded, opaque token
format. Otherwise the server generates one. The same value is returned in the
`X-Request-ID` response header and JSON body. Error status mapping is stable:

For a successful response carrying an `Idempotency-Key`, a valid caller-supplied
`X-Request-ID` is echoed on the first request. The effective identity is stored
with the completed idempotency record, so later replays keep that identity even
if the caller sends a different request ID or omits the header. When no caller
ID is supplied, the server derives a stable opaque ID from the idempotency key.

- `PatchError`: existing domain code and HTTP 409;
- request validation: `REQUEST_INVALID`, HTTP 422;
- unknown application failure: `INTERNAL_ERROR`, HTTP 500, with no traceback;
- non-loopback access: existing `PATH_DENIED`, HTTP 403.

The adapter must not add request IDs to approval tokens, event payloads,
artifacts, or model-visible messages.

## Architecture and data flow

`create_app` remains the single composition root. A small response-contract
middleware obtains the request ID, calls the existing route stack, and adds
the header/body field for JSON responses. Exception handlers centralize
validation and unexpected-error mapping. The route functions continue to call
`ControlPlaneManager` and workflow modules directly; no business logic is
duplicated in the adapter.

The canonical and compatibility route registrations share the same handler
functions and the same safe response adapter. Both prefixes therefore remove
local filesystem coordinates and redact credential-shaped text; the
compatibility alias is not a path-exposure escape hatch. It can be removed in
a later planned migration without changing domain code.

## Testing strategy

Tests cross the HTTP seam using FastAPI `TestClient`:

1. a representative success response from `/api/v1` contains a request ID in
   both body and header;
2. the same request through `/v1` remains compatible;
3. malformed JSON/schema input returns `REQUEST_INVALID` with no traceback;
4. a `PatchError` retains its stable code and request ID;
5. an unexpected exception is redacted as `INTERNAL_ERROR`;
6. OpenAPI lists canonical `/api/v1` routes and does not remove compatibility
   routes;
7. request-ID validation/generation is bounded and does not echo unsafe input.

The existing full suite remains the regression gate. Ruff, Pyright, lock-file,
diff, and secret scans run after implementation.

## Acceptance criteria

- The frozen plan's canonical `/api/v1` prefix is available locally.
- Existing `/v1` tests and callers continue to pass.
- All application JSON responses from the app carry a request ID without exposing tokens
  or tracebacks. The operator-only `POST .../approve` response is the explicit
  exception: it returns the newly issued one-time capability token to its
  caller so the existing control-plane apply/verify flow remains compatible.
  The console never calls that endpoint, and read models, SSE, events, and
  apply results must never expose the token. The standards-defined
  `/openapi.json` document is also an explicit exception to the application
  response adapter: it remains byte-for-byte OpenAPI-compatible and is not
  decorated with application `request_id` or redaction fields.
- The new contract tests pass, and the full test suite remains green.
- No frontend, database migration, or external network behaviour changes.
