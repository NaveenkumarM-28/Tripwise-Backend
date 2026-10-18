# Heroku-style process file (Render normally uses render.yaml's startCommand;
# this covers any host that expects a Procfile instead).
# $PORT is provided by the hosting platform at runtime.
# --loop local_loop:local_loop_factory: psycopg's async API needs a selector
# event loop; on Windows uvicorn's default (ProactorEventLoop) would make
# every DB request fail. On Linux the hook defers to uvicorn's own default.
web: uvicorn main:app --host 0.0.0.0 --port $PORT --loop local_loop:local_loop_factory
