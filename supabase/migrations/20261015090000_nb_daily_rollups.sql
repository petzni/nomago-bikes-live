-- Dnevni povzetki za izbiro obdobij na strani. Podrobna zgodovina (station_status, fleet_series)
-- se po 60 dneh briše, ti povzetki pa ostanejo. Polni jih builder.py.
create table if not exists nb.station_daily (day text, station_uid bigint, empty_s int, cov_s int, min89 int,
  primary key (day, station_uid));
create table if not exists nb.system_daily (day text, domain text, fleet_avg float8, peak_transit int,
  primary key (day, domain));
alter table nb.station_daily enable row level security;
alter table nb.system_daily enable row level security;
