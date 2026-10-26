-- Izpadi nextbike vira: kolesa za nekaj minut izginejo iz seznama in se vrnejo na isto postajo.
-- To niso vožnje (kind='feed_gap'):
--  1) 4+ "vrnitev na isto postajo" v isti domeni, ki se začnejo v istem posnetku (±60 s),
--  2) 3+ takih, ki se končajo v istem posnetku (±60 s),
--  3) vse vrnitve na isto postajo v ±10 min okoli že zaznanega izpada (izpad traja več posnetkov).
create or replace function nb.reclassify_feed_gaps(since bigint) returns int
language plpgsql as $$
declare n1 int; n2 int;
begin
  update nb.trips t set kind = 'feed_gap'
  where t.kind = 'trip' and t.from_uid = t.to_uid and t.end_ts >= since
    and ((select count(*) from nb.trips o where o.domain = t.domain and o.from_uid = o.to_uid
           and o.kind in ('trip', 'feed_gap') and o.start_ts between t.start_ts - 60 and t.start_ts + 60) >= 4
      or (select count(*) from nb.trips o where o.domain = t.domain and o.from_uid = o.to_uid
           and o.kind in ('trip', 'feed_gap') and o.end_ts between t.end_ts - 60 and t.end_ts + 60) >= 3);
  get diagnostics n1 = row_count;
  update nb.trips t set kind = 'feed_gap'
  where t.kind = 'trip' and t.from_uid = t.to_uid and t.end_ts >= since
    and exists (select 1 from nb.trips o where o.domain = t.domain and o.kind = 'feed_gap'
                and o.start_ts between t.start_ts - 600 and t.start_ts + 600);
  get diagnostics n2 = row_count;
  return n1 + n2;
end $$;

create or replace function nb.trg_rebalance() returns trigger
language plpgsql as $$
begin
  perform nb.reclassify_rebalance(extract(epoch from now())::bigint - 6 * 3600);
  perform nb.reclassify_feed_gaps(extract(epoch from now())::bigint - 12 * 3600);
  return null;
end $$;

-- popravek za nazaj
select nb.reclassify_feed_gaps(0);
