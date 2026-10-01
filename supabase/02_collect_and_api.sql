-- Nomago Bikes v živo: samodejno zbiranje v Supabase (pg_cron + pg_net) in javni pogledi za stran.
-- Zaženi PO 01_schema.sql. Varno za ponovni zagon.
-- Če "create extension" javi napako: Dashboard -> Database -> Extensions -> vklopi pg_cron in pg_net, nato zaženi znova.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Domene sistemov (spremeni tukaj, če dodaš ali odstraniš sistem)
create table if not exists nb.config (key text primary key, value text);
insert into nb.config values ('domains', 'cc,cn,ce,cf,cd') on conflict (key) do nothing;

create table if not exists nb.http_requests (id bigint primary key, requested_at timestamptz default now(), done boolean default false);
alter table nb.config enable row level security;
alter table nb.http_requests enable row level security;

-- Obdela prispele odgovore in pošlje nov zahtevek. pg_net je asinhron, zato vsak klic
-- obdela odgovor prejšnjega (2 min zamika pri obdelavi, čas posnetka pa je pravi).
create or replace function nb.collect_tick() returns void
language plpgsql as $$
declare r record; t bigint;
begin
  for r in
    select q.id, s.status_code, s.content, s.created
    from nb.http_requests q join net._http_response s on s.id = q.id
    where not q.done order by q.id
  loop
    t := extract(epoch from r.created)::bigint;
    begin
      if r.status_code = 200 and r.content is not null then
        perform nb.ingest(r.content::jsonb, t);
      else
        insert into nb.runs values (t, false) on conflict do nothing;
      end if;
    exception when others then
      insert into nb.runs values (t, false) on conflict do nothing;
      raise warning 'nb.ingest napaka: %', sqlerrm;
    end;
    update nb.http_requests set done = true where id = r.id;
  end loop;
  delete from nb.http_requests where requested_at < now() - interval '1 day';

  insert into nb.http_requests (id)
  select net.http_get(
    url := 'https://maps.nextbike.net/maps/nextbike-live.json?domains=' || (select value from nb.config where key = 'domains'),
    headers := '{"User-Agent": "nomago-bikes-live"}'::jsonb,
    timeout_milliseconds := 30000);
end $$;

-- Čiščenje: podrobna zgodovina postaj in flote ni potrebna dlje kot 60 dni (vožnje ostanejo).
create or replace function nb.cleanup() returns void language sql as $$
  delete from nb.station_status s where s.ts < extract(epoch from now())::bigint - 60 * 86400
    and exists (select 1 from nb.station_status n where n.station_uid = s.station_uid and n.ts > s.ts);
  delete from nb.fleet_series where ts < extract(epoch from now())::bigint - 60 * 86400;
  delete from nb.runs where ts < extract(epoch from now())::bigint - 60 * 86400;
$$;

select cron.unschedule(jobid) from cron.job where jobname in ('nb-collect', 'nb-cleanup');
select cron.schedule('nb-collect', '*/2 * * * *', 'select nb.collect_tick()');
select cron.schedule('nb-cleanup', '17 3 * * *', 'select nb.cleanup()');

-- Javni pogledi (samo branje) za živo osveževanje strani prek Supabase REST API-ja.
create or replace view public.nb_live_stations as
  select uid, domain, name, lat, lng, racks, cur_bikes as bikes, cur_free as free, last_seen
  from nb.stations where spot = 1 and last_seen > extract(epoch from now())::bigint - 3600;
create or replace view public.nb_live_fleet as
  select distinct on (domain) domain, ts, available, in_transit
  from nb.fleet_series where ts > extract(epoch from now())::bigint - 3600 order by domain, ts desc;
revoke all on public.nb_live_stations, public.nb_live_fleet from anon, authenticated;
grant select on public.nb_live_stations, public.nb_live_fleet to anon, authenticated;
