"""Event-loop hook for uvicorn, passed as `--loop local_loop:local_loop_factory`.

Why this file exists: uvicorn >= 0.36 creates its event loop through an
explicit `loop_factory` (bypassing `asyncio.set_event_loop_policy`), and on
Windows its default factory picks ProactorEventLoop. psycopg 3's async mode
uses `add_reader`, which only works on a SelectorEventLoop, so on Windows
every database request would fail ("Psycopg cannot use the 'ProactorEventLoop'
to run in async mode") and surface as a 503 - even though the database is
perfectly reachable.

This module lets the launch command ask for the right loop explicitly:

    uvicorn main:app --loop local_loop:local_loop_factory

uvicorn treats a custom `--loop` import string as "zero-arg callable that
returns an event-loop instance" (note: its built-in loop names behave
differently - they point to factories that return loop *classes*; the custom
path in Config.get_loop_factory skips that indirection). On Windows this
module returns a SelectorEventLoop instance; on other platforms it defers to
uvicorn's own "auto" default (uvloop when installed, otherwise asyncio), so
Render/Linux deployments behave exactly as before.
"""

from __future__ import annotations

import asyncio
import sys


def local_loop_factory(use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    """Return the event-loop *instance* uvicorn should run the app on.

    `use_subprocess` is accepted for symmetry with uvicorn's built-in
    factories (uvicorn does not pass it on the custom-loop path); worker and
    reloader child processes run the app - and therefore psycopg - too, so
    Windows always gets the selector loop.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()

    from uvicorn.loops.auto import auto_loop_factory

    return auto_loop_factory(use_subprocess=use_subprocess)()
