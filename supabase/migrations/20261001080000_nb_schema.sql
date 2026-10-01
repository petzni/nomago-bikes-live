-- Nomago Bikes v živo: tabele in sklepanje voženj v Postgresu (Supabase).
-- Varno za ponovni zagon. Logika je enaka kot v collector.py (SQLite različica).

create schema if not exists nb;

create table if not exists nb.runs (ts bigint primary key, ok boolean);
create table if not exists nb.systems (domain text primary key, name text, country text);
create table if not exists nb.cities (uid bigint primary key, domain text, name text, lat float8, lng float8);
create table if not exists nb.stations (
  uid bigint primary key, domain text, city_uid bigint, number text, name text,
  lat float8, lng float8, racks int, spot int, last_seen bigint, cur_bikes int, cur_free int);
-- stanje postaje, zapisano samo ob spremembi
create table if not exists nb.station_status (ts bigint, station_uid bigint, bikes int, free int,
  primary key (station_uid, ts));
create index if not exists station_status_ts on nb.station_status (ts);
-- trenutno stanje vsakega kolesa
create table if not exists nb.bike_state (
  number text primary key, domain text, station_uid bigint, lat float8, lng float8,
  last_seen bigint, missing_since bigint);
create table if not exists nb.trips (
  id bigserial primary key, domain text, bike text, from_uid bigint, to_uid bigint,
  start_ts bigint, end_ts bigint, dur_s bigint, km float8, kind text);
create index if not exists trips_end on nb.trips (domain, end_ts);
create index if not exists trips_start on nb.trips (domain, start_ts);
create table if not exists nb.fleet_series (ts bigint, domain text, available int, in_transit int,
  primary key (domain, ts));
create index if not exists fleet_series_ts on nb.fleet_series (ts);

-- tabele niso izpostavljene prek API-ja (shema nb ni javna); za vsak primer še RLS brez politik
do $$ declare t text; begin
  for t in select tablename from pg_tables where schemaname = 'nb' loop
    execute format('alter table nb.%I enable row level security', t);
  end loop;
end $$;

create or replace function nb.km(lat1 float8, lng1 float8, lat2 float8, lng2 float8) returns float8
language sql immutable as $$
  select case when lat1 is null or lng1 is null or lat2 is null or lng2 is null then 0 else
    2 * 6371 * asin(sqrt(power(sin(radians(lat2 - lat1) / 2), 2)
      + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lng2 - lng1) / 2), 2))) end
$$;

-- Obdela en posnetek nextbike-live.json ob času p_ts (unix sekunde). Vrne število novih voženj.
create or replace function nb.ingest(payload jsonb, p_ts bigint) returns int
language plpgsql as $$
declare
  -- nastavitve (enake kot v common.py)
  min_trip_s constant int := 60;
  loop_min_s constant int := 180;
  service_gap_s constant int := 6 * 3600;
  rebalance_min constant int := 3;
  detour constant float8 := 1.3;
  loop_kmh constant float8 := 10;
  loop_max_km constant float8 := 8;
  prev_ts bigint;
  n int;
begin
  select max(ts) into prev_ts from nb.runs where ok;

  drop table if exists _p, _cur, _cand;
  create temp table _p as
  select c->>'domain' as domain, c->>'name' as cname, c->>'country' as country,
         (ci->>'uid')::bigint as city_uid, p
  from jsonb_array_elements(coalesce(payload->'countries', '[]')) c
  cross join jsonb_array_elements(coalesce(c->'cities', '[]')) ci
  cross join jsonb_array_elements(coalesce(ci->'places', '[]')) p
  where c->>'domain' is not null;

  insert into nb.systems
  select distinct on (c->>'domain') c->>'domain', c->>'name', c->>'country'
  from jsonb_array_elements(coalesce(payload->'countries', '[]')) c where c->>'domain' is not null
  on conflict (domain) do update set name = excluded.name, country = excluded.country;

  insert into nb.cities
  select distinct on ((ci->>'uid')::bigint) (ci->>'uid')::bigint, c->>'domain', ci->>'name',
         (ci->>'lat')::float8, (ci->>'lng')::float8
  from jsonb_array_elements(coalesce(payload->'countries', '[]')) c
  cross join jsonb_array_elements(coalesce(c->'cities', '[]')) ci
  where c->>'domain' is not null
  on conflict (uid) do update set domain = excluded.domain, name = excluded.name, lat = excluded.lat, lng = excluded.lng;

  -- postaje: najprej sprememba stanja, nato posodobitev
  insert into nb.station_status
  select p_ts, (p->>'uid')::bigint, coalesce((p->>'bikes')::int, 0), (p->>'free_racks')::int
  from _p left join nb.stations o on o.uid = (p->>'uid')::bigint
  where coalesce((p->>'spot')::boolean, false)
    and (o.uid is null or o.cur_bikes is distinct from coalesce((p->>'bikes')::int, 0)
         or o.cur_free is distinct from (p->>'free_racks')::int)
  on conflict do nothing;

  insert into nb.stations
  select distinct on ((p->>'uid')::bigint) (p->>'uid')::bigint, domain, city_uid, coalesce(p->>'number', ''), p->>'name',
         (p->>'lat')::float8, (p->>'lng')::float8, (p->>'bike_racks')::int, 1, p_ts,
         coalesce((p->>'bikes')::int, 0), (p->>'free_racks')::int
  from _p where coalesce((p->>'spot')::boolean, false)
  on conflict (uid) do update set domain = excluded.domain, city_uid = excluded.city_uid, number = excluded.number,
    name = excluded.name, lat = excluded.lat, lng = excluded.lng, racks = excluded.racks,
    last_seen = excluded.last_seen, cur_bikes = excluded.cur_bikes, cur_free = excluded.cur_free;

  -- kje je zdaj vsako kolo
  create temp table _cur as
  select distinct on (b.bike) b.bike, _p.domain,
         case when coalesce((p->>'spot')::boolean, false) then (p->>'uid')::bigint end as station_uid,
         (p->>'lat')::float8 as lat, (p->>'lng')::float8 as lng
  from _p cross join lateral (
    select x->>'number' as bike from jsonb_array_elements(
      case when jsonb_typeof(p->'bike_list') = 'array' then p->'bike_list' else '[]' end) x
    where x->>'number' is not null
    union all
    select x from jsonb_array_elements_text(
      case when jsonb_typeof(p->'bike_numbers') = 'array' then p->'bike_numbers' else '[]' end) x
    where not coalesce(jsonb_typeof(p->'bike_list') = 'array' and jsonb_array_length(p->'bike_list') > 0, false)
  ) b;

  -- kandidati za vožnje
  create temp table _cand as
  select c.domain, c.bike, s.station_uid as fu, c.station_uid as tu,
         (s.last_seen + s.missing_since) / 2 as st,
         case when prev_ts is not null and prev_ts >= s.missing_since then (prev_ts + p_ts) / 2 else p_ts end as en,
         p_ts - s.last_seen as gap, s.lat as plat, s.lng as plng, c.lat, c.lng
  from _cur c join nb.bike_state s on s.number = c.bike
  where s.missing_since is not null
  union all
  select c.domain, c.bike, s.station_uid, c.station_uid, prev_ts, p_ts, p_ts - prev_ts, s.lat, s.lng, c.lat, c.lng
  from _cur c join nb.bike_state s on s.number = c.bike
  where s.missing_since is null and prev_ts is not null
    and case when s.station_uid is not null or c.station_uid is not null
             then s.station_uid is distinct from c.station_uid
             else nb.km(s.lat, s.lng, c.lat, c.lng) > 0.1 end;

  with grp as (
    select fu, tu from _cand where fu is not null and tu is not null and fu <> tu
    group by fu, tu having count(*) >= rebalance_min
  ), cls as materialized (
    select c.*, greatest(c.en - c.st, 0) as dur,
           case when c.gap > service_gap_s then 'service'
                when g.fu is not null then 'rebalance'
                when greatest(c.en - c.st, 0) < min_trip_s
                  or (c.fu = c.tu and greatest(c.en - c.st, 0) < loop_min_s) then null
                else 'trip' end as kind
    from _cand c left join grp g on g.fu = c.fu and g.tu = c.tu
  ), ins as (
    insert into nb.trips (domain, bike, from_uid, to_uid, start_ts, end_ts, dur_s, km, kind)
    select domain, bike, fu, tu, st, en, dur,
           round((case when fu is not null and fu = tu then least(dur / 3600.0 * loop_kmh, loop_max_km)
                       else nb.km(plat, plng, lat, lng) * detour end)::numeric, 3)::float8,
           kind
    from cls where kind is not null
    returning kind
  )
  select count(*) filter (where kind = 'trip') into n from ins;

  -- posodobi stanje koles
  insert into nb.bike_state
  select bike, domain, station_uid, lat, lng, p_ts, null from _cur
  on conflict (number) do update set domain = excluded.domain, station_uid = excluded.station_uid,
    lat = excluded.lat, lng = excluded.lng, last_seen = excluded.last_seen, missing_since = null;
  update nb.bike_state s set missing_since = p_ts
  where s.missing_since is null and not exists (select 1 from _cur c where c.bike = s.number);

  -- flota: na voljo / v vožnji
  insert into nb.fleet_series
  select p_ts, coalesce(a.domain, t.domain), coalesce(a.n, 0), coalesce(t.n, 0)
  from (
    select domain, sum(case when coalesce((p->>'spot')::boolean, false) then coalesce((p->>'bikes')::int, 0)
                            else greatest(
                              jsonb_array_length(case when jsonb_typeof(p->'bike_numbers') = 'array' then p->'bike_numbers' else '[]' end),
                              jsonb_array_length(case when jsonb_typeof(p->'bike_list') = 'array' then p->'bike_list' else '[]' end), 1) end)::int as n
    from _p group by domain
  ) a full join (
    select domain, count(*)::int as n from nb.bike_state
    where missing_since is not null and p_ts - last_seen <= service_gap_s group by domain
  ) t on t.domain = a.domain
  on conflict (domain, ts) do update set available = excluded.available, in_transit = excluded.in_transit;

  insert into nb.runs values (p_ts, true) on conflict (ts) do update set ok = true;
  drop table if exists _p, _cur, _cand;
  return n;
end $$;
