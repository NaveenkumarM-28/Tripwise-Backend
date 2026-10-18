# Tripwise - Backend

A FastAPI backend for Tripwise.

- **Stage 2** - destination discovery on top of Geoapify's geocoding and
  places APIs.
- **Stage 3** - a persistent itinerary (saved destinations) backed by
  Supabase Postgres, one `{"error", "detail"}` JSON shape for every error,
  CORS for all origins, and deployment files (Render + Procfile).

## Setup

```powershell
# from "C:\Users\mohan\Tripwise - Backend"
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Your `.env` file must contain a Geoapify key (get a free one at
<https://www.geoapify.com/>) and the Postgres connection string (Supabase in
development):

```
GEOAPIFY_API_KEY=your_key_here
DATABASE_URL=postgresql://postgres:password@host:5432/postgres
```

`.env` is listed in `.gitignore`, so neither value is ever committed. Both are
read by `config.py` (pydantic-settings) and never hardcoded, logged, or
returned in any response - connection errors in particular surface as a fixed
`503` message, never as the psycopg error text (which can quote the DSN).

### Database setup (once)

Create the itinerary table in your Supabase project by running
[`db/schema.sql`](db/schema.sql) in the Supabase SQL editor (or with any
Postgres client). The script is idempotent (`create table if not exists`) and
also enables row-level security so the table is not exposed through
Supabase's public REST API - the backend connects directly over Postgres.

## Run

```powershell
.venv\Scripts\python.exe -m uvicorn main:app --reload --loop local_loop:local_loop_factory
```

Interactive docs: <http://127.0.0.1:8000/docs>

The `--loop local_loop:local_loop_factory` flag matters on **Windows**:
psycopg's async API needs a selector event loop, while uvicorn's Windows
default is ProactorEventLoop - without the flag every database request fails
as a 503. `local_loop.py` simply picks SelectorEventLoop on Windows and
defers to uvicorn's own default elsewhere (so Render/Linux is unaffected).

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/api/health` | Liveness check |
| GET | `/api/test-geoapify` | Smoke-test the Geoapify key |
| GET | `/api/destinations` | List places in a city |
| GET | `/api/destinations/{id}` | Full details for one place |
| POST | `/api/itinerary` | Save a destination into the itinerary |
| GET | `/api/itinerary` | List saved itinerary items, newest first |
| DELETE | `/api/itinerary/{id}` | Remove one itinerary item by id |

### `GET /api/destinations`

| Query param | Rules | Default |
| ----------- | ----- | ------- |
| `city` | required | - |
| `category` | `beach`, `museum`, `park`, `temple`, `attraction`, `restaurant` | union of all six |
| `sortBy` | `name` or `distance` | `name` |
| `order` | `asc` or `desc` | `asc` |
| `limit` | 1-50 | `20` |

Responses:

- **200** with `data`, `count`, `city`, `attribution`. An unknown/un-geocodable
  city or a search with no matches returns `data: []` and `count: 0` (not an error).
- **400** with `{"error": "..."}` for a missing city or an invalid
  `category` / `sortBy` / `order` / `limit`.
- **502** with `{"error": "..."}` when Geoapify times out, is unreachable,
  or answers with a non-200 status. Messages are generic on purpose: httpx
  errors embed the request URL, which contains the API key.

There is deliberately **no `sortBy=rating`** - Geoapify does not return
ratings, and we will not sort by data we do not have.

Note: Geoapify's `/v2/places` **requires** a `categories` value (a request
without one returns HTTP 400), and its `type` parameter is just an alias for
categories - there is no "everything" mode. When no `category` is given, the
service therefore requests the comma-separated union of the six whitelisted
categories, so results always stay within the same set of place types.

### `GET /api/destinations/{id}`

- **200** with `data` + `attribution` (Place Details API).
- **404** when Geoapify does not know the id.
- **502** for upstream failures (same rules as above).

### `POST /api/itinerary`

Saves a destination into the itinerary (stored in Postgres). Body (JSON):

| Field | Rules | Default |
| ----- | ----- | ------- |
| `destinationId` | required non-empty string, must be unique | - |
| `name` | required non-empty string | - |
| `location` | optional string; blank/absent stored as `NULL` | `null` |
| `category` | optional string; blank/absent stored as `NULL` | `null` |
| `latitude` | optional number or `null` (strings/bools rejected) | `null` |
| `longitude` | optional number or `null` (strings/bools rejected) | `null` |

Unknown extra keys in the body are ignored.

- **201** with the saved record: `id`, `destinationId`, `name`, `location`,
  `category`, `latitude`, `longitude`, `createdAt` (ISO-8601).
- **400** for a missing/empty body, malformed JSON, or a missing, blank, or
  wrong-typed field - each with the usual `{"error", "detail"}` shape.
- **409** (`detail: "duplicate_destination"`) when `destinationId` is already
  saved (checked twice: a friendly pre-check, plus the unique constraint
  catching races).
- **503** when the database is unreachable.

### `GET /api/itinerary`

- **200** with `{"data": [...], "count": n}`, newest first.

### `DELETE /api/itinerary/{id}`

- **200** with a confirmation message.
- **400** for a non-numeric or non-positive id.
- **404** when no item has that id.
- **503** when the database is unreachable.

## Error handling

Every error the API can produce uses one JSON shape:

```json
{"error": "<human-readable message>", "detail": "<reason code>"}
```

`error` is what a person reads; `detail` is a short snake_case code a client
can branch on (`missing_city`, `invalid_category`, `missing_field`,
`duplicate_destination`, `not_found`, `database_unavailable`,
`geoapify_error`, ...). Stage 2's `error` messages and status codes are
unchanged - `detail` was added alongside them.

Enforced globally in `main.py`:

- FastAPI/Pydantic request-validation problems -> **400** (not the default
  422), flattened into the shared shape,
- unknown routes -> **404** and wrong methods -> **405** in the same shape,
- any unhandled exception -> **500** with a fixed body; the traceback stays
  in the server console and never reaches the client.

## CORS

`main.py` enables CORS for **all origins** - `allow_origins=["*"]` with all
methods and headers, so a browser frontend on any domain can call the API.
Credentials are deliberately disabled: browsers reject the wildcard
`Access-Control-Allow-Origin: *` together with cookies, and this API uses
neither.

## Deployment

- [`render.yaml`](render.yaml) - Render blueprint: free Python service, build
  from `requirements.txt`, start with uvicorn on `$PORT`, health check on
  `/api/health`. `GEOAPIFY_API_KEY` and `DATABASE_URL` are marked
  `sync: false` and are set in the Render dashboard - never in the file.
- [`Procfile`](Procfile) - the same start command for Heroku-style hosts:
  `web: uvicorn main:app --host 0.0.0.0 --port $PORT`.

## Project layout

```
Tripwise - Backend/
├── config.py                  # shared Settings (.env loader) - avoids circular imports
├── main.py                    # FastAPI app, CORS, global error handlers, routers
├── local_loop.py              # uvicorn --loop hook: selector loop on Windows (psycopg)
├── requirements.txt           # pinned dependencies
├── Procfile                   # Heroku-style start command (uvicorn on $PORT)
├── render.yaml                # Render blueprint (build/start/health check)
├── services/
│   ├── __init__.py
│   └── places_service.py      # all Geoapify HTTP calls + response mapping
├── routes/
│   ├── __init__.py
│   ├── destinations.py        # /api/destinations handlers + validation
│   └── itinerary.py           # /api/itinerary handlers + validation
├── db/
│   ├── __init__.py
│   ├── database.py            # psycopg 3 async connection helper (one per request)
│   └── schema.sql             # itinerary_items table - run once in Supabase
├── test_stage2.ps1            # Stage 2 smoke tests (needs a server on :8000 or -Base)
├── test_stage3.ps1            # Stage 3 smoke tests (starts its own server on :8001)
├── test_stage3_lib.ps1        # helper library for test_stage3.ps1
└── .env                       # secrets, git-ignored
```

**How a request flows:** route validates query params → calls
`services/places_service.py` → service geocodes the city, then fetches nearby
places with a circle filter + proximity bias → route sorts/limits and adds
`attribution`.

## Attribution & Data Sources

This product uses **[Geoapify](https://www.geoapify.com/)** for geocoding,
place search, and place details:

> Powered by [Geoapify](https://www.geoapify.com/)
> on [OpenStreetMap](https://www.openstreetmap.org/copyright) © OpenStreetMap contributors

Map and place data © OpenStreetMap contributors, available under the
[Open Database License (ODbL)](https://www.openstreetmap.org/copyright).

Notes on Geoapify's free plan (from <https://www.geoapify.com/pricing/> and
the attribution docs) as of Stage 2:

- Free plan: **3,000 credits/day**, max **5 requests/second**, commercial use allowed.
- Attribution is **required** on the free plan: a followable
  `Powered by Geoapify` link near the displayed map/place data, plus the
  OpenStreetMap credit.
- The same attribution text is returned in the `attribution` field of every
  `/api/destinations` response and is repeated in the API description shown
  on `/docs`.

## Tests performed (Stage 2)

Run with `powershell -NoProfile -ExecutionPolicy Bypass -File .\test_stage2.ps1`
against a live server + the live Geoapify API — **16/16 passed**:

- happy-path list for Goa (200, `data`/`count`/`city`/`attribution` present)
- `category=restaurant` filter (results all tagged `restaurant`)
- invalid `category` / `sortBy=rating` / `order` / `limit=99` / `limit=0`
  and missing `city` → **400** with the readable `{"error": "..."}` message
- nonsense city (`city=zzzznotacity123xyz`) → **200** with `data: []`, `count: 0`
- `sortBy=name&order=desc` → verified against an independently sorted list
- `sortBy=distance&order=asc` → distances verified non-decreasing
- detail endpoint with a real place id → 200 (+ `attribution`)
- bogus place id → **404** `{"error": "Destination '...' not found."}`
- response bodies checked for `apiKey` leakage → none

Also verified separately:

- **502 path**: server started with an invalid key so Geoapify answers non-200
  → both routes returned `502` with the generic message, and the key did not
  appear in the body.
- **No secrets anywhere**: the API key was scanned against every `.py/.md/.ps1/.sql`
  file and every server log → zero occurrences (only `.env` has it).
- `.gitignore` contains `.env` (the project is not a git repo yet, so this is
  a content check only).
- `/openapi.json` (`/docs`) description includes the Geoapify and OSM links.
- `db/schema.sql` and itinerary/database code: untouched.

Not tested: real network outages/timeouts (the 502 mapping above covers the
non-200 case; the timeout branch is code-reviewed only), and free-plan
rate-limit exhaustion (5 req/s).

## Tests performed (Stage 3)

Run with `powershell -NoProfile -ExecutionPolicy Bypass -File .\test_stage3.ps1`
(self-contained: starts its own uvicorn on port **8001**, never touches 8000;
helpers in `test_stage3_lib.ps1`) against the live Supabase database -
**26/26 passed**:

- empty list → 200 `{"data": [], "count": 0}`
- add → 201 with the full saved record (`id`, camelCase fields, `createdAt`)
- duplicate `destinationId` → **409** `detail: "duplicate_destination"`
- missing `destinationId` / missing `name` / blank `name` / wrong-typed
  `destinationId` / malformed JSON / empty body → **400** in the
  `{"error", "detail"}` shape with the matching reason codes
- second add + list → 200, `data`/`count` correct, newest first
- delete → 200 message; delete again → **404** `not_found`;
  non-numeric id and id `0` → **400**
- global handlers: bad query type → **400** (not 422), unknown route →
  **404**, wrong method → **405**, all in the error shape
- CORS: preflight and simple request both answer `Access-Control-Allow-Origin: *`
- persistence: row (id + `createdAt`) identical after a full server restart
- database down (server started with an unreachable DSN): list and add →
  **503** clean JSON, no traceback, no psycopg text, no connection string
- cleanup: every `test-stage3-*` row removed at the end; foreign rows untouched

Stage 2 regression: `test_stage2.ps1` re-run against a server on port 8001 -
**16/16 still passing** (Stage 2 code unchanged).

Windows note: uvicorn ≥ 0.36 on Windows defaults to ProactorEventLoop, which
psycopg's async API cannot use - every DB request failed as a 503 until the
launch commands gained `--loop local_loop:local_loop_factory` (see
`local_loop.py`). On Linux/Render the hook defers to uvicorn's own default.
