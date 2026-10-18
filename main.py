"""Tripwise - FastAPI application entry point.

Stage 2 added:
  * an "attribution" note in the app description (shows up on /docs),
  * the /api/destinations router (list + detail).

Stage 3 added:
  * the /api/itinerary router (add / list / delete),
  * CORS - all origins allowed,
  * global error handlers so *every* failure returns the same JSON shape:
        {"error": "<human-readable message>", "detail": "<reason code>"}
    - request-validation problems -> 400 (instead of FastAPI's default 422),
    - unknown routes / methods    -> 404 / 405 in that same shape,
    - unexpected exceptions       -> 500 with a clean body (no stack trace).

Secrets are read once via config.Settings (shared with the Geoapify service
and db/database.py) - never hardcoded, never logged, never returned.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import settings
from routes.destinations import router as destinations_router
from routes.itinerary import router as itinerary_router

ATTRIBUTION = (
    "Place data by Geoapify (https://www.geoapify.com), "
    "© OpenStreetMap contributors (https://www.openstreetmap.org/copyright)"
)

app = FastAPI(
    title="Tripwise - Backend",
    version="0.3.0",
    description=(
        "Tripwise backend API. Destination search is powered by Geoapify; "
        "saved trips live in Postgres.\n\n"
        "## Attribution & Data Sources\n\n"
        f"{ATTRIBUTION}\n\n"
        "Powered by Geoapify (https://www.geoapify.com). "
        "Map data © OpenStreetMap contributors, available under the "
        "Open Database License (ODbL) - https://www.openstreetmap.org/copyright."
    ),
)

# CORS: allow every origin (Stage 3 requirement). Credentials stay disabled on
# purpose - the wildcard "*" is rejected by browsers together with cookies,
# and this API uses neither.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Stage 2 router: GET /api/destinations and GET /api/destinations/{id}
app.include_router(destinations_router)
# Stage 3 router: POST/GET/DELETE /api/itinerary
app.include_router(itinerary_router)


# ---------------------------------------------------------------------------
# Global error handlers - one shape for every error the API can produce:
#   {"error": "<human-readable message>", "detail": "<reason code>"}
# ---------------------------------------------------------------------------


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError):
    """FastAPI/Pydantic problems (bad types, missing fields) -> 400.

    FastAPI's built-in answer is a 422 with a {"detail": [...]} body that is
    hard to read.  We flatten it into the API-wide error shape instead.
    Only the location and message are kept: the raw `input` value could be
    large and is already known to the caller.
    """
    parts = []
    for err in exc.errors()[:5]:
        location = ".".join(str(part) for part in err.get("loc", ()))
        parts.append(f"{location}: {err.get('msg', 'invalid value')}")
    return JSONResponse(
        status_code=400,
        content={
            "error": "Invalid request: " + "; ".join(parts),
            "detail": "request_validation",
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_error(request: Request, exc: StarletteHTTPException):
    """404 (unknown route) and 405 (wrong method) in the API-wide shape.

    Routers return JSONResponse directly for the errors they raise
    themselves, so this only catches framework-level misses such as
    GET /health (the real route is /api/health).
    """
    return JSONResponse(
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
        content={
            "error": str(exc.detail) or "Request failed.",
            "detail": f"http_{exc.status_code}",
        },
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    """Anything unhandled -> a clean 500.

    The body never includes str(exc): exception text can contain internals
    (file paths, connection details).  Starlette re-raises the exception
    after this response is sent, so uvicorn still prints the full traceback
    to the *server console* for debugging - it just never reaches the client.
    """
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error. Please try again later.",
            "detail": "internal_error",
        },
    )


@app.get("/api/health")
async def health():
    """Basic liveness probe. No external calls, no secrets."""
    return {"status": "ok"}


@app.get("/api/test-geoapify")
async def test_geoapify():
    """Handy smoke test: does the Geoapify key work?

    Kept from Stage 1 but refactored to use the shared settings object and a
    generic error message - httpx exceptions embed the request URL, which
    contains the API key, so we swallow them and report only the reason.
    """
    if not settings.geoapify_api_key:
        return {"ok": False, "reason": "GEOAPIFY_API_KEY missing from .env"}

    url = "https://api.geoapify.com/v1/geocode/search"
    params = {"text": "Panaji", "limit": 1, "apiKey": settings.geoapify_api_key}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params)
    except httpx.TimeoutException:
        return {"ok": False, "reason": "Geoapify request timed out"}
    except httpx.HTTPError:
        return {"ok": False, "reason": "Could not reach Geoapify"}

    if response.status_code != 200:
        return {"ok": False, "reason": f"Geoapify returned status {response.status_code}"}

    features = response.json().get("features", [])
    return {
        "ok": True,
        "first_result": features[0]["properties"]["formatted"] if features else None,
        "attribution": ATTRIBUTION,
    }
