create table if not exists itinerary_items (
  id             bigint generated always as identity primary key,
  destination_id text not null unique,
  name           text not null,
  location       text,
  category       text,
  latitude       double precision,
  longitude      double precision,
  created_at     timestamptz not null default now()
);

-- Block public access through Supabase's auto-generated REST API.
-- Your FastAPI backend connects directly via Postgres, so it is unaffected.
alter table itinerary_items enable row level security;