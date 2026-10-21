-- Prerazporeditve: kombi kolesa razklada več minut, zato se na cilju ne pojavijo v istem 2-minutnem posnetku.
-- 3 ali več premikov med istima postajama, ki se začnejo in končajo v razmiku do 20 minut, so prerazporeditev.
create index if not exists trips_pair on nb.trips (from_uid, to_uid, start_ts);

create or replace function nb.reclassify_rebalance(since bigint) returns int
language plpgsql as $$
declare n int;
begin
  update nb.trips t set kind = 'rebalance'
  where t.kind = 'trip' and t.from_uid is not null and t.to_uid is not null and t.from_uid <> t.to_uid
    and t.end_ts >= since
    and (select count(*) from nb.trips o
         where o.from_uid = t.from_uid and o.to_uid = t.to_uid and o.kind in ('trip', 'rebalance')
           and o.start_ts between t.start_ts - 1200 and t.start_ts + 1200
           and abs(o.end_ts - t.end_ts) <= 1200) >= 3;
  get diagnostics n = row_count;
  return n;
end $$;

-- po vsakem vnosu voženj preveri zadnjih 6 ur
create or replace function nb.trg_rebalance() returns trigger
language plpgsql as $$
begin
  perform nb.reclassify_rebalance(extract(epoch from now())::bigint - 6 * 3600);
  return null;
end $$;

drop trigger if exists trips_rebalance on nb.trips;
create trigger trips_rebalance after insert on nb.trips for each statement execute function nb.trg_rebalance();

-- popravek za nazaj (vse doslej zabeležene vožnje)
select nb.reclassify_rebalance(0);
