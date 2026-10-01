"""Skupne nastavitve, shema baze in pomožne funkcije."""
import math
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))

# --- Nastavitve (po potrebi spremeni ali nastavi kot env spremenljivke) ---
DOMAINS = os.environ.get("NB_DOMAINS", "cc,cn,ce,cf,cd").split(",")
API_URL = "https://maps.nextbike.net/maps/nextbike-live.json?domains={domains}"
DB_PATH = os.environ.get("NB_DB", os.path.join(BASE, "data", "nbstats.sqlite"))
SITE_DATA = os.environ.get("NB_SITE_DATA", os.path.join(BASE, "site", "data"))
TZ = "Europe/Ljubljana"
# Prikazna imena sistemov na strani. Če domene ni tukaj, se uporabi največje mesto (+ število ostalih).
SYSTEM_NAMES = {
    # "cn": "Celjska regija",
}

# Sklepanje voženj
MIN_TRIP_S = 60            # krajše "vožnje" A->A so ponovni priklopi, ne vožnje
LOOP_MIN_S = 180           # A->A vožnja mora trajati vsaj 3 min
SERVICE_GAP_S = 6 * 3600   # kolo, odsotno > 6 h, je bilo najverjetneje v servisu
REBALANCE_MIN_BIKES = 3    # >=3 kolesa z iste postaje na isto postajo v istem intervalu = prerazporeditev

# Ocene
DETOUR_FACTOR = 1.3        # zračna razdalja * faktor = ocena prevožene poti
LOOP_SPEED_KMH = 10        # za A->A vožnje: ocena km iz trajanja
LOOP_MAX_KM = 8
CO2_KG_PER_KM = 0.15       # ocena prihranka, če vožnja nadomesti avto
KCAL_PER_KM = 30
PUBLIC_MIN_ROUTE_TRIPS = 3 # relacije z manj vožnjami se na javni strani ne prikažejo (GDPR)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (ts INTEGER PRIMARY KEY, ok INTEGER);
CREATE TABLE IF NOT EXISTS systems (domain TEXT PRIMARY KEY, name TEXT, country TEXT);
CREATE TABLE IF NOT EXISTS cities (uid INTEGER PRIMARY KEY, domain TEXT, name TEXT, lat REAL, lng REAL);
CREATE TABLE IF NOT EXISTS stations (
  uid INTEGER PRIMARY KEY, domain TEXT, city_uid INTEGER, number TEXT, name TEXT,
  lat REAL, lng REAL, racks INTEGER, spot INTEGER, last_seen INTEGER,
  cur_bikes INTEGER, cur_free INTEGER);
-- stanje postaje, zapisano samo ob spremembi
CREATE TABLE IF NOT EXISTS station_status (ts INTEGER, station_uid INTEGER, bikes INTEGER, free INTEGER,
  PRIMARY KEY (station_uid, ts));
-- trenutno stanje vsakega kolesa
CREATE TABLE IF NOT EXISTS bike_state (
  number TEXT PRIMARY KEY, domain TEXT, station_uid INTEGER, lat REAL, lng REAL,
  last_seen INTEGER, missing_since INTEGER);
CREATE TABLE IF NOT EXISTS trips (
  id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, bike TEXT,
  from_uid INTEGER, to_uid INTEGER, start_ts INTEGER, end_ts INTEGER,
  dur_s INTEGER, km REAL, kind TEXT);
CREATE INDEX IF NOT EXISTS trips_end ON trips(domain, end_ts);
CREATE INDEX IF NOT EXISTS trips_start ON trips(domain, start_ts);
CREATE TABLE IF NOT EXISTS fleet_series (ts INTEGER, domain TEXT, available INTEGER, in_transit INTEGER,
  PRIMARY KEY (domain, ts));
"""


class PG:
    """Tanek ovoj okoli psycopg, da builder.py dela enako s Postgresom (Supabase) kot s SQLite."""

    is_pg = True

    def __init__(self, dsn):
        import psycopg  # pip install "psycopg[binary]"
        self.c = psycopg.connect(dsn, autocommit=True, prepare_threshold=None)
        self.c.execute("SET search_path TO nb, public")

    def execute(self, sql, params=()):
        return self.c.execute(sql.replace("?", "%s"), params)

    def commit(self):
        pass


def connect(path=DB_PATH):
    """DATABASE_URL (postgres://...) -> Supabase/Postgres, sicer lokalna SQLite datoteka."""
    if os.environ.get("DATABASE_URL"):
        try:
            return PG(os.environ["DATABASE_URL"])
        except Exception as e:
            msg = str(e).replace(os.environ["DATABASE_URL"], "***")
            raise SystemExit(f"Povezava z bazo ni uspela ({type(e).__name__}): {msg}\n"
                             "Preveri DATABASE_URL: Session pooler (port 5432), vpisano geslo brez [ ].")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def haversine_km(lat1, lng1, lat2, lng2):
    if None in (lat1, lng1, lat2, lng2):
        return 0.0
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
