"""Postgres access for the Tripwise itinerary (Stage 3).

Design notes:

* One connection per request - opened lazily by the route that needs it and
  closed as soon as the handler finishes. Simple, safe, and correct for a
  small demo API; a shared pool (psycopg_pool) is the upgrade path if this
  ever sees real traffic.
* psycopg 3's async API, so the itinerary routes stay non-blocking and match
  the async style of the Geoapify routes.

Security rule for this module: `settings.database_url` contains the database
user and password. It is never logged, never placed into an exception message,
and never returned to a client. Connection failures are converted into
`DatabaseError` with a generic message and raised `from None`, which suppresses
the original exception chain - psycopg error text can include the host and
user parsed out of the DSN, and we do not want that reaching a response or a
traceback log either.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
from psycopg.rows import dict_row

from config import settings

# Give up quickly rather than hanging a request when the database is down.
CONNECT_TIMEOUT_SECONDS = 10


class DatabaseError(Exception):
    """A database problem that surfaces to the client as HTTP 503.

    The message is fixed and generic; it never contains the connection string.
    """

    def __init__(self, message: str = "Database connection failed.") -> None:
        super().__init__(message)
        self.message = message


@asynccontextmanager
async def get_connection() -> AsyncIterator[psycopg.AsyncConnection]:
    """Yield a fresh connection, closing it when the caller is done.

    Any failure to connect becomes `DatabaseError` (-> 503 in the routes).
    We catch a deliberately broad set of exceptions at this boundary: the DSN
    itself can be rejected by psycopg, and none of those error texts may leak.
    """
    try:
        conn = await psycopg.AsyncConnection.connect(
            settings.database_url,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except Exception:
        raise DatabaseError() from None

    # Rows come back as plain dicts keyed by column name, which keeps the
    # route serialization code readable (row["destination_id"] vs row[1]).
    conn.row_factory = dict_row

    try:
        yield conn
    finally:
        # Close on the happy path too; closing an uncommitted transaction
        # rolls it back, so a partial write never persists.
        await conn.close()
