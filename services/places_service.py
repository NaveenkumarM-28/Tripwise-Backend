"""Geoapify Places service for TripWise destination discovery.

This module talks to three Geoapify APIs (all parameters verified against the
live API and the official docs at https://apidocs.geoapify.com):

  1. Geocoding API   - turns a city name into latitude/longitude.
  2. Places API      - finds points of interest near those coordinates using a
                       circle filter and a proximity bias.
  3. Place Details   - fetches extra information for a single place by its id.

The GEOAPIFY_API_KEY is read from the .env file through pydantic-settings (see
config.py). It is never hard-coded, never returned to callers, and never
written into error messages or logs.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from config import settings

# ---------------------------------------------------------------------------
# Endpoints (verified against https://apidocs.geoapify.com/docs/)
# ---------------------------------------------------------------------------
GEOCODE_URL = "https://api.geoapify.com/v1/geocode/search"
PLACES_URL = "https://api.geoapify.com/v2/places"
PLACE_DETAILS_URL = "https://api.geoapify.com/v2/place-details"

# How far (in metres) from the resolved city centre we look for places.
SEARCH_RADIUS_M = 10_000

# Seconds to wait on Geoapify before giving up.
TIMEOUT_SECONDS = 15.0

# Friendly category name -> real Geoapify category string.
#
# IMPORTANT: These exact strings were tested against the live Places API. Some
# intuitive guesses such as "natural.beach", "tourism.museum" or
# "religion.temple" return HTTP 400 ("Category ... is not supported"). The
# values below are the ones Geoapify actually accepts:
#
#   beach      -> "beach"                    (top-level category)
#   museum     -> "entertainment.museum"     (NOT "tourism.museum")
#   park       -> "leisure.park"
#   temple     -> "religion.place_of_worship"(Geoapify has no dedicated temple
#                                             category; this is the closest
#                                             documented match and covers
#                                             temples, churches, mosques, ...)
#   attraction -> "tourism.attraction"
#   restaurant -> "catering.restaurant"
CATEGORY_MAP: dict[str, str] = {
    "beach": "beach",
    "museum": "entertainment.museum",
    "park": "leisure.park",
    "temple": "religion.place_of_worship",
    "attraction": "tourism.attraction",
    "restaurant": "catering.restaurant",
}

# Required by Geoapify and OpenStreetMap (ODbL) licensing. Included in every
# destinations/details JSON response and in the README.
ATTRIBUTION: str = (
    "Place data by Geoapify (https://www.geoapify.com), "
    "© OpenStreetMap contributors (https://www.openstreetmap.org/copyright)"
)


class GeoapifyError(Exception):
    """A Geoapify problem that should surface to the client as HTTP 502.

    The message is always safe to show: it never contains the request URL,
    because httpx embeds the API key inside the URL as ?apiKey=...
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def _fetch_json(url: str, params: dict[str, Any], *, unknown_id_is_none: bool = False) -> Any:
    """GET a JSON document from Geoapify with safe error handling.

    Timeouts, connection failures, non-200 responses and unreadable bodies are
    all converted into a GeoapifyError with a generic message. We deliberately
    discard the original httpx exception object (`raise ... from None`)
    because its string form includes the full URL with the API key, which must
    never reach a client or a log file.

    unknown_id_is_none: for the Place Details API only. Geoapify answers an
    unknown place id with HTTP 400 + {"message": "Invalid Place ID"} (verified
    live); we translate that specific answer into None so the route can turn
    it into a 404 instead of a 502. Any other non-200 stays a GeoapifyError.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(url, params=params)
    except httpx.TimeoutException:
        raise GeoapifyError("Geoapify request timed out. Please try again.") from None
    except httpx.RequestError:
        # str(exc) would include the full URL (and the key), so we drop it.
        raise GeoapifyError("Could not reach Geoapify. Check your connection.") from None

    if response.status_code != 200:
        # Special case: Place Details for an id Geoapify does not know comes
        # back as 400 + {"message": "Invalid Place ID"} (or "...not allowed to
        # be empty"). We only *compare* that message field - we never echo the
        # body back - and map it to None so the route returns 404, not 502.
        if unknown_id_is_none and response.status_code == 400:
            try:
                message = str(response.json().get("message", ""))
            except ValueError:
                message = ""
            # Matches "Invalid Place ID" and '"id" is not allowed to be empty'.
            if "place id" in message.lower() or "not allowed to be empty" in message.lower():
                return None

        # We intentionally do NOT include response.text: Geoapify echoes back
        # request details we do not want to expose.
        raise GeoapifyError(
            "Geoapify returned an unexpected response. "
            "Check the API key and your usage limits."
        )

    try:
        return response.json()
    except ValueError:
        raise GeoapifyError("Geoapify returned an unreadable response.") from None


async def geocode_city(city: str) -> Optional[dict[str, Any]]:
    """Turn a city name into {lat, lon, city} using the Geocoding API.

    Returns None when Geoapify cannot find the place (empty features list),
    which the routes translate into an empty 200 response.
    """
    data = await _fetch_json(
        GEOCODE_URL,
        {"text": city, "limit": 1, "apiKey": settings.geoapify_api_key},
    )
    features = data.get("features") or []
    if not features:
        return None

    geometry = features[0].get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if len(coordinates) < 2:
        return None
    lon, lat = coordinates[0], coordinates[1]

    # Prefer the resolved place name; fall back to whatever the caller typed.
    properties = features[0].get("properties") or {}
    resolved_city = properties.get("city") or properties.get("name") or city
    return {"lat": lat, "lon": lon, "city": resolved_city}


def _friendly_category(
    categories: list[str], requested: Optional[str]
) -> Optional[str]:
    """Choose a friendly category label for a place.

    If the caller asked for a specific category, every result belongs to it, so
    echo that back. Otherwise reverse-map Geoapify's category list to a
    friendly name, falling back to the most specific Geoapify category.
    """
    if requested:
        return requested
    for friendly, geo_category in CATEGORY_MAP.items():
        if geo_category in categories:
            return friendly
    return categories[-1] if categories else None


def _map_place(
    feature: dict[str, Any],
    fallback_city: str,
    requested_category: Optional[str],
) -> Optional[dict[str, Any]]:
    """Convert one Geoapify GeoJSON feature into TripWise's place format.

    Returns None for features without a name so they can be skipped.
    """
    properties = feature.get("properties") or {}
    name = properties.get("name")
    if not name:
        # Geoapify sometimes returns unnamed POIs; the spec says skip them.
        return None

    categories = properties.get("categories") or []
    return {
        "id": properties.get("place_id"),
        "name": name,
        "city": properties.get("city") or fallback_city,
        "category": _friendly_category(categories, requested_category),
        "latitude": properties.get("lat"),
        "longitude": properties.get("lon"),
        "address": properties.get("formatted"),
        "distance_m": properties.get("distance"),
    }


async def search_places(
    city: str,
    category: Optional[str] = None,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], str]:
    """Find places near a city.

    Returns (places, resolved_city). The caller is responsible for sorting and
    trimming to the final limit. Raises GeoapifyError on Geoapify problems.
    """
    location = await geocode_city(city)
    if location is None:
        # City could not be geocoded -> caller returns an empty 200.
        return [], city

    params: dict[str, Any] = {
        # A circle filter keeps results inside the radius; the proximity bias
        # pushes the closest matches to the top of Geoapify's own ranking.
        "filter": (
            f"circle:{location['lon']},{location['lat']},{SEARCH_RADIUS_M}"
        ),
        "bias": f"proximity:{location['lon']},{location['lat']}",
        "limit": limit,
        "apiKey": settings.geoapify_api_key,
    }

    # /v2/places REQUIRES a categories list (verified live: a request without
    # one returns 400 '"value" must contain at least one of [type, categories]';
    # `type` is just an alias for categories, not an "everything" switch).  So
    # when the caller does not filter, we ask for the union of our six
    # whitelisted categories instead - results stay consistent with the
    # filterable set rather than dumping arbitrary POIs.
    if category:
        geo_category = CATEGORY_MAP.get(category)
    else:
        geo_category = ",".join(CATEGORY_MAP.values())
    if geo_category:
        params["categories"] = geo_category

    data = await _fetch_json(PLACES_URL, params)

    places: list[dict[str, Any]] = []
    for feature in data.get("features") or []:
        mapped = _map_place(feature, location["city"], category)
        if mapped is not None:
            places.append(mapped)
    return places, location["city"]


async def get_place_details(place_id: str) -> Optional[dict[str, Any]]:
    """Fetch full details for one place by its Geoapify place id.

    Returns None when the id does not resolve to a place (caller returns 404).
    Raises GeoapifyError on Geoapify problems.
    """
    data = await _fetch_json(
        PLACE_DETAILS_URL,
        {
            "id": place_id,
            "features": "details",
            "apiKey": settings.geoapify_api_key,
        },
        # An unknown id comes back as HTTP 400 "Invalid Place ID"; treat it as
        # "not found" (None -> 404) rather than an upstream failure (502).
        unknown_id_is_none=True,
    )
    if data is None:
        return None
    features = data.get("features") or []
    details_feature = next(
        (
            f
            for f in features
            if (f.get("properties") or {}).get("feature_type") == "details"
        ),
        None,
    )
    if details_feature is None:
        return None

    properties = details_feature.get("properties") or {}
    name = properties.get("name")
    if not name:
        return None

    categories = properties.get("categories") or []
    contact = properties.get("contact") or {}
    return {
        # Echo the id the caller asked for so the response stays stable (the
        # details feature's own place_id can differ from the requested one).
        "id": place_id,
        "name": name,
        "city": properties.get("city"),
        "category": _friendly_category(categories, None),
        "latitude": properties.get("lat"),
        "longitude": properties.get("lon"),
        "address": properties.get("formatted"),
        # Distance is not meaningful for a direct lookup by id.
        "distance_m": None,
        "details": {
            "description": properties.get("description"),
            "website": properties.get("website"),
            "opening_hours": properties.get("opening_hours"),
            "phone": contact.get("phone"),
            "email": contact.get("email"),
            "categories": categories,
        },
    }
