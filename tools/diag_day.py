"""Diagnostika: vse zaznane vožnje za domeno in dan (izpis v povzetek GitHub Actions).

  DATABASE_URL=... python tools/diag_day.py cx 2026-10-25
"""
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common as C  # noqa: E402

TZ = ZoneInfo(C.TZ)
dom, day = sys.argv[1], sys.argv[2]
d0 = datetime.fromisoformat(day).replace(tzinfo=TZ)
a, b = int(d0.timestamp()), int((d0 + timedelta(days=1)).timestamp())
con = C.connect()
names = {r[0]: r[1] for r in con.execute("SELECT uid, name FROM stations")}
rows = con.execute("""SELECT bike, from_uid, to_uid, start_ts, end_ts, dur_s, km, kind FROM trips
                      WHERE domain = ? AND start_ts >= ? AND start_ts < ? ORDER BY start_ts""", (dom, a, b)).fetchall()
hm = lambda ts: datetime.fromtimestamp(ts, TZ).strftime("%H:%M")
nm = lambda u: names.get(u, "prosto parkirano") if u is not None else "prosto parkirano"
out = [f"## Vožnje {dom} {day}", "", f"Skupaj zapisov: {len(rows)}", ""]
out += [f"- {k}: {n}" for k, n in Counter(r[7] for r in rows).most_common()]
trips = [r for r in rows if r[7] == "trip"]
out += ["", "### Vrste voženj (kind=trip)", ""]
cat = Counter()
for r in trips:
    if r[1] is None or r[2] is None:
        cat["prosto parkirano na enem koncu"] += 1
    elif r[1] == r[2]:
        cat["vrnitev na isto postajo"] += 1
    else:
        cat["med različnima postajama"] += 1
out += [f"- {k}: {n}" for k, n in cat.most_common()]
out += ["", "### Trajanje (kind=trip)", ""]
bins = Counter("<3 min" if r[5] < 180 else "3-10 min" if r[5] < 600 else "10-30 min" if r[5] < 1800 else ">30 min" for r in trips)
out += [f"- {k}: {n}" for k, n in bins.items()]
out += ["", "### Kolesa z največ vožnjami", ""]
out += [f"- {bk}: {n}" for bk, n in Counter(r[0] for r in trips).most_common(10)]
out += ["", "### Vse vožnje", "", "| od | do | min | km | kolo | iz | v | vrsta |", "|---|---|---|---|---|---|---|---|"]
for bk, fu, tu, s, e, du, km, kind in rows:
    out.append(f"| {hm(s)} | {hm(e)} | {du / 60:.1f} | {km:.2f} | {bk} | {nm(fu)} | {nm(tu)} | {kind} |")
# stanje koles ob koncu dneva: ali kolesa skačejo med postajo in prosto parkiranim položajem
text = "\n".join(out)
print(text)
summ = os.environ.get("GITHUB_STEP_SUMMARY")
if summ:
    with open(summ, "a", encoding="utf-8") as f:
        f.write(text + "\n")
