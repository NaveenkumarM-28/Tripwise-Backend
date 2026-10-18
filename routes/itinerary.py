"""Itinerary endpoints (Stage 3).

  POST   /api/itinerary        - save a destination into the itinerary
  GET    /api/itinerary        - list saved items, newest first
  DELETE /api/itinerary/{id}   - remove one item by its numeric id

Like routes/destinations.py, validation is done by hand rather than through
Pydantic models: FastAPI's automatic validation answers 422 with a technical
body, while the spec asks for a 400 with readable JSON. The POST handler
therefore reads the body itself via `request.json()`.

Error shape used by this router (and by the global handlers in main.py):

    {"error": "<human-readable message>", "detail": "<reason code>"}

`error` is what a person reads; `detail` is a short snake_case code a client
can branch on ("not_found", "duplicate_destination", ...). Stage 2's
destination errors use the same two keys - their messages are unchanged.

Database failures (connection refused, bad credentials, ...) surface as 503
with a fixed message: no stack trace, no connection string, no password.
"""

from __future__ import annotations

from typing import Any, Optional

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from db.database import DatabaseError, get_connection

router = APIRouter(prefix="/api/itinerary", tags=["itinerary"])

# bigint primary key ceiling - ids beyond this cannot exist, so we answer 404
# instead of sending an un-parseable number to Postgres (which would be a 503).
MAX_BIGINT = 9_223_372_036_854_775_807

EXAMPLE_BODY = (
    'Example: {"destinationId": "511843394a7c2b4c005f7c86", '
    '"name": "Baga Beach", "category": "beach", "latitude": 15.558, '
    '"longitude": 73.751}'
)


def _error(status_code: int, message: str, code: str) -> JSONResponse:
    """The one error body shape used by Stage 3 responses."""
    return JSONResponse(
        status_code=status_code,
        content={"error": message, "detail": code},
    )


def _db_down() -> JSONResponse:
    """503 for any database problem - deliberately generic wording.

    The message is fixed text: it never includes str(exc) because psycopg
    errors can quote parts of the connection string.
    """
    return _error(
        503,
        "The database is temporarily unreachable. Please try again later.",
        "database_unavailable",
    )


def _require_text(
    payload: dict,
    field: str,
    *,
    required: bool,
) -> tuple[Optional[str], Optional[JSONResponse]]:
    """Validate one string field. Returns (value, error_response).

    - required field absent/null -> 400 "'field' is required..."
    - wrong type                 -> 400 "'field' must be a string (got int)"
    - blank / whitespace         -> 400 "'field' must not be blank"
    - optional and absent/null   -> (None, None)
    - optional and blank         -> (None, None)  -> stored as NULL
    """
    value = payload.get(field)

    if value is None:
        if required:
            return None, _error(
                400,
                f"'{field}' is required and must be a non-empty string.",
                "missing_field",
            )
        return None, None

    if not isinstance(value, str):
        return None, _error(
            400,
            f"'{field}' must be a string (got {type(value).__name__}).",
            "invalid_field_type",
        )

    stripped = value.strip()
    if not stripped:
        if required:
            return None, _error(400, f"'{field}' must not be blank.", "blank_field")
        return None, None

    return stripped, None


def _optional_number(
    payload: dict,
    field: str,
) -> tuple[Optional[float], Optional[JSONResponse]]:
    """Validate one optional numeric field (int/float, not bool, not str)."""
    value = payload.get(field)
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, _error(
            400,
            f"'{field}' must be a number or null (got {type(value).__name__}).",
            "invalid_field_type",
        )
    return value, None


def _to_api(row: dict[str, Any]) -> dict[str, Any]:
    """Map a DB row (snake_case) to the API shape (camelCase).

    `createdAt` stays a datetime here; FastAPI's encoder turns it into an
    ISO-8601 string when the response is serialized.
    """
    return {
        "id": row["id"],
        "destinationId": row["destination_id"],
        "name": row["name"],
        "location": row["location"],
        "category": row["category"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "createdAt": row["created_at"],
    }


@router.post("", status_code=201)
async def add_itinerary_item(request: Request):
    """Save a destination. 400 for bad input, 409 for a duplicate.

    The body is read manually (`request.json()`) instead of declared as a
    Pydantic model, so every rejection is a readable 400 rather than
    FastAPI's default 422. Unknown extra keys in the body are ignored.
    """
    raw = await request.body()
    if not raw or not raw.strip():
        return _error(400, f"A JSON body is required. {EXAMPLE_BODY}", "missing_body")

    try:
        payload = await request.json()
    except Exception:
        return _error(400, "The request body must be valid JSON.", "invalid_json")

    if not isinstance(payload, dict):
        return _error(400, "The request body must be a JSON object.", "invalid_body")

    destination_id, err = _require_text(payload, "destinationId", required=True)
    if err:
        return err
    name, err = _require_text(payload, "name", required=True)
    if err:
        return err
    location, err = _require_text(payload, "location", required=False)
    if err:
        return err
    category, err = _require_text(payload, "category", required=False)
    if err:
        return err
    latitude, err = _optional_number(payload, "latitude")
    if err:
        return err
    longitude, err = _optional_number(payload, "longitude")
    if err:
        return err

    insert_sql = """
        insert into itinerary_items
            (destination_id, name, location, category, latitude, longitude)
        values (%s, %s, %s, %s, %s, %s)
        returning id, destination_id, name, location, category,
                  latitude, longitude, created_at
    """
    params = (destination_id, name, location, category, latitude, longitude)

    try:
        async with get_connection() as conn:
            # Friendly pre-check so the common duplicate case gets a clear
            # message without relying on the constraint error below.
            existing = await conn.execute(
                "select 1 from itinerary_items where destination_id = %s",
                (destination_id,),
            )
            if await existing.fetchone() is not None:
                return _error(
                    409,
                    f"destinationId '{destination_id}' is already in the itinerary.",
                    "duplicate_destination",
                )

            cur = await conn.execute(insert_sql, params)
            row = await cur.fetchone()
            await conn.commit()
    except psycopg.errors.UniqueViolation:
        # Race: two requests saved the same id between our check and insert.
        # The unique constraint on destination_id caught it.
        return _error(
            409,
            f"destinationId '{destination_id}' is already in the itinerary.",
            "duplicate_destination",
        )
    except (DatabaseError, psycopg.Error):
        # Covers refused connections, auth failures, SQL errors, ...
        # psycopg error text can embed parts of the DSN, so we never echo it.
        return _db_down()

    return _to_api(row)


@router.get("")
async def list_itinerary_items():
    """Everything saved in the itinerary, newest first."""
    sql = """
        select id, destination_id, name, location, category,
               latitude, longitude, created_at
        from itinerary_items
        order by created_at desc, id desc
    """
    try:
        async with get_connection() as conn:
            cur = await conn.execute(sql)
            rows = await cur.fetchall()
    except (DatabaseError, psycopg.Error):
        return _db_down()

    return {"data": [_to_api(row) for row in rows], "count": len(rows)}


@router.delete("/{item_id}")
async def delete_itinerary_item(item_id: str):
    """Delete one item. 400 for a non-positive/non-integer id, 404 if absent."""
    if not item_id.isdigit():
        return _error(
            400,
            "'id' must be a positive integer, e.g. /api/itinerary/3.",
            "invalid_path_parameter",
        )
    numeric_id = int(item_id)
    if numeric_id < 1:
        return _error(
            400,
            "'id' must be a positive integer, e.g. /api/itinerary/3.",
            "invalid_path_parameter",
        )
    if numeric_id > MAX_BIGINT:
        # Cannot exist as a bigint row -> 404 rather than a Postgres error.
        return _error(404, f"No itinerary item with id {numeric_id}.", "not_found")

    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                """
                delete from itinerary_items
                where id = %s
                returning id
                """,
                (numeric_id,),
            )
            deleted = await cur.fetchone()
            await conn.commit()
    except (DatabaseError, psycopg.Error):
        return _db_down()

    if deleted is None:
        return _error(404, f"No itinerary item with id {numeric_id}.", "not_found")

    return {"message": f"Itinerary item {numeric_id} deleted."}
