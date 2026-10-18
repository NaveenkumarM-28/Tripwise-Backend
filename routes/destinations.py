"""Destination endpoints (Stage 2).

Two routes live here:

  GET /api/destinations        - list places in a city, sorted/limited
  GET /api/destinations/{id}   - full details for one place

Why all the 400 checks in `list_destinations`?  FastAPI's built-in validation
returns 422 (Unprocessable Entity) with a fairly technical error body.  The
Stage 2 spec asks for a 400 with a readable JSON message instead, so we accept
the query params as plain types and validate them by hand.  It is a few extra
lines but the responses stay friendly:

    {"error": "Invalid category 'zoo'. Allowed: beach, museum, park, temple, attraction, restaurant."}
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from services import places_service
from services.places_service import ATTRIBUTION, CATEGORY_MAP, GeoapifyError

router = APIRouter(prefix="/api/destinations", tags=["destinations"])

# Sort options Geoapify can actually support.  Note: there is NO "rating"
# option because Geoapify does not return ratings for places at all - we
# will not sort by data we do not have.
ALLOWED_SORT_BY = ("name", "distance")
ALLOWED_ORDER = ("asc", "desc")
ALLOWED_CATEGORIES = tuple(CATEGORY_MAP.keys())


def _bad_request(message: str, detail: str = "invalid_request") -> dict:
    """Body shape used for every error in this router.

    Stage 3 standardised all API errors on {"error", "detail"}:
      * `error`  - the human-readable message (identical to Stage 2),
      * `detail` - a short machine-readable reason code a client can branch on.
    Status codes and messages are unchanged from Stage 2.
    """
    return {"error": message, "detail": detail}


def _sort_places(places: list[dict], sort_by: str, order: str) -> list[dict]:
    """Sort in place... (pun intended) and return the list.

    - name sorts alphabetically (case-insensitive)
    - distance sorts by distance_m (metres from the city centre)
    - places missing a distance (possible when the API omits it) are pinned
      to the end regardless of order so they never pollute the ranking
    """
    reverse = order == "desc"

    if sort_by == "name":
        return sorted(places, key=lambda p: (p.get("name") or "").lower(), reverse=reverse)

    # distance sort: places missing a distance (the API can omit it) stay
    # pinned to the end no matter which order was requested.
    with_distance = [p for p in places if p.get("distance_m") is not None]
    without_distance = [p for p in places if p.get("distance_m") is None]
    with_distance.sort(key=lambda p: p["distance_m"], reverse=reverse)
    return with_distance + without_distance


@router.get("")
async def list_destinations(
    request: Request,
    city: Optional[str] = Query(None, description="City to explore (required)."),
    category: Optional[str] = Query(None, description="Optional place type filter."),
    sortBy: str = Query("name", description="name or distance (default: name)."),
    order: str = Query("asc", description="asc or desc (default: asc)."),
    # NOTE: no ge=/le= bounds here on purpose - FastAPI would answer 422 for
    # limit=99 before our code runs, but the spec wants a 400 with a readable
    # message, so range-checking happens below.
    limit: int = Query(20, description="1-50 results (default: 20)."),
):
    """List destinations in a city.

    Query parameters are deliberately loose typed so *we* control the error
    messages.  FastAPI would 422 on a bad `limit` before our code runs, so
    `limit` arrives as a plain int only if it is parseable at all - if the
    client sends `limit=abc` FastAPI still 422s, which is acceptable (it is a
    malformed request, not a semantic one).
    """
    # --- required parameter ------------------------------------------------
    if not city or not city.strip():
        return JSONResponse(status_code=400, content=_bad_request(
            "Missing required query parameter 'city'. "
            "Example: /api/destinations?city=Goa",
            "missing_city",
        ))

    # --- enum-ish parameters ----------------------------------------------
    if category is not None and category.lower() not in ALLOWED_CATEGORIES:
        return JSONResponse(status_code=400, content=_bad_request(
            f"Invalid category '{category}'. "
            f"Allowed: {', '.join(ALLOWED_CATEGORIES)}.",
            "invalid_category",
        ))

    if sortBy not in ALLOWED_SORT_BY:
        return JSONResponse(status_code=400, content=_bad_request(
            f"Invalid sortBy '{sortBy}'. Allowed: {', '.join(ALLOWED_SORT_BY)}.",
            "invalid_sort_by",
        ))

    if order not in ALLOWED_ORDER:
        return JSONResponse(status_code=400, content=_bad_request(
            f"Invalid order '{order}'. Allowed: {', '.join(ALLOWED_ORDER)}.",
            "invalid_order",
        ))

    if limit < 1 or limit > 50:
        return JSONResponse(status_code=400, content=_bad_request(
            "Invalid limit. Must be between 1 and 50.",
            "invalid_limit",
        ))

    # --- talk to Geoapify --------------------------------------------------
    # search_places geocodes the city first, then fetches nearby places with a
    # circle filter + proximity bias.  A city Geoapify cannot resolve simply
    # yields an empty list (-> 200, count 0), while timeouts/network/non-200
    # answers raise GeoapifyError (-> 502).
    try:
        places, resolved_city = await places_service.search_places(
            city=city.strip(),
            category=category.lower() if category else None,
            limit=limit,
        )
    except GeoapifyError as exc:
        # The message is intentionally generic: httpx errors embed the full
        # URL (including the API key), and we never let that reach the client
        # or the logs.
        return JSONResponse(status_code=502, content=_bad_request(str(exc), "geoapify_error"))

    places = _sort_places(places, sortBy, order)

    return {
        "data": places,
        "count": len(places),
        "city": resolved_city,
        "attribution": ATTRIBUTION,
    }


@router.get("/{place_id:path}")
async def get_destination(place_id: str, request: Request):
    """Full details for a single place, fetched live from Geoapify.

    We use the Place Details API (`/v2/place-details?id=...&features=details`)
    rather than caching the list results, because the list response is a thin
    projection (name/category/coords only) and Stage 2 explicitly wants the
    richer record on the detail route.  Cost note: one details call = one
    Geoapify credit, same as a places call.

    `:path` converter lets ids containing `/` (some OSM ids do) through.
    """
    if not place_id or not place_id.strip():
        return JSONResponse(status_code=400, content=_bad_request("Place id is required.", "missing_place_id"))

    try:
        details = await places_service.get_place_details(place_id.strip())
    except GeoapifyError as exc:
        return JSONResponse(status_code=502, content=_bad_request(str(exc), "geoapify_error"))

    if details is None:
        return JSONResponse(status_code=404, content=_bad_request(
            f"Destination '{place_id}' not found.",
            "not_found",
        ))

    return {"data": details, "attribution": ATTRIBUTION}
