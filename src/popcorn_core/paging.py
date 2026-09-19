"""Following a cursor across the listing endpoints.

`/api/conversations/list` and `/api/users/list` both cap a page at a limit the
server enforces and report whether more remain in `response_metadata.next_cursor`
— an empty string once the last page is served. A caller that asks for one
maximal page and stops gets a silently truncated list: no flag, no warning, and
a zero exit code.

Both cursors are OFFSETS into a list the server recomputes per request, not
stable keys, so a conversation created, archived or hidden between two page
fetches shifts every later page and an entry can be skipped or repeated. That
is a property of the endpoints, not something the CLI can correct locally; the
loop below follows the cursor the server sends and accepts the skew, which is
still strictly better than dropping everything past the first page.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import APIClient

# The endpoints cap a page here; asking for more is a validation error, and
# asking for less only multiplies round trips.
PAGE_LIMIT = 1000

# A server that keeps handing back a cursor must not spin the CLI forever.
# Repeating or non-advancing cursors are caught separately — this bounds the
# case where each page looks legitimately new.
MAX_PAGES = 100


def fetch_all(
    client: APIClient,
    path: str,
    params: dict[str, Any],
    key: str,
) -> list[dict[str, Any]]:
    """Return every item under `key`, following the cursor to the last page."""
    return [item for page in iter_pages(client, path, params, key) for item in page]


def iter_pages(
    client: APIClient,
    path: str,
    params: dict[str, Any],
    key: str,
) -> Iterator[list[dict[str, Any]]]:
    """Yield each page's items under `key`, following `next_cursor`.

    A generator so a caller looking for one entry (name resolution) can stop
    reading once it has found it, rather than paying for the whole workspace.
    """
    page_params = {**params, "limit": PAGE_LIMIT}
    seen_cursors: set[str] = set()

    for _ in range(MAX_PAGES):
        resp = client.get(path, page_params)
        yield resp.get(key) or []

        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
        # A cursor we have already followed would loop forever, and one the
        # server repeats after a page is a bug we should not amplify.
        if not cursor or cursor in seen_cursors:
            return
        seen_cursors.add(cursor)
        page_params = {**page_params, "cursor": cursor}
