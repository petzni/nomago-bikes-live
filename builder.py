"""Iz baze izračuna statistike in jih zapiše kot JSON za statično stran (site/data/).

Zaženi za vsakim zbiranjem ali vsakih nekaj minut:  python3 builder.py
"""
import bisect
import json
import os
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import common as C

TZ = ZoneInfo(C.TZ)
DAY = 86400


def local(ts):
    return datetime.fromtimestamp(ts, TZ)


def day_start(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def ts_of(dt):
    return int(dt.timestamp())


class StationHistory:
    """Časovna vrsta števila koles na postaji (zapisano ob spremembah)."""

    def __init__(self, rows):
        self.ts = [r[0] for r in rows]
        self.v = [r[1] for r in rows]

    def segments(self, a, b, now):
        """(od, do, kolesa) za interval [a, b), omejeno z začetkom zapisov in zdaj."""
        if not self.ts:
            return []
        b = min(b, now)
        i = max(bisect.bisect_right(self.ts, a) - 1, 0)
        out = []
        while i < len(self.ts) and self.ts[i] < b:
            s = max(self.ts[i], a)
            e = min(self.ts[i + 1] if i + 1 < len(self.ts) else now, b)
            if e > s:
                out.append((s, e, self.v[i]))
            i += 1
        return out


def weather(lat, lng):
    if os.environ.get("NB_WEATHER", "1") != "1" or lat is None:
        return {}
    url = ("https://api.open-meteo.com/v1/forecast?latitude={:.4f}&longitude={:.4f}"
           "&daily=temperature_2m_max,precipitation_sum&past_days=31&forecast_days=1&timezone=Europe%2FLjubljana"
           ).format(lat, lng)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.load(r)["daily"]
        return {t: {"tmax": tm, "rain": pr} for t, tm, pr in
                zip(d["time"], d["temperature_2m_max"], d["precipitation_sum"])}
    except Exception:
        return {}


def summarize(trips):
    n = len(trips)
    km = sum(t["km"] for t in trips)
    return {"trips": n, "km": round(km, 1),
            "avg_min": round(sum(t["dur"] for t in trips) / n / 60, 1) if n else None}


def build_system(con, domains, now, name, stations, hist, wx):
    ph = ",".join("?" * len(domains))
    now_dt = local(now)
    today0 = day_start(now_dt)
    t_today = ts_of(today0)
    since = ts_of(today0 - timedelta(days=60))

    trips = [dict(zip(("bike", "fu", "tu", "start", "end", "dur", "km"), r)) for r in con.execute(
        f"""SELECT bike, from_uid, to_uid, start_ts, end_ts, dur_s, km FROM trips
            WHERE kind='trip' AND domain IN ({ph}) AND start_ts >= ? ORDER BY start_ts""", (*domains, since))]
    st = {uid: s for uid, s in stations.items() if s["domain"] in domains}
    multi = len(domains) > 1

    def sname(uid):
        if uid not in st:
            return "prosto parkirano"
        return f'{st[uid]["name"]} · {st[uid]["city_name"]}' if multi else st[uid]["name"]

    # --- trenutno stanje ---
    fs = con.execute(f"""SELECT ts, sum(available), sum(in_transit) FROM fleet_series
                         WHERE domain IN ({ph}) AND ts >= ? GROUP BY ts ORDER BY ts""",
                     (*domains, now - DAY)).fetchall()
    last = fs[-1] if fs else (now, 0, 0)
    live_st = [s for s in st.values() if s["last_seen"] and s["last_seen"] >= last[0] - 600]
    series = []
    for ts, av, it in fs:  # 10-minutni vzorec
        if not series or ts - series[-1][0] >= 600:
            series.append([ts, av, it])
    live = {
        "ts": last[0], "available": last[1], "in_transit": last[2], "fleet": last[1] + last[2],
        "stations_total": len(live_st),
        "stations_with_bikes": sum(1 for s in live_st if (s["bikes"] or 0) > 0),
        "empty_stations": sum(1 for s in live_st if (s["bikes"] or 0) == 0),
        "series_24h": series,
        "stations": [{"uid": s["uid"], "name": s["name"], "lat": s["lat"], "lng": s["lng"],
                      "bikes": s["bikes"], "free": s["free"], "racks": s["racks"], "domain": s["domain"]}
                     for s in sorted(live_st, key=lambda s: s["name"] or "")],
    }

    # --- po dnevih ---
    by_day = defaultdict(list)
    for t in trips:
        by_day[local(t["start"]).date().isoformat()].append(t)
    peak_it = defaultdict(int)
    for ts, av, it in con.execute(f"""SELECT ts, sum(available), sum(in_transit) FROM fleet_series
                                      WHERE domain IN ({ph}) AND ts >= ? GROUP BY ts""", (*domains, since)):
        d = local(ts).date().isoformat()
        peak_it[d] = max(peak_it[d], it)
    first = con.execute(f"SELECT min(ts) FROM fleet_series WHERE domain IN ({ph})", domains).fetchone()[0] or now
    first_day = local(first).date()
    days30 = [(today0 - timedelta(days=i)).date() for i in range(29, -1, -1)]
    daily = []
    for d in days30:
        if d < first_day:
            continue
        k = d.isoformat()
        row = {"date": k, **summarize(by_day.get(k, [])), "peak_in_transit": peak_it.get(k, 0)}
        if k in wx:
            row.update(wx[k])
        daily.append(row)

    today = [t for t in trips if t["start"] >= t_today]
    yday = [t for t in trips if t_today - DAY <= t["start"] < t_today]
    last7 = [t for t in trips if t["start"] >= now - 7 * DAY]
    prev7 = [t for t in trips if now - 14 * DAY <= t["start"] < now - 7 * DAY]

    eco = summarize(last7)
    eco.update({"co2_kg": round(eco["km"] * C.CO2_KG_PER_KM), "kcal": round(eco["km"] * C.KCAL_PER_KM)})

    def routes(ts_list, n=5, min_n=C.PUBLIC_MIN_ROUTE_TRIPS):
        c = Counter((t["fu"], t["tu"]) for t in ts_list if t["fu"] in st and t["tu"] in st)
        return [{"from": sname(a), "to": sname(b), "loop": a == b, "trips": k}
                for (a, b), k in c.most_common(n) if k >= min_n]

    longest = sorted((t for t in today if t["fu"] != t["tu"]), key=lambda t: -t["km"])[:3]
    hot = Counter()
    for t in trips:
        if t["start"] >= now - DAY:
            hot[t["fu"]] += 1
        if t["end"] >= now - DAY:
            hot[t["tu"]] += 1
    bikes_today = Counter(t["bike"] for t in today)
    iron = bikes_today.most_common(1)
    night = [t for t in today if 2 <= local(t["start"]).hour < 6]
    yhours = Counter(local(t["start"]).hour for t in yday)

    # --- postaje: prazne, luknje, top ---
    empty_share, frozen, holes = [], None, []
    first_full_day = (today0 - timedelta(days=14)).date()
    for uid, s in st.items():
        h = hist.get(uid)
        if not h:
            continue
        segs = h.segments(now - 7 * DAY, now, now)
        tot = sum(e - b for b, e, _ in segs)
        if tot >= 2 * DAY:
            empty_share.append((sum(e - b for b, e, v in segs if v == 0) / tot, uid))
        if s["bikes"] == 0 and h.ts:
            i = len(h.v) - 1
            while i > 0 and h.v[i - 1] == 0:
                i -= 1
            dur = now - h.ts[i]
            if not frozen or dur > frozen["empty_s"]:
                frozen = {"name": sname(uid), "empty_s": dur}
        streak, d = 0, today0 if now_dt.hour >= 9 else today0 - timedelta(days=1)
        while d.date() >= first_full_day:
            segs = h.segments(ts_of(d.replace(hour=8)), ts_of(d.replace(hour=9)), now)
            if segs and min(v for _, _, v in segs) < 2:
                streak += 1
                d -= timedelta(days=1)
            else:
                break
        if streak >= 3:
            holes.append({"name": sname(uid), "days": streak})
    empty_share.sort(reverse=True)

    dep7, dep_prev = Counter(), Counter()
    for t in last7:
        dep7[t["fu"]] += 1
    for t in prev7:
        dep_prev[t["fu"]] += 1
    top_st = [{"name": sname(u), "trips": n, "prev": dep_prev[u],
               "change": round((n - dep_prev[u]) / dep_prev[u] * 100, 1) if dep_prev[u] else None}
              for u, n in dep7.most_common(8) if u in st]

    # --- vzorci (30 dni) ---
    t30 = [t for t in trips if t["start"] >= ts_of(today0 - timedelta(days=29))]
    heat = [[0] * 24 for _ in range(7)]
    wd_days = Counter(d.weekday() for d in days30 if d >= first_day)
    for t in t30:
        lt = local(t["start"])
        heat[lt.weekday()][lt.hour] += 1
    heat = [[round(v / max(wd_days[w], 1), 2) for v in row] for w, row in enumerate(heat)]
    weekday = [round(sum(heat[w]), 1) for w in range(7)]

    fleet_avg = (sum(r[1] + r[2] for r in fs) / len(fs)) if fs else 0
    days_in_7 = min(7, max((now - first) / DAY, 1))
    return {
        "name": name, "domains": domains, "generated_at": now,
        "since": first_day.isoformat(), "days_tracked": (now_dt.date() - first_day).days + 1,
        "live": live,
        "today": summarize(today), "yesterday": summarize(yday),
        "last7": {**eco, "per_day": round(len(last7) / days_in_7, 1),
                  "per_bike_day": round(len(last7) / days_in_7 / fleet_avg, 2) if fleet_avg else None,
                  "change": round((len(last7) - len(prev7)) / len(prev7) * 100, 1) if prev7 else None},
        "daily": daily,
        "routes_today": routes(today), "routes_7d": routes(last7, 10),
        "longest_today": [{"from": sname(t["fu"]), "to": sname(t["tu"]), "km": round(t["km"], 1),
                           "min": round(t["dur"] / 60)} for t in longest],
        "hot_24h": [{"name": sname(u), "moves": n} for u, n in hot.most_common(5) if u in st],
        "frozen": frozen,
        "iron_horse": {"bike": iron[0][0], "trips": iron[0][1],
                       "km": round(sum(t["km"] for t in today if t["bike"] == iron[0][0]), 1)} if iron else None,
        "night_owl": {"trips": len(night), "avg_min": summarize(night)["avg_min"]},
        "peak_hour_yesterday": ({"hour": yhours.most_common(1)[0][0], "trips": yhours.most_common(1)[0][1]}
                                if yhours else None),
        "top_stations_7d": top_st,
        "empty_share_7d": [{"name": sname(u), "pct": round(p * 100, 1)} for p, u in empty_share[:5] if p > 0],
        "morning_holes": sorted(holes, key=lambda h: -h["days"])[:10],
        "heatmap": heat, "weekday": weekday,
    }


def main(now=None):
    con = C.connect()
    now = now or int(time.time())
    try:
        con.execute("SELECT 1 FROM stations LIMIT 1").fetchall()
    except Exception as e:
        raise SystemExit(f"Tabele nb.* ne obstajajo ali niso dosegljive ({type(e).__name__}). "
                         "Ali so se Supabase migracije izvedle?")
    stations = {}
    for r in con.execute("SELECT uid, domain, city_uid, name, lat, lng, racks, last_seen, cur_bikes, cur_free "
                         "FROM stations WHERE spot=1"):
        stations[r[0]] = dict(zip(("uid", "domain", "city", "name", "lat", "lng", "racks", "last_seen",
                                   "bikes", "free"), r))
    city_names = dict(con.execute("SELECT uid, name FROM cities"))
    for s in stations.values():
        s["city_name"] = city_names.get(s["city"], "")
    hist_rows = defaultdict(list)
    for uid, ts, b in con.execute("SELECT station_uid, ts, bikes FROM station_status WHERE ts >= ? ORDER BY ts",
                                  (now - 20 * DAY,)):
        hist_rows[uid].append((ts, b))
    hist = {u: StationHistory(r) for u, r in hist_rows.items()}

    systems = []
    for dom, sysname in con.execute("SELECT domain, name FROM systems ORDER BY domain"):
        cities = [r[0] for r in con.execute(
            """SELECT c.name FROM cities c LEFT JOIN stations s ON s.city_uid = c.uid AND s.spot = 1
               WHERE c.domain=? GROUP BY c.uid, c.name ORDER BY count(s.uid) DESC, c.name""", (dom,))]
        c = con.execute("SELECT avg(lat), avg(lng) FROM stations WHERE domain=?", (dom,)).fetchone()
        auto = (cities[0] + (f" +{len(cities) - 1}" if len(cities) > 1 else "")) if cities else sysname
        systems.append({"domain": dom, "name": C.SYSTEM_NAMES.get(dom) or auto, "cities": cities, "operator": sysname,
                        "lat": c[0], "lng": c[1]})

    os.makedirs(C.SITE_DATA, exist_ok=True)
    out = {}
    for s in systems:
        out[s["domain"]] = build_system(con, [s["domain"]], now, s["name"], stations, hist,
                                        weather(s["lat"], s["lng"]))
    if len(systems) > 1:
        allw = weather(sum(s["lat"] or 0 for s in systems) / len(systems),
                       sum(s["lng"] or 0 for s in systems) / len(systems))
        out["all"] = build_system(con, [s["domain"] for s in systems], now, "Vsi sistemi", stations, hist, allw)
        out["all"]["compare"] = [{
            "domain": d, "name": o["name"], "fleet": o["live"]["fleet"], "stations": o["live"]["stations_total"],
            "per_day": o["last7"]["per_day"], "per_bike_day": o["last7"]["per_bike_day"],
            "avg_min": o["last7"]["avg_min"], "km_7d": o["last7"]["km"], "change": o["last7"]["change"],
        } for d, o in out.items() if d != "all"]

    for k, v in out.items():
        with open(os.path.join(C.SITE_DATA, f"{k}.json"), "w", encoding="utf-8") as f:
            json.dump(v, f, ensure_ascii=False, separators=(",", ":"))
    meta = {"generated_at": now, "demo": os.environ.get("NB_DEMO") == "1",
            "systems": [{"domain": s["domain"], "name": s["name"], "cities": s["cities"]} for s in systems]}
    with open(os.path.join(C.SITE_DATA, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    print(f"built {len(out)} files")


if __name__ == "__main__":
    sys.exit(main())
