"""Simulator za test brez dostopa do API-ja.

Ustvari posnetke v enaki obliki kot nextbike-live.json (izmišljena mesta, postaje in kolesa)
in jih po vrsti pošlje zbiralniku. Uporabno za razvoj strani:

    NB_DB=data/demo.sqlite NB_DEMO=1 python3 simulate.py --days 30
    NB_DB=data/demo.sqlite NB_DEMO=1 python3 builder.py
"""
import argparse
import math
import os
import random
import time

import common as C
import collector

NAMES = ["Glavni trg", "Železniška postaja", "Avtobusna postaja", "Zdravstveni dom", "Šolski center",
         "Mestni park", "Tržnica", "Kopališče", "Knjižnica", "Športna dvorana", "Upravna enota",
         "Nakupovalni center", "Fakulteta", "Dijaški dom", "Bolnišnica", "Občina", "Stadion",
         "Industrijska cona", "Jezero", "Grad", "Kino", "Muzej", "Gimnazija", "Osnovna šola",
         "Trg svobode", "Mestni log", "Obrtna cona", "Pokopališče", "Dom starejših", "Sejmišče",
         "Center za socialno delo", "Tehnološki park", "Kampus", "Pošta", "Cerkev", "Most",
         "Rekreacijski center", "Gasilski dom", "Policija", "Sodišče"]
CITIES = [  # domena, ime, center, postaj, voženj/dan (delavnik, lepo vreme), razpršenost km
    ("cc", "Demo mesto CC", (46.051, 14.506), 34, 520, 2.6),
    ("cn", "Demo mesto CN", (46.236, 15.268), 28, 380, 2.4),
    ("ce", "Demo mesto CE", (45.956, 13.648), 22, 260, 2.2),
    ("cf", "Demo mesto CF", (46.131, 14.996), 10, 90, 1.2),
    ("cd", "Demo mesto CD", (45.940, 13.622), 8, 70, 1.0),
    ("cx", "Demo mesto CX", (46.545, 14.955), 12, 90, 1.5),
]
HOUR = [0.2, 0.1, 0.1, 0.1, 0.2, 0.6, 2.2, 5.5, 6.0, 4.0, 4.2, 5.0,
        6.2, 6.4, 7.5, 8.6, 8.2, 7.2, 6.0, 4.6, 3.2, 2.2, 1.3, 0.6]
HOUR = [h / sum(HOUR) for h in HOUR]


def km_off(lat, dx, dy):
    return dy / 111.0, dx / (111.0 * math.cos(math.radians(lat)))


class City:
    def __init__(self, rnd, uid0, domain, name, center, n, daily, spread):
        self.domain, self.name, self.daily = domain, name, daily
        self.uid = uid0
        names = rnd.sample(NAMES, n)
        self.stations = []
        for i in range(n):
            r = spread * math.sqrt(rnd.random()) * (0.3 if i < 4 else 1)
            a = rnd.random() * 2 * math.pi
            dlat, dlng = km_off(center[0], r * math.cos(a), r * math.sin(a))
            self.stations.append({
                "uid": uid0 * 1000 + i, "number": f"{uid0}{i:03d}", "name": names[i].upper(),
                "lat": center[0] + dlat, "lng": center[1] + dlng, "racks": rnd.randint(8, 16),
                "pop": rnd.lognormvariate(0, 0.7) * (2.5 if i < 4 else 1), "bikes": []})
        nb = int(sum(s["racks"] for s in self.stations) * 0.55)
        for b in range(nb):
            s = rnd.choice(self.stations)
            s["bikes"].append(f"{uid0}{b:04d}")
        self.transit = []  # (end_ts, bike, station)
        self.service = []  # (back_ts, bike)
        self.created = 0  # simulirane vožnje (za preverjanje)
        self.partner = None

    def dist(self, a, b):
        return C.haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])

    def step(self, rnd, ts, dt, lt, wx):
        # zaključene vožnje
        keep = []
        for end, bike, st in self.transit:
            (st["bikes"].append(bike) if end <= ts else keep.append((end, bike, st)))
        self.transit = keep
        keep = []
        for back, bike in self.service:
            (rnd.choice(self.stations)["bikes"].append(bike) if back <= ts else keep.append((back, bike)))
        self.service = keep
        # nove vožnje
        wd = 0.65 if lt.tm_wday >= 5 else 1.0
        lam = self.daily * wd * wx * HOUR[lt.tm_hour] * dt / 3600
        n = sum(1 for _ in range(int(lam * 3) + 3) if rnd.random() < lam / (int(lam * 3) + 3))
        for _ in range(n):
            src = [s for s in self.stations if s["bikes"]]
            if not src:
                break
            a = rnd.choices(src, weights=[s["pop"] for s in src])[0]
            bike = a["bikes"].pop(rnd.randrange(len(a["bikes"])))
            if self.partner and rnd.random() < 0.08:  # čezmejna vožnja (GO2GO)
                b = rnd.choice(self.partner.stations)
                mins = max(5, self.dist(a, b) * 1.3 / 12 * 60)
            elif rnd.random() < 0.12:
                b, mins = a, rnd.uniform(6, 50)
            else:
                w = [s["pop"] * math.exp(-self.dist(a, s) / 1.6) if s is not a else 0 for s in self.stations]
                b = rnd.choices(self.stations, weights=w)[0]
                mins = max(2.5, self.dist(a, b) * 1.3 / 12 * 60 * rnd.lognormvariate(0.05, 0.3))
            self.transit.append((ts + mins * 60, bike, b))
            self.created += 1
        # prerazporeditev ob 5:00 in 13:00
        if lt.tm_hour in (5, 13) and lt.tm_min < dt / 60:
            full = sorted(self.stations, key=lambda s: -len(s["bikes"]) / s["racks"])
            for s_from, s_to in zip(full[:2], full[::-1][:2]):
                for _ in range(min(4, len(s_from["bikes"]) - 2)):
                    self.transit.append((ts + 900, s_from["bikes"].pop(), s_to))
        # servis
        if rnd.random() < dt / 86400 * 1.5:
            src = [s for s in self.stations if s["bikes"]]
            if src:
                s = rnd.choice(src)
                self.service.append((ts + rnd.uniform(1, 3) * 86400, s["bikes"].pop()))

    def snapshot(self):
        places = [{"uid": s["uid"], "lat": s["lat"], "lng": s["lng"], "name": s["name"], "spot": True,
                   "number": int(s["number"]), "bikes": len(s["bikes"]), "bike_racks": s["racks"],
                   "free_racks": max(s["racks"] - len(s["bikes"]), 0), "bike_numbers": list(s["bikes"])}
                  for s in self.stations]
        return {"domain": self.domain, "name": "Nomago Bikes (demo)", "country": "SI",
                "cities": [{"uid": self.uid, "name": self.name, "lat": self.stations[0]["lat"],
                            "lng": self.stations[0]["lng"], "places": places}]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--step", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--end", type=int, default=0, help="unix čas zadnjega posnetka (privzeto zdaj)")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    cities = [City(rnd, 900 + i, *c) for i, c in enumerate(CITIES)]
    by = {c.domain: c for c in cities}
    by["ce"].partner, by["cd"].partner = by["cd"], by["ce"]
    con = C.connect()
    end = (a.end or int(time.time())) // a.step * a.step
    ts = end - int(a.days * 86400)
    wx, wx_day = 1.0, None
    t0 = time.time()
    while ts <= end:
        lt = time.localtime(ts)
        if lt.tm_yday != wx_day:
            wx_day, wx = lt.tm_yday, (rnd.uniform(0.35, 0.6) if rnd.random() < 0.22 else rnd.uniform(0.85, 1.15))
        for c in cities:
            c.step(rnd, ts, a.step, lt, wx)
        collector.ingest(con, {"countries": [c.snapshot() for c in cities]}, ts)
        ts += a.step
    real = sum(c.created for c in cities)
    found = con.execute("SELECT count(*) FROM trips WHERE kind='trip'").fetchone()[0]
    print(f"simulated {a.days} days in {time.time() - t0:.0f}s; simulirane vožnje {real}, zaznane {found}")


if __name__ == "__main__":
    main()
