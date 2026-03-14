# Backend Changes for CLI Agent Ergonomics

**Date:** 2026-03-13
**Status:** Proposed
**Affects:** `popcorn-backend` (`services/api/`, `lib/http/`, `lib/core/`)

---

## 1. X-Request-ID Response Header

**Why:** When an agent hits an API error, it needs a correlation ID to include in bug reports or retry logic. The CLI will surface this in `--json` error envelopes.

**Where:** `lib/http/middleware.py`

**Implementation:**
- Add a new middleware (or extend `MetricsMiddleware`) that:
  1. Reads `X-Request-ID` from the incoming request (if the caller provides one)
  2. Falls back to generating a `uuid4` if absent
  3. Sets `X-Request-ID` on the response headers
  4. Attaches it to the request state so error-logging middleware can include it in logs

```python
# Pseudocode
async def dispatch(self, request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response
```

**Effort:** ~30 min. No schema changes, no DB changes.

---

## 2. Thread Listing Endpoint

**Why:** Agents need to discover threads in a channel without knowing parent message IDs upfront. Currently `GET /api/messages/thread?thread_ts=<id>` returns replies for a *known* thread, but there's no way to list which threads exist.

**Where:** `services/api/messages.py`, `lib/core/services/messages.py`, message repository

**Endpoint:**
```
GET /api/messages/threads?conversation_id=<uuid>&limit=50&offset=0
```

**Response:**
```json
{
  "ok": true,
  "threads": [
    {
      "parent_message": { ... },
      "reply_count": 12,
      "last_reply_at": "2026-03-13T10:00:00Z",
      "participants": ["user-id-1", "user-id-2"]
    }
  ]
}
```

**Implementation:**
- Query messages where `parent_message_id IS NULL AND has_replies = true` (or join on child message count), scoped to conversation
- Order by `last_reply_at DESC`
- Include reply count and participant IDs as aggregates

**Effort:** ~2-4 hours depending on existing indexes. May need a `reply_count` materialized field or a subquery.

---

## 3. Webhook Event Type Discovery Endpoint

**Why:** Agents creating webhooks need to know what event types/sources are supported without reading docs.

**Where:** `services/api/webhooks.py`

**Endpoint:**
```
GET /api/webhooks/event-types
```

**Response:**
```json
{
  "ok": true,
  "sources": [
    {
      "name": "github",
      "detection": "X-GitHub-Event header",
      "example_events": ["push", "pull_request", "issues", "release"]
    },
    {
      "name": "linear",
      "detection": "Linear-Event header",
      "example_events": ["Issue", "Comment", "Project"]
    },
    {
      "name": "slack",
      "detection": "X-Slack-Signature header"
    },
    {
      "name": "sentry",
      "detection": "Sentry-Hook-Resource header",
      "example_events": ["error", "issue", "metric_alert"]
    }
  ],
  "action_modes": ["silent", "as_is", "ai_enhanced"]
}
```

**Implementation:**
- Static/semi-static endpoint — data comes from `lib/webhooks/detection.py` constants and `lib/webhooks/models/enums.py`
- Could be a simple dict return, no DB queries needed

**Effort:** ~30 min.

---

## Priority

1. **X-Request-ID** — smallest change, biggest agent impact
2. **Webhook event types** — static data, quick win
3. **Thread listing** — most complex, requires query work
