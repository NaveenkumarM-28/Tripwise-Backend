"""Shared application configuration for TripWise.

This module reads secrets and settings from the .env file in the project root
using pydantic-settings. Both main.py and the Geoapify service import the same
`settings` object, so the .env file is only loaded once.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings.

    The field name `geoapify_api_key` maps to the `GEOAPIFY_API_KEY` variable
    in .env (pydantic-settings is case-insensitive by default).
    """

    geoapify_api_key: str

    # Postgres connection string (Supabase in development). Same rule as the
    # API key: never logged, never echoed in an error - see db/database.py.
    # We keep it as a plain str rather than AnyUrl because a Postgres DSN is
    # not always a well-formed http(s) URL.
    database_url: str

    # extra="ignore" lets .env contain unrelated keys without causing a
    # validation error.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Single shared instance. Importing this triggers .env loading and validation,
# so the app fails fast at startup if GEOAPIFY_API_KEY is missing.
settings = Settings()
