"""Lokalna kopija baze (SQLite) za builder.py, da GitHub Actions ne prenaša celotne zgodovine iz Supabase ob vsakem zagonu.

  python tools/mirror.py pull   # prenese samo nove vrstice iz Supabase v NB_DB (SQLite)
  python tools/mirror.py push   # vrne v Supabase, kar builder ustvari: dnevne povzetke in interne podatke

Kopija živi v predpomnilniku GitHub Actions. Če se izgubi, jo prvi zagon zgradi znova (en večji prenos).
Po migraciji, ki spremeni stare vožnje za nazaj, povečaj MIRROR_VERSION v .github/workflows/pages.yml.
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common as C  # noqa: E402

RECENT_TRIPS_S = 24 * 3600  # vožnje, ki jih baza lahko še prerazvrsti (prerazporeditev, izpad vira), osvežimo vedno


def pg():
    import psycopg
    c = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, prepare_threshold=None)
    c.execute("SET search_path TO nb, public")
    return c


def lite():
    con = sqlite3.connect(C.DB_PATH)
    con.executescript(C.SCHEMA)
    return con


def copy(src, dst, sql, params, table, ncols, replace=True):
    rows, n = src.execute(sql, params), 0
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    batch = []
    for r in rows:
        batch.append(tuple(r))
        if len(batch) >= 5000:
            dst.executemany(f"{verb} INTO {table} VALUES ({','.join('?' * ncols)})", batch); n += len(batch); batch = []
    if batch:
        dst.executemany(f"{verb} INTO {table} VALUES ({','.join('?' * ncols)})", batch); n += len(batch)
    return n


def pull():
    src, dst, t0 = pg(), lite(), time.time()
    mx = lambda q: dst.execute(q).fetchone()[0]
    stats = {}
    # majhne tabele v celoti
    for table, cols, n in (("systems", "domain, name, country", 3), ("cities", "uid, domain, name, lat, lng", 5),
                           ("stations", "uid, domain, city_uid, number, name, lat, lng, racks, spot, last_seen, cur_bikes, cur_free", 12)):
        stats[table] = copy(src, dst, f"SELECT {cols} FROM {table}", (), table, n)
    # časovne vrste: samo novo
    stats["station_status"] = copy(src, dst, "SELECT ts, station_uid, bikes, free FROM station_status WHERE ts > %s",
                                   (mx("SELECT coalesce(max(ts), 0) FROM station_status"),), "station_status", 4)
    stats["fleet_series"] = copy(src, dst, "SELECT ts, domain, available, in_transit FROM fleet_series WHERE ts > %s",
                                 (mx("SELECT coalesce(max(ts), 0) FROM fleet_series"),), "fleet_series", 4)
    # vožnje: nove po id + zadnjih 48 ur znova (vrsta vožnje se lahko še spremeni)
    last_id = mx("SELECT coalesce(max(id), 0) FROM trips")
    recent = int(time.time()) - RECENT_TRIPS_S
    stats["trips"] = copy(src, dst, """SELECT id, domain, bike, from_uid, to_uid, start_ts, end_ts, dur_s, km, kind FROM trips
                                       WHERE id > %s OR end_ts >= %s""", (last_id, recent), "trips", 10)
    # dnevni povzetki (piše jih builder; vir resnice je Supabase)
    stats["station_daily"] = copy(src, dst, "SELECT day, station_uid, empty_s, cov_s, min89 FROM station_daily WHERE day >= %s",
                                  (mx("SELECT coalesce(max(day), '') FROM station_daily"),), "station_daily", 5)
    stats["system_daily"] = copy(src, dst, "SELECT day, domain, fleet_avg, peak_transit FROM system_daily WHERE day >= %s",
                                 (mx("SELECT coalesce(max(day), '') FROM system_daily"),), "system_daily", 4)
    # lokalna kopija ne potrebuje starejše podrobne zgodovine kot baza (60 dni)
    old = int(time.time()) - 62 * 86400
    dst.execute("DELETE FROM station_status WHERE ts < ? AND rowid NOT IN "
                "(SELECT max(rowid) FROM station_status GROUP BY station_uid)", (old,))
    dst.execute("DELETE FROM fleet_series WHERE ts < ?", (old,))
    dst.commit()
    msg = f"mirror pull {time.time() - t0:.1f}s: " + ", ".join(f"{k} {v}" for k, v in stats.items())
    print(f"::notice title=mirror::{msg}" if os.environ.get("GITHUB_ACTIONS") else msg)


def push():
    src, dst = lite(), pg()
    since = (time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400)))
    with dst.cursor() as cur:
        rows = src.execute("SELECT * FROM station_daily WHERE day >= ?", (since,)).fetchall()
        if rows:
            cur.executemany("INSERT INTO station_daily VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows)
        rows2 = src.execute("SELECT * FROM system_daily WHERE day >= ?", (since,)).fetchall()
        if rows2:
            cur.executemany("INSERT INTO system_daily VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows2)
    path = os.path.join(os.environ.get("NB_INTERNAL_DIR", os.path.join(C.BASE, "data")), "internal.json")
    if os.path.exists(path):
        body = open(path, encoding="utf-8").read()
        json.loads(body)
        dst.execute("""INSERT INTO internal_files (path, body, updated_at) VALUES ('bundle', %s::jsonb, now())
                       ON CONFLICT (path) DO UPDATE SET body = excluded.body, updated_at = now()""", (body,))
    print(f"mirror push: station_daily {len(rows)}, system_daily {len(rows2)}, internal {'da' if os.path.exists(path) else 'ne'}")


if __name__ == "__main__":
    {"pull": pull, "push": push}[sys.argv[1]]()
