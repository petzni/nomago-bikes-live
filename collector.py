"""Zbiralnik: prebere nextbike-live.json, shrani stanje postaj in iz premikov koles sklepa vožnje.

Zaženi na 1-2 minuti (cron/systemd timer):  python3 collector.py
"""
import json
import sys
import time
import urllib.request
from collections import defaultdict

import common as C


def fetch(domains=C.DOMAINS, timeout=30):
    url = C.API_URL.format(domains=",".join(domains))
    req = urllib.request.Request(url, headers={"User-Agent": "nomago-bikes-stats/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _bike_numbers(place):
    if place.get("bike_list"):
        return [str(b.get("number")) for b in place["bike_list"] if b.get("number") is not None]
    return [str(n) for n in (place.get("bike_numbers") or [])]


def _prev_run(con):
    row = con.execute("SELECT max(ts) FROM runs WHERE ok=1").fetchone()
    return row[0]


def ingest(con, data, ts):
    """Obdela en posnetek API-ja ob času ts (unix sekunde)."""
    if getattr(con, "is_pg", False):  # v Postgresu to naredi SQL funkcija nb.ingest (supabase/01_schema.sql)
        return con.execute("SELECT nb.ingest(%s::jsonb, %s)", (json.dumps(data), ts)).fetchone()[0]
    prev_ts = _prev_run(con)
    cur = {}  # bike -> (domain, station_uid|None, lat, lng)
    available = defaultdict(int)
    station_rows, status_rows = [], []
    last_station = {r[0]: (r[1], r[2]) for r in con.execute("SELECT uid, cur_bikes, cur_free FROM stations")}

    for country in data.get("countries", []):
        domain = country.get("domain")
        if not domain:
            continue
        con.execute("INSERT OR REPLACE INTO systems VALUES (?,?,?)",
                    (domain, country.get("name"), country.get("country")))
        for city in country.get("cities", []):
            con.execute("INSERT OR REPLACE INTO cities VALUES (?,?,?,?,?)",
                        (city.get("uid"), domain, city.get("name"), city.get("lat"), city.get("lng")))
            for p in city.get("places", []):
                spot = bool(p.get("spot"))
                bikes_here = _bike_numbers(p)
                if spot:
                    uid = p.get("uid")
                    nb, free = p.get("bikes") or 0, p.get("free_racks")
                    station_rows.append((uid, domain, city.get("uid"), str(p.get("number") or ""), p.get("name"),
                                         p.get("lat"), p.get("lng"), p.get("bike_racks"), 1, ts, nb, free))
                    if last_station.get(uid) != (nb, free):
                        status_rows.append((ts, uid, nb, free))
                    available[domain] += nb
                    for b in bikes_here:
                        cur[b] = (domain, uid, p.get("lat"), p.get("lng"))
                else:  # prosto parkirano kolo
                    available[domain] += len(bikes_here) or 1
                    for b in bikes_here:
                        cur[b] = (domain, None, p.get("lat"), p.get("lng"))

    con.executemany("""INSERT INTO stations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(uid) DO UPDATE SET domain=excluded.domain, city_uid=excluded.city_uid, number=excluded.number,
        name=excluded.name, lat=excluded.lat, lng=excluded.lng, racks=excluded.racks, last_seen=excluded.last_seen,
        cur_bikes=excluded.cur_bikes, cur_free=excluded.cur_free""", station_rows)
    con.executemany("INSERT OR REPLACE INTO station_status VALUES (?,?,?,?)", status_rows)

    state = {r[0]: r for r in con.execute(
        "SELECT number, domain, station_uid, lat, lng, last_seen, missing_since FROM bike_state")}
    candidates = []  # (domain, bike, from_uid, to_uid, start, end, km, gap, flat, flng)
    upd = []

    for bike, (domain, sid, lat, lng) in cur.items():
        st = state.get(bike)
        if st is None:
            upd.append((bike, domain, sid, lat, lng, ts, None))
            continue
        _, _, psid, plat, plng, last_seen, missing_since = st
        if missing_since is not None:
            # kolo se je vrnilo: konec vožnje nekje med prejšnjim in tem posnetkom
            start = (last_seen + missing_since) / 2
            end = (prev_ts + ts) / 2 if prev_ts and prev_ts >= missing_since else ts
            candidates.append((domain, bike, psid, sid, int(start), int(end), ts - last_seen, plat, plng, lat, lng))
        else:
            moved = (psid != sid) if (psid or sid) else C.haversine_km(plat, plng, lat, lng) > 0.1
            if moved and prev_ts:
                candidates.append((domain, bike, psid, sid, int(prev_ts), int(ts), ts - prev_ts,
                                   plat, plng, lat, lng))
        upd.append((bike, domain, sid, lat, lng, ts, None))

    for bike, st in state.items():
        if bike not in cur and st[6] is None:
            upd.append((bike, st[1], st[2], st[3], st[4], st[5], ts))  # zdaj manjka

    con.executemany("INSERT OR REPLACE INTO bike_state VALUES (?,?,?,?,?,?,?)", upd)

    # razvrsti kandidate
    groups = defaultdict(list)
    for c in candidates:
        if c[2] != c[3] and c[2] is not None and c[3] is not None:
            groups[(c[2], c[3])].append(c)
    rebalance = {id(c) for g in groups.values() if len(g) >= C.REBALANCE_MIN_BIKES for c in g}

    trip_rows = []
    for c in candidates:
        domain, bike, fu, tu, start, end, gap, plat, plng, lat, lng = c
        dur = max(0, end - start)
        if gap > C.SERVICE_GAP_S:
            kind = "service"
        elif id(c) in rebalance:
            kind = "rebalance"
        elif dur < C.MIN_TRIP_S or (fu == tu and fu is not None and dur < C.LOOP_MIN_S):
            continue  # ponovni priklop / prekinjena izposoja
        else:
            kind = "trip"
        if fu is not None and fu == tu:
            km = min(dur / 3600 * C.LOOP_SPEED_KMH, C.LOOP_MAX_KM)
        else:
            km = C.haversine_km(plat, plng, lat, lng) * C.DETOUR_FACTOR
        trip_rows.append((domain, bike, fu, tu, start, end, dur, round(km, 3), kind))
    con.executemany("""INSERT INTO trips (domain, bike, from_uid, to_uid, start_ts, end_ts, dur_s, km, kind)
                       VALUES (?,?,?,?,?,?,?,?,?)""", trip_rows)

    # flota: na voljo / v vožnji
    in_transit = defaultdict(int)
    for domain, n in con.execute("""SELECT domain, count(*) FROM bike_state
            WHERE missing_since IS NOT NULL AND ? - last_seen <= ? GROUP BY domain""", (ts, C.SERVICE_GAP_S)):
        in_transit[domain] = n
    for d in set(available) | set(in_transit):
        con.execute("INSERT OR REPLACE INTO fleet_series VALUES (?,?,?,?)", (ts, d, available[d], in_transit[d]))

    con.execute("INSERT OR REPLACE INTO runs VALUES (?,1)", (ts,))
    con.commit()
    return len([t for t in trip_rows if t[-1] == "trip"])


def main():
    con = C.connect()
    ts = int(time.time())
    try:
        data = fetch()
    except Exception as e:  # API nedosegljiv: zabeleži in nadaljuj naslednjič
        if getattr(con, "is_pg", False):
            con.execute("INSERT INTO runs VALUES (?, false) ON CONFLICT DO NOTHING", (ts,))
        else:
            con.execute("INSERT OR REPLACE INTO runs VALUES (?,0)", (ts,))
        con.commit()
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1
    n = ingest(con, data, ts)
    print(f"ok {ts}: {n} novih voženj")
    return 0


if __name__ == "__main__":
    sys.exit(main())
