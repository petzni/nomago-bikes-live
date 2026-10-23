"""Diagnoza za razvoz (Operations): kje in kdaj zmanjka koles ali prostih mest, koliko koles je treba
premakniti pred konicama in kako to razdeliti na dve vozili.

    python3 ops/diagnoza.py                          # BICIKEL (cc), od 1. 10. 2026
    python3 ops/diagnoza.py --domain cn --od 2026-10-01

Bere isto bazo kot builder.py (DATABASE_URL = Supabase, sicer lokalni SQLite).
Rezultat: ops/out/diagnoza-<domena>.html (poročilo) in ops/out/postaje-<domena>.csv (vse številke po postajah).
Poročilo vsebuje imena postaj in interne ocene, zato ni za javno objavo.
"""
import argparse
import csv
import html
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common as C  # noqa: E402

TZ = ZoneInfo(C.TZ)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

# Okna konic in trenutek priprave (stanje, s katerim postaja vstopi v konico)
WINDOWS = {
    "jutro": {"prep": (6, 45), "start": 7, "end": 10, "label": "jutranjo konico 7–10"},
    "popoldne": {"prep": (14, 45), "start": 15, "end": 19, "label": "popoldansko konico 15–19"},
}
PEAKS = [(7, 9), (15, 18)]          # zgoščen promet na cestah
RETURN_WEIGHT = 0.7                 # izgubljeno vračilo (polna postaja) je malo manj hudo kot izgubljena izposoja
TOLERANCE = 0.25                    # ciljni interval: zaloge, pri katerih je izguba največ toliko večja od najmanjše
STOP_MIN, BIKE_MIN, DRIVE_MIN = 4.0, 0.75, 9.0   # ocene: postanek, na kolo, vožnja med postanki (izven konic)
HOLIDAYS = {"2026-10-31", "2026-11-01", "2026-12-25", "2026-12-26", "2027-01-01", "2027-01-02"}


def local(ts):
    return datetime.fromtimestamp(ts, TZ)


def geocode(q):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nomago-bikes-live/ops"})
        r = json.load(urllib.request.urlopen(req, timeout=15))
        return (float(r[0]["lat"]), float(r[0]["lon"])) if r else None
    except Exception:
        return None


# ---------- branje ----------

def load(con, domain, t0, t1):
    st = {}
    for uid, name, lat, lng, racks in con.execute(
            "SELECT uid, name, lat, lng, racks FROM stations WHERE domain = ? AND spot = 1", (domain,)):
        st[uid] = {"uid": uid, "name": name, "lat": lat, "lng": lng, "racks": racks or 0}
    hist = defaultdict(list)
    uids = list(st)
    for i in range(0, len(uids), 500):
        part = uids[i:i + 500]
        ph = ",".join("?" * len(part))
        for ts, uid, b, f in con.execute(
                f"SELECT ts, station_uid, bikes, free FROM station_status WHERE station_uid IN ({ph}) AND ts < ? "
                f"ORDER BY station_uid, ts", (*part, t1)):
            hist[uid].append((ts, b, f))
    trips = con.execute("""SELECT from_uid, to_uid, start_ts, end_ts, kind FROM trips
        WHERE domain = ? AND start_ts >= ? AND start_ts < ? AND kind IN ('trip', 'rebalance')""",
                        (domain, t0, t1)).fetchall()
    return st, hist, trips


# ---------- izračun ----------

def is_workday(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def hourly_cover(hist, cap, t0, t1):
    """(dan, ura) -> [s na voljo, s prazna, s polna, s skupaj] in stanje ob trenutkih priprave."""
    out = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    prep = defaultdict(dict)  # okno -> dan -> kolesa
    rows = hist
    if not rows:
        return out, prep
    for i, (ts, b, f) in enumerate(rows):
        a = max(ts, t0)
        e = min(rows[i + 1][0] if i + 1 < len(rows) else t1, t1)
        if e <= a:
            continue
        full = (f == 0) if f is not None else (cap > 0 and b >= cap)
        x = a
        while x < e:
            dt = local(x)
            nxt = int((dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp())
            y = min(e, nxt)
            r = out[(dt.date(), dt.hour)]
            s = y - x
            r[3] += s
            if b > 0:
                r[0] += s
            else:
                r[1] += s
            if full:
                r[2] += s
            x = y
        # stanje ob trenutku priprave
        d = local(a).date()
        while d <= local(e - 1).date():
            for w, cfg in WINDOWS.items():
                h, m = cfg["prep"]
                pt = int(datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).timestamp())
                if a <= pt < e:
                    prep[w][d] = b
            d += timedelta(days=1)
    return out, prep


def window_cost(lp, lr, cap, minutes_per_hour=60):
    """Pričakovane izgube (izposoje, vračila) v oknu za vsako začetno zalogo 0..cap.
    Rojstno-smrtna veriga po minutah: vračilo +1, izposoja -1. lp, lr = stopnje na uro za vsako uro okna."""
    n = cap + 1
    P = np.eye(n)  # vrstica = začetna zaloga, stolpec = trenutna
    lost_p, lost_r = np.zeros(n), np.zeros(n)
    for hp, hr in zip(lp, lr):
        pd, pu = min(hp / 60, 0.5), min(hr / 60, 0.5)
        T = np.zeros((n, n))
        for k in range(n):
            up = pu if k < cap else 0.0
            dn = pd if k > 0 else 0.0
            T[k, k] = 1 - up - dn
            if k < cap:
                T[k, k + 1] = up
            if k > 0:
                T[k, k - 1] = dn
        for _ in range(minutes_per_hour):
            lost_p += P[:, 0] * pd
            lost_r += P[:, cap] * pu
            P = P @ T
    return lost_p, lost_r


def kmeans(X, k, seed=1, iters=60):
    rnd = np.random.default_rng(seed)
    k = min(k, len(X))
    c = X[rnd.choice(len(X), k, replace=False)]
    for _ in range(iters):
        lab = np.argmin(((X[:, None, :] - c[None]) ** 2).sum(-1), axis=1)
        nc = np.array([X[lab == j].mean(0) if (lab == j).any() else c[j] for j in range(k)])
        if np.allclose(nc, c):
            break
        c = nc
    return lab, c


def analyse(st, hist, trips, t0, t1, depot):
    days = [local(t0).date() + timedelta(days=i) for i in range((local(t1 - 1).date() - local(t0).date()).days + 1)]
    wdays = [d for d in days if is_workday(d)]
    nwd = max(len(wdays), 1)

    pick = defaultdict(int)   # (uid, dan, ura)
    ret = defaultdict(int)
    reb = []                  # (start, end, from, to)
    for fu, tu, a, b, kind in trips:
        if kind == "trip":
            la, lb = local(a), local(b)
            if fu in st:
                pick[(fu, la.date(), la.hour)] += 1
            if tu in st:
                ret[(tu, lb.date(), lb.hour)] += 1
        elif fu in st or tu in st:
            reb.append((a, b, fu, tu))

    res = {}
    for uid, s in st.items():
        rows = hist.get(uid, [])
        cap = s["racks"] if s["racks"] > 0 else max(max((b for _, b, _ in rows), default=0), 1)
        cov, prep = hourly_cover(rows, cap, t0, t1)
        H = np.zeros((24, 6))  # na voljo h, prazna h, polna h, skupaj h, izposoje, vračila (delavniki)
        for d in wdays:
            for h in range(24):
                r = cov.get((d, h))
                if r:
                    H[h, :4] += np.array(r) / 3600
                H[h, 4] += pick.get((uid, d, h), 0)
                H[h, 5] += ret.get((uid, d, h), 0)
        if H[:, 3].sum() < 24:  # manj kot dan podatkov
            continue
        notfull = np.maximum(H[:, 3] - H[:, 2], 0)
        # prava stopnja povpraševanja: izposoje na uro, ko so bila kolesa na voljo (z rahlim glajenjem)
        base_p = (H[:, 4].sum() + 1) / (H[:, 0].sum() + 1)
        base_r = (H[:, 5].sum() + 1) / (notfull.sum() + 1)
        prof = (H[:, 4] + H[:, 5] + 0.5) / (H[:, 3] + 0.5)
        prof = prof / max(prof.mean(), 1e-9)
        lp = (H[:, 4] + 2 * base_p * prof) / (H[:, 0] + 2)
        lr = (H[:, 5] + 2 * base_r * prof) / (notfull + 2)
        lost_p_h = lp * H[:, 1] / nwd      # izgubljene izposoje na delavnik po urah
        lost_r_h = lr * H[:, 2] / nwd
        tot_h = np.maximum(H[:, 3] / nwd, 1e-9)
        r = {
            **s, "cap": cap, "lp": lp, "lr": lr,
            "empty_share": H[:, 1] / np.maximum(H[:, 3], 1e-9), "full_share": H[:, 2] / np.maximum(H[:, 3], 1e-9),
            "lost_p_h": lost_p_h, "lost_r_h": lost_r_h,
            "lost_p": lost_p_h.sum(), "lost_r": lost_r_h.sum(),
            "pickups_day": H[:, 4].sum() / nwd, "returns_day": H[:, 5].sum() / nwd,
            "depot_km": C.haversine_km(depot[0], depot[1], s["lat"], s["lng"]) * 1.3 if depot else None,
            "win": {},
        }
        r["empty_min_day"] = (H[:, 1].sum() / nwd) * 60
        r["full_min_day"] = (H[:, 2].sum() / nwd) * 60
        for w, cfg in WINDOWS.items():
            hrs = range(cfg["start"], cfg["end"])
            cp, cr = window_cost([lp[h] for h in hrs], [lr[h] for h in hrs], cap)
            cost = cp + RETURN_WEIGHT * cr
            best = cost.min()
            ok = np.where(cost <= best + TOLERANCE)[0]
            lo, hi = int(ok.min()), int(ok.max())
            obs = [prep[w][d] for d in wdays if d in prep[w]]
            med = float(np.median(obs)) if obs else None
            move = 0 if med is None else (lo - med if med < lo else (hi - med if med > hi else 0))
            saved = 0.0 if med is None else float(cost[min(int(round(med)), cap)] - best)
            r["win"][w] = {"lo": lo, "hi": hi, "now": med, "move": int(round(move)), "saved": saved,
                           "net": float(sum(lr[h] - lp[h] for h in hrs))}
        res[uid] = r

    # tipi postaj: profil neto pretoka (vračila - izposoje) 6-21 h, normaliziran
    uids = list(res)
    X = np.array([[res[u]["lr"][h] - res[u]["lp"][h] for h in range(6, 21)] for u in uids])
    scale = np.maximum(np.abs(X).sum(1, keepdims=True), 1e-9)
    lab, cen = kmeans(X / scale, 4) if len(uids) >= 4 else (np.zeros(len(uids), int), X[:1] / scale[:1])
    names = {}
    for j, c in enumerate(cen):
        m, a = c[1:4].sum(), c[9:12].sum()  # 7-10 in 15-18
        if abs(m) < 0.08 and abs(a) < 0.08:
            names[j] = "uravnotežena"
        elif m < 0 and a > 0:
            names[j] = "jutranji izvor (stanovanjska)"
        elif m > 0 and a < 0:
            names[j] = "jutranji cilj (delo, šole)"
        elif m < 0:
            names[j] = "stalno odtekanje"
        else:
            names[j] = "stalno pritekanje"
    for u, l in zip(uids, lab):
        res[u]["type"] = names[int(l)]

    # predlog dveh con: k-means na koordinatah, utežen s potrebo po premikih
    need = np.array([sum(abs(res[u]["win"][w]["move"]) for w in WINDOWS) + 0.1 for u in uids])
    XY = np.array([[res[u]["lat"], res[u]["lng"] * math.cos(math.radians(res[u]["lat"]))] for u in uids])
    zl = np.zeros(len(uids), int)
    if len(uids) >= 2:
        rnd = np.random.default_rng(3)
        c = XY[rnd.choice(len(uids), 2, replace=False)]
        for _ in range(50):
            zl = np.argmin(((XY[:, None] - c[None]) ** 2).sum(-1), 1)
            c = np.array([np.average(XY[zl == j], axis=0, weights=need[zl == j]) if (zl == j).any() else c[j]
                          for j in range(2)])
        # ime cone po smeri neba
        north = int(c[0, 0] < c[1, 0])
        for u, z in zip(uids, zl):
            res[u]["zone"] = "A" if z == north else "B"
    else:
        for u in uids:
            res[u]["zone"] = "A"

    # opažen razvoz: kolesa po urah (delavniki) in kolesa na postanek
    reb_h = np.zeros(24)
    stops = defaultdict(int)
    for a, b, fu, tu in reb:
        la = local(a)
        if is_workday(la.date()):
            reb_h[la.hour] += 1
            stops[(fu, tu, b // 1200)] += 1
    reb_h /= nwd
    per_stop = (sum(stops.values()) / len(stops)) if stops else 0
    return {"res": res, "days": days, "wdays": wdays, "reb_h": reb_h, "reb_per_stop": per_stop,
            "reb_total_day": sum(stops.values()) / nwd if stops else 0}


# ---------- poročilo ----------

def svg_bars(series, colors, labels, peaks, height=170, unit=""):
    w, pad, top = 720, 34, 12
    bw = (w - pad - 8) / 24
    mx = max(max(sum(s[h] for s in series) for h in range(24)), 1e-9)
    out = [f'<svg viewBox="0 0 {w} {height + 26}" class="chart" role="img">']
    for a, b in peaks:
        out.append(f'<rect x="{pad + a * bw:.1f}" y="{top}" width="{(b - a) * bw:.1f}" height="{height - top}" class="peak"/>')
    for i in range(5):
        y = top + (height - top) * i / 4
        out.append(f'<line x1="{pad}" x2="{w - 8}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{pad - 6}" y="{y + 4:.1f}" class="ax" text-anchor="end">{mx * (4 - i) / 4:.0f}</text>')
    for h in range(24):
        y0 = height
        for s, col, lab in zip(series, colors, labels):
            v = s[h]
            hh = (height - top) * v / mx
            out.append(f'<rect x="{pad + h * bw + 2:.1f}" y="{y0 - hh:.1f}" width="{bw - 4:.1f}" height="{hh:.1f}" '
                       f'fill="{col}" rx="2"><title>{h}:00 · {lab}: {v:.1f}{unit}</title></rect>')
            y0 -= hh
        if h % 2 == 0:
            out.append(f'<text x="{pad + h * bw + bw / 2:.1f}" y="{height + 16}" class="ax" text-anchor="middle">{h}</text>')
    out.append("</svg>")
    leg = "".join(f'<span><i style="background:{c}"></i>{html.escape(l)}</span>' for c, l in zip(colors, labels))
    return "".join(out) + f'<div class="legend">{leg}<span><i class="pk"></i>prometna konica</span></div>'


def fmt(x, d=1):
    return "–" if x is None else f"{x:.{d}f}".replace(".", ",")


def report(A, name, domain, depot, depot_q, t0, t1, cap_vehicle, vehicles):
    res = A["res"]
    R = sorted(res.values(), key=lambda r: -(r["lost_p"] + RETURN_WEIGHT * r["lost_r"]))
    tot_lp = sum(r["lost_p"] for r in R)
    tot_lr = sum(r["lost_r"] for r in R)
    tot_pick = sum(r["pickups_day"] for r in R)
    loss = np.array([r["lost_p"] + RETURN_WEIGHT * r["lost_r"] for r in R])
    cum = np.cumsum(loss) / max(loss.sum(), 1e-9)
    n50 = int(np.searchsorted(cum, 0.5) + 1) if len(R) else 0
    n80 = int(np.searchsorted(cum, 0.8) + 1) if len(R) else 0
    lp_h = sum(r["lost_p_h"] for r in R) if R else np.zeros(24)
    lr_h = sum(r["lost_r_h"] for r in R) if R else np.zeros(24)

    plan = {}
    for w, cfg in WINDOWS.items():
        dl = [r for r in R if r["win"][w]["move"] > 0]
        pu = [r for r in R if r["win"][w]["move"] < 0]
        deliver = sum(r["win"][w]["move"] for r in dl)
        pickup = -sum(r["win"][w]["move"] for r in pu)
        moves = max(deliver, pickup)
        stops = len(dl) + len(pu) + math.ceil(abs(deliver - pickup) / cap_vehicle)  # razlika iz depoja/v depo
        minutes = stops * (STOP_MIN + DRIVE_MIN) + (deliver + pickup) * BIKE_MIN
        zones = defaultdict(lambda: [0, 0])
        for r in dl:
            zones[r["zone"]][0] += r["win"][w]["move"]
        for r in pu:
            zones[r["zone"]][1] -= r["win"][w]["move"]
        plan[w] = {"deliver": deliver, "pickup": pickup, "moves": moves, "loads": math.ceil(moves / cap_vehicle) if moves else 0,
                   "stops": stops, "hours": minutes / 60, "per_vehicle": minutes / 60 / vehicles,
                   "saved": sum(r["win"][w]["saved"] for r in R), "zones": dict(zones), "dl": dl, "pu": pu}

    css = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mut:#6b6b66;--line:#e4e3de;--acc:#0b6e4f;--warn:#c2410c;--blue:#2563eb;--pk:rgba(194,65,12,.08)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--mut:#a3a29b;--line:#33332f;--acc:#34d399;--warn:#fb923c;--blue:#60a5fa;--pk:rgba(251,146,60,.12)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1060px;margin:0 auto;padding:24px 16px 60px}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:34px 0 10px}
.mut{color:var(--mut)}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:12px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.kpi b{display:block;font-size:26px}
.kpi span{color:var(--mut);font-size:13px}table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{color:var(--mut);font-weight:600}
td:first-child,th:first-child,td.l,th.l{text-align:left;white-space:normal}.scroll{overflow-x:auto}
.chart{width:100%;height:auto}.chart .peak{fill:var(--pk)}.chart .grid{stroke:var(--line)}.chart .ax{fill:var(--mut);font-size:11px}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12.5px;color:var(--mut);margin-top:4px}.legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.legend i.pk{background:var(--pk);border:1px solid var(--warn)}#map{height:460px;border-radius:12px}
.pos{color:var(--acc);font-weight:600}.neg{color:var(--warn);font-weight:600}.tag{font-size:12px;padding:1px 7px;border-radius:9px;background:var(--line)}
ul{margin:6px 0;padding-left:20px}li{margin:3px 0}
"""
    o = [f"""<!doctype html><html lang="sl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Diagnoza razvoza {html.escape(name)}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>{css}</style></head><body><main>
<h1>Diagnoza razvoza: {html.escape(name)}</h1>
<div class="mut">Delavniki {local(t0):%-d. %-m.}–{local(t1 - 1):%-d. %-m. %Y} ({len(A['wdays'])} dni) · {len(R)} postaj ·
depo {html.escape(depot_q)} · {vehicles} vozili po {cap_vehicle} koles · interno</div>
<div class="kpis" style="margin-top:16px">
<div class="card kpi"><b>{fmt(tot_lp, 0)}</b><span>ocenjenih izgubljenih izposoj na delavnik (prazne postaje), {fmt(tot_lp / max(tot_pick + tot_lp, 1e-9) * 100)} % povpraševanja</span></div>
<div class="card kpi"><b>{fmt(tot_lr, 0)}</b><span>ocenjenih izgubljenih vračil na delavnik (polne postaje)</span></div>
<div class="card kpi"><b>{n50} / {n80}</b><span>postaj povzroči 50 % / 80 % vseh izgub</span></div>
<div class="card kpi"><b>{fmt(A['reb_total_day'], 0)}</b><span>koles na delavnik premaknjenih z zaznanim razvozom, {fmt(A['reb_per_stop'])} kolesa na postanek</span></div>
</div>
<h2>Kdaj zmanjka koles in prostih mest</h2>
<div class="card">{svg_bars([lp_h, lr_h * RETURN_WEIGHT], ['var(--warn)', 'var(--blue)'],
                         ['izgubljene izposoje', f'izgubljena vračila × {fmt(RETURN_WEIGHT)}'], PEAKS)}
<p class="mut">Povprečje na delavnik. Izguba = prava stopnja povpraševanja × čas, ko je bila postaja prazna (oz. polna).</p></div>
<h2>Kdaj ekipa danes razvaža</h2>
<div class="card">{svg_bars([A['reb_h']], ['var(--acc)'], ['premaknjena kolesa (zaznan razvoz)'], PEAKS)}
<p class="mut">Zaznan je samo premik 3 ali več koles med istima postajama v 20-minutnem oknu. Manjši premiki se štejejo kot vožnje, zato je dejanski razvoz nekoliko večji.</p></div>
"""]
    o.append("<h2>Koliko je treba premakniti pred konicama</h2><div class='scroll'><table><tr><th class='l'>Okno</th>"
             "<th>Pripeljati</th><th>Odpeljati</th><th>Polnih kombijev</th><th>Postankov</th><th>Ur dela skupaj</th>"
             f"<th>Ur na vozilo</th><th>Prihranek izgub / dan</th></tr>")
    for w, cfg in WINDOWS.items():
        p = plan[w]
        o.append(f"<tr><td class='l'>pred {cfg['label']} (stanje ob {cfg['prep'][0]}:{cfg['prep'][1]:02d})</td>"
                 f"<td>{p['deliver']}</td><td>{p['pickup']}</td><td>{p['loads']}</td><td>{p['stops']}</td>"
                 f"<td>{fmt(p['hours'])}</td><td>{fmt(p['per_vehicle'])}</td><td>{fmt(p['saved'])}</td></tr>")
    o.append(f"""</table></div><p class="mut">Ciljni interval za vsako postajo je izračunan iz prave stopnje izposoj in vračil v konici
(rojstno-smrtna veriga po minutah). Premik je razlika med tipičnim stanjem ob uri priprave in najbližjo mejo intervala.
Ocena časa: {fmt(STOP_MIN, 0)} min na postanek, {fmt(DRIVE_MIN, 0)} min vožnje med postanki izven konic, {fmt(BIKE_MIN, 2)} min na kolo.
Razliko med pripeljanimi in odpeljanimi pokrije depo (dodatni postanki v depoju so vključeni).</p>""")
    for w in WINDOWS:
        z = plan[w]["zones"]
        if z:
            o.append(f"<p>Po conah pred {WINDOWS[w]['label']}: " + "; ".join(
                f"cona {k}: pripeljati {v[0]}, odpeljati {v[1]}" for k, v in sorted(z.items())) + "</p>")

    for w, cfg in WINDOWS.items():
        rows = sorted(plan[w]["dl"] + plan[w]["pu"], key=lambda r: -r["win"][w]["saved"])[:25]
        o.append(f"<h2>Seznam nalog pred {cfg['label']}</h2><div class='scroll'><table><tr><th class='l'>Postaja</th><th>Cona</th>"
                 "<th>Stojal</th><th>Tipično zdaj</th><th>Cilj</th><th>Premik</th><th>Prihranek izgub</th><th>Od depoja km</th></tr>")
        for r in rows:
            x = r["win"][w]
            mv = x["move"]
            o.append(f"<tr><td class='l'>{html.escape(r['name'])}</td><td>{r['zone']}</td><td>{r['racks']}</td><td>{fmt(x['now'], 0)}</td>"
                     f"<td>{x['lo']}–{x['hi']}</td><td class='{'pos' if mv > 0 else 'neg'}'>{'+' if mv > 0 else ''}{mv}</td>"
                     f"<td>{fmt(x['saved'])}</td><td>{fmt(r['depot_km'])}</td></tr>")
        o.append("</table></div>")

    o.append("<h2>Najbolj kritične postaje (vse ure, delavniki)</h2><div class='scroll'><table><tr><th class='l'>Postaja</th>"
             "<th class='l'>Tip</th><th>Izposoj / dan</th><th>Prazna min / dan</th><th>Polna min / dan</th>"
             "<th>Izgubljene izposoje</th><th>Izgubljena vračila</th><th>Delež vseh izgub</th></tr>")
    for r, c in list(zip(R, cum))[:30]:
        sh = (r["lost_p"] + RETURN_WEIGHT * r["lost_r"]) / max(loss.sum(), 1e-9) * 100
        o.append(f"<tr><td class='l'>{html.escape(r['name'])}</td><td class='l'><span class='tag'>{r['type']}</span></td>"
                 f"<td>{fmt(r['pickups_day'])}</td><td>{fmt(r['empty_min_day'], 0)}</td><td>{fmt(r['full_min_day'], 0)}</td>"
                 f"<td>{fmt(r['lost_p'])}</td><td>{fmt(r['lost_r'])}</td><td>{fmt(sh)} %</td></tr>")
    o.append("</table></div>")

    types = defaultdict(list)
    for r in R:
        types[r["type"]].append(r)
    o.append("<h2>Tipi postaj</h2><div class='card'><ul>")
    for t, rs in sorted(types.items(), key=lambda x: -len(x[1])):
        o.append(f"<li><b>{t}</b>: {len(rs)} postaj, npr. " + ", ".join(html.escape(r["name"]) for r in
                 sorted(rs, key=lambda r: -r["pickups_day"])[:5]) + "</li>")
    o.append("</ul><p class='mut'>Razvrstitev z gručenjem (k-means) 15-urnih profilov neto pretoka 6–21 h. "
             "Jutranji izvori morajo konico začeti polni, cilji s prostimi mesti.</p></div>")

    pts = [{"n": r["name"], "a": r["lat"], "o": r["lng"], "t": r["type"], "z": r["zone"],
            "l": round(r["lost_p"] + RETURN_WEIGHT * r["lost_r"], 2),
            "m": r["win"]["jutro"]["move"], "p": r["win"]["popoldne"]["move"]} for r in R]
    o.append(f"""<h2>Zemljevid</h2><div id="map"></div>
<p class="mut">Velikost kroga = izgube na delavnik, barva = tip postaje, obroba = cona. Črni kvadrat = depo.</p>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
const P={json.dumps(pts, ensure_ascii=False)}, D={json.dumps(depot)};
const col={{"jutranji izvor (stanovanjska)":"#2563eb","jutranji cilj (delo, šole)":"#c2410c","uravnotežena":"#9ca3af","stalno odtekanje":"#7c3aed","stalno pritekanje":"#0b6e4f"}};
const m=L.map('map');L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{attribution:'© OpenStreetMap'}}).addTo(m);
const b=[];P.forEach(p=>{{b.push([p.a,p.o]);L.circleMarker([p.a,p.o],{{radius:4+Math.sqrt(p.l)*4,color:p.z==='A'?'#111':'#fff',weight:2,fillColor:col[p.t]||'#999',fillOpacity:.85}})
.bindPopup(`<b>${{p.n}}</b><br>${{p.t}} · cona ${{p.z}}<br>izgube/dan: ${{p.l}}<br>premik pred jutrom: ${{p.m}}, pred popoldnevom: ${{p.p}}`).addTo(m)}});
if(D){{L.marker(D,{{icon:L.divIcon({{html:'<div style="width:14px;height:14px;background:#111;border:2px solid #fff"></div>',className:''}})}}).bindPopup('Depo').addTo(m);b.push(D)}}
if(b.length)m.fitBounds(b,{{padding:[20,20]}});
</script>""")
    o.append(f"""<h2>Opombe</h2><div class="card"><ul>
<li>Vožnje so sklepane iz premikov koles na javnem API-ju. Prave izposoje iz zaledja nextbike bi oceno izboljšale.</li>
<li>Upoštevani so samo delavniki brez praznikov. Po {len(A['wdays'])} dneh so ocene za manj prometne postaje še grobe.</li>
<li>Razdalje od depoja so zračne × 1,3. Za pravi načrt poti potrebujemo matriko časov vožnje (OSRM) in OR-Tools.</li>
</ul></div></main></body></html>""")
    return "".join(o), R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="cc")
    ap.add_argument("--od", default="2026-10-01")
    ap.add_argument("--do", default=None, help="do vključno (privzeto včeraj)")
    ap.add_argument("--depo", default="Središka ulica 4, Ljubljana")
    ap.add_argument("--depo-lat", type=float)
    ap.add_argument("--depo-lng", type=float)
    ap.add_argument("--kapaciteta", type=int, default=10)
    ap.add_argument("--vozil", type=int, default=2)
    a = ap.parse_args()
    t0 = int(datetime.fromisoformat(a.od).replace(tzinfo=TZ).timestamp())
    end = date.fromisoformat(a.do) if a.do else datetime.now(TZ).date() - timedelta(days=1)
    t1 = int(datetime(end.year, end.month, end.day, tzinfo=TZ).timestamp()) + 86400
    depot = [a.depo_lat, a.depo_lng] if a.depo_lat is not None else geocode(a.depo)
    depot = list(depot) if depot else None
    name = next((s["name"] for s in C.SYSTEMS if a.domain in s["domains"]), a.domain)
    con = C.connect()
    st, hist, trips = load(con, a.domain, t0, t1)
    if not st:
        raise SystemExit(f"Ni postaj za domeno {a.domain}.")
    A = analyse(st, hist, trips, t0, t1, depot)
    page, R = report(A, name, a.domain, depot, a.depo + ("" if depot else " (ni najden)"), t0, t1, a.kapaciteta, a.vozil)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"diagnoza-{a.domain}.html"), "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(OUT, f"postaje-{a.domain}.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["postaja", "tip", "cona", "stojal", "izposoj_dan", "vracil_dan", "prazna_min_dan", "polna_min_dan",
                    "izgub_izposoj_dan", "izgub_vracil_dan", "jutro_zdaj", "jutro_cilj_od", "jutro_cilj_do", "jutro_premik",
                    "popoldne_zdaj", "popoldne_cilj_od", "popoldne_cilj_do", "popoldne_premik", "depo_km"]
                   + [f"izposoje_{h}" for h in range(24)] + [f"vracila_{h}" for h in range(24)])
        for r in R:
            j, p = r["win"]["jutro"], r["win"]["popoldne"]
            w.writerow([r["name"], r["type"], r["zone"], r["racks"], round(r["pickups_day"], 2), round(r["returns_day"], 2),
                        round(r["empty_min_day"]), round(r["full_min_day"]), round(r["lost_p"], 2), round(r["lost_r"], 2),
                        j["now"], j["lo"], j["hi"], j["move"], p["now"], p["lo"], p["hi"], p["move"],
                        None if r["depot_km"] is None else round(r["depot_km"], 2)]
                       + [round(x, 3) for x in r["lp"]] + [round(x, 3) for x in r["lr"]])
    print(f"{name}: {len(R)} postaj, {len(A['wdays'])} delavnikov -> {OUT}/diagnoza-{a.domain}.html")


if __name__ == "__main__":
    main()
