# Nomago Bikes v živo

Javna statistična stran za sisteme Nomago Bikes, po vzoru [bajs.informacija.hr](https://bajs.informacija.hr). Vse teče na brezplačnih storitvah.

| Sistem | nextbike domena | Občine |
|---|---|---|
| KOLESCE | `cn` | Braslovče, Celje, Laško, Polzela, Šentjur, Sevnica, Slovenske Konjice, Štore, Vojnik, Žalec, Zreče |
| BICIKEL | `cc` | Dobrova - Polhov Gradec, Komenda, Ljubljana, Medvode, Mengeš, Škofljica, Trzin |
| GO2GO | `ce` + `cd` | Nova Gorica, Šempeter - Vrtojba + Gorizia (en čezmejni sistem, stran šteje tudi vožnje čez mejo) |
| ZANAPREJ | `cf` | Zagorje ob Savi |
| KOROBAJK | `cx` | Prevalje, Ravne na Koroškem |

Imena in združevanje domen so v `SYSTEMS` v `common.py`. Domene, ki jih Supabase zbira, so v tabeli `nb.config` (nova domena = nova migracija, glej `…_nb_add_korobajk.sql`).

```
nextbike-live.json
      │  vsaki 2 min (pg_cron + pg_net)
      ▼
Supabase Postgres ── nb.ingest(): stanje postaj + sklepanje voženj
      │                                    │
      │ vsakih 15 min (GitHub Actions)     │ vsako minuto (REST, javni pogledi nb_live_*)
      ▼                                    ▼
builder.py ──► site/data/*.json ──► GitHub Pages (site/index.html)
```

**Kako štejemo vožnje.** Ko kolo (`bike_numbers`) izgine s postaje A in se pojavi na B, je to vožnja. Premik 3 ali več koles hkrati med istima postajama je prerazporeditev, odsotnost nad 6 ur je servis, A→A krajše od 3 min je ponovni priklop. Nič od tega ne šteje kot vožnja. Na 30 simuliranih dneh je bilo pravilno zaznanih 99 % voženj.

## Postavitev (približno 15 minut)

### 1. Supabase (baza in zbiranje)

1. Na [supabase.com](https://supabase.com) ustvari brezplačen projekt (regija Frankfurt ali Zürich).
2. Shemo namesti na enega od dveh načinov:
   - **GitHub integracija** (priporočeno): Project Settings → Integrations → GitHub → poveži ta repozitorij, *Supabase directory* `supabase`, vklopi **Deploy to production** (veja `main`). Migracije iz `supabase/migrations/` se namestijo ob vsakem pushu na `main`.
   - **Ročno**: v **SQL Editor** po vrsti zaženi obe datoteki iz `supabase/migrations/`.

   Če namestitev javi napako pri `create extension`: **Database → Extensions** → vklopi `pg_cron` in `pg_net`, nato ponovi.
3. Preveri čez 5 minut v SQL Editorju:
   ```sql
   select count(*) from nb.stations;                    -- > 0
   select * from nb.runs order by ts desc limit 5;      -- ok = true
   ```

Zbiranje zdaj teče samo od sebe vsaki 2 minuti. Spremembe baze dodajaj kot nove datoteke v `supabase/migrations/` (obstoječih ne spreminjaj, ker se ne izvedejo ponovno).

### 2. GitHub (izračun in stran)

1. **Settings → Pages → Source: GitHub Actions**.
2. **Settings → Secrets and variables → Actions**:
   - *Secrets* → `DATABASE_URL`: v Supabase **Connect** → **Session pooler** (IPv4) → URI z vpisanim geslom baze.
     Ne uporabi »Direct connection«, ker je samo IPv6 in ga GitHub Actions ne dosežejo.
   - *Variables* → `SUPABASE_URL` (npr. `https://abcd.supabase.co`) in `SUPABASE_ANON_KEY` (Settings → API → anon/publishable key). Z njima stran vsako minuto osveži stanje postaj.
3. **Actions → Statistika in objava strani → Run workflow**.

Stran je na `https://petzni.github.io/nomago-bikes-live/`. Brez `DATABASE_URL` se stran ne objavi.

### Kaj je javno

Tabele v shemi `nb` niso dostopne prek Supabase API-ja. Javna sta le pogleda `nb_live_stations` (stanje postaj) in `nb_live_fleet` (število koles na voljo/v vožnji), samo za branje. Vožnje s številkami koles so dostopne le z `DATABASE_URL`. Na strani so samo agregati, relacije z manj kot 3 vožnjami pa so skrite.

## Obdobja za nazaj

Pod zavihki je izbira obdobja: Danes (v živo), Včeraj, Zadnjih 7 dni, Prejšnji teden, Ta mesec, Prejšnji mesec ali Po meri (od–do). Obdobje lahko omejiš na dneve v tednu (npr. samo delavnike), s klikom na stolpec v grafu »Vožnje po dnevih« pa dodajaš ali odstranjuješ posamezne dneve. Spremembe so prikazane glede na enako dolgo obdobje tik pred izbranim.

Pogled se izračuna v brskalniku iz dnevnih podatkov po postajah (`data/<sistem>-hist.json` + ena datoteka na mesec). Deluje skupaj z izbiro občin in partnerjev. Čas brez koles in jutranje luknje za dneve, starejše od 60 dni, builder hrani v tabeli `nb.station_daily` (flota v `nb.system_daily`); vožnje ostanejo v `nb.trips` neomejeno. Izbirati je mogoče do 400 dni nazaj (`HIST_MAX_DAYS`).

## Tokovi koles (animacija)

Razdelek »Tokovi koles« za izbrani sistem, občine in obdobje (največ 31 dni) pokaže animacijo vseh voženj: vsako kolo je pika, ki potuje od postaje do postaje in za seboj pušča kratko bledečo sled, zato pogoste relacije same narišejo tokove. Podatki so v `data/<sistem>-flow-<mesec>.json` (od, do, dan, ura, število voženj, povprečno trajanje). Ura odhoda je znana na uro natančno, zato je gibanje znotraj ure razporejeno enakomerno. Prikazane so le relacije z vsaj 3 vožnjami v celotnem obdobju zbiranja.

## Izbira občin

Pri sistemih z več občinami (KOLESCE, BICIKEL, GO2GO) lahko pod zavihki izbereš eno ali več občin. Pogled se izračuna v brskalniku iz agregatov po postajah (`cube` v JSON datoteki sistema). Vožnja šteje v občini, kjer se začne. Kolesa v vožnji in flota so vezani na celoten sistem, zato jih pri izbiri občin ni.

## Interne analize po partnerjih

Postaje sistema BICIKEL so razdeljene po partnerjih v `partners/bicikel.csv` (`postaja;partner`). Interni pogled odpreš z naslovom
`https://petzni.github.io/nomago-bikes-live/#interno` (ali neposredno npr. `#interno-bicikel-btc`). Pod zavihki se pojavi izbira partnerja, sistem pa dobi tabelo »Primerjava partnerjev«.

Postaje, ki jih ni v tabeli, so v skupini »Brez partnerja«. Ime postaje se mora ujemati z imenom v nextbike (velike/male črke, presledki in pomišljaji niso pomembni). Za drug sistem dodaj datoteko in vnos v `PARTNER_FILES` v `common.py`.

Interni pogled ni zaščiten: povezava ni objavljena, a JSON podatki so dostopni vsakomur, ki pozna naslov.

## Razvoj lokalno (brez Supabase)

```bash
rm -f data/demo.sqlite
TZ=Europe/Ljubljana NB_DB=data/demo.sqlite python3 simulate.py --days 30
TZ=Europe/Ljubljana NB_DB=data/demo.sqlite NB_DEMO=1 NB_WEATHER=0 python3 builder.py
cd site && python3 -m http.server 8000     # http://localhost:8000
```

Z nastavljenim `DATABASE_URL` vsi skripti (`collector.py`, `builder.py`, `simulate.py`) delajo s Postgresom namesto s SQLite. Na lastnem strežniku lahko vse teče tudi brez Supabase: `run.sh` vsaki 2 minuti iz crona (glej `deploy/`).

## Datoteke

| Datoteka | Kaj dela |
|---|---|
| `supabase/migrations/…_nb_schema.sql` | tabele in funkcija `nb.ingest()` (sklepanje voženj v SQL) |
| `supabase/migrations/…_nb_collect_and_api.sql` | pg_cron opravilo vsaki 2 min, čiščenje starih podatkov, javni pogledi |
| `builder.py` | statistike → `site/data/*.json` (SQLite ali Postgres) |
| `collector.py` | ista logika v Pythonu za SQLite/lastni strežnik |
| `simulate.py` | simulirani podatki v obliki nextbike-live.json za razvoj in test |
| `common.py` | nastavitve (pragovi, CO₂ na km, faktor obvoza) in povezava z bazo |
| `site/index.html` | stran (ena datoteka, brez build koraka; zemljevid: Leaflet + OpenStreetMap) |
| `.github/workflows/pages.yml` | izračun vsakih 15 min in objava na GitHub Pages |

## Omejitve

- **Kilometri so ocena** (zračna razdalja × 1,3). Kot operater imate v nextbike zaledju prave podatke o izposojah. Ko jih uvozite v `nb.trips`, ostane preostali del enak.
- **»V vožnji«** vključuje tudi kolesa na kombiju med razvozom.
- **GitHub Actions cron** ni točen na minuto, zato se statistika včasih osveži z nekaj minutami zamude. Živo stanje postaj iz Supabase se osveži ne glede na to.
- **Brezplačni Supabase projekt** se po 7 dneh brez aktivnosti zaustavi. Redni klici strani in GitHub Actions bi to morali preprečiti, vseeno pa ga po prvem tednu preveri.
- **Prostor:** baza naraste za približno 1–2 MB na dan, starejši podrobni podatki se po 60 dneh brišejo. Brezplačnih 500 MB zadošča za več let.
