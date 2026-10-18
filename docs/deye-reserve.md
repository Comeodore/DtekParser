# deye-reserve: what it is for and how it works

State as of 19.10.2026. The code is in `deye_reserve/`, tests in
`tests/test_reserve_*.py`; dtek-service's side is `app/reserve_hook.py`,
`app/api.py` (`/api/v1/inverter/latest`), `app/collector.py` and
`app/inverter/deye.py` (webhooks).

## Goal

Keep the house in Круглик powered through DTEK outages at the lowest
grid cost, without anyone touching the inverter. deye-reserve works out
the Deye inverter's Time of Use table (six slots: start time, target SOC,
grid charge on/off) from the DTEK schedule, the sun forecast and the
house's load, and writes it into the inverter.

In order of priority:

1. **Safety.** Never run the battery to shutdown during a scheduled
   outage; keep a reserve for unscheduled ones; tell the owner when the
   battery will not last.
2. **Cost.** Buy at the night tariff rather than the day one, use the
   sun rather than throw it away (zero export cuts PV once the battery
   is full).
3. **Quiet.** Few writes, and the owner hears only what matters.

## The system

| | |
|---|---|
| Inverter | Deye single-phase hybrid 6 kW AC, SN 2511090098, productId 0_5407_1, max PV input 7.8 kW; Deye station 61918417 |
| Battery | 2 × Deye SE-G5.1 Pro-B = 10.24 kWh; shutdown 20%; Max A Charge 100 A, Grid Charge 50 A (the owner keeps 50 A: the grid breaker is unknown) |
| Panels | 11 × 615 W = 6.77 kWp, south roof slope (ridge runs west–east), tilt ≈ 30° |
| Site | 50.3084, 30.4067 (Круглик, Kyiv region), DTEK queue GPV2.2 (krem) |
| Wiring | the whole house, router included, is on the Load port; the logger keeps reporting through outages |
| Grid | zero export, no green (feed-in) tariff yet; while the battery carries the house the inverter still imports ~39 W (zero-export power), ~1.2 UAH/day |
| Tariff | 4.32 UAH/kWh day, 2.16 UAH/kWh night (23:00–07:00) |
| Load | 9.1 kWh/day in October (7.5 in September), no weekday pattern; mornings can reach ~900 W for hours (16–17.10) |

Measured on this battery (Deye history 09–10.2026): the load gets 9.75 kWh
per 100% of SOC; filling 100% takes 11.75 kWh at the battery, i.e. 12.3 kWh
from the grid or 11.2 kWh of PV surplus. Grid charging runs at ~2.6 kW into
the battery (2.9 kW drawn with the house). Until 18.10 Max A Charge was 50 A
and PV charging stopped at ~2.7 kW whatever the SOC.

## Inputs

- **DTEK schedule**: dtek-service `GET 127.0.0.1:9998/api/v1/schedule`
  (krem). Today and tomorrow; tomorrow without outages counts as not
  published; "possible" outages count as outages. dtek-service posts
  `/events/schedule` when krem's schedule data changes and `/events/grid`
  when the inverter loses or regains the grid.
- **Live SOC, grid and load**: dtek-service `GET /api/v1/inverter/latest`
  (it polls Deye `device/latest` every ~72 s; deye-reserve does not poll
  Deye for readings). Read at each replan and every 3 min for the battery
  watch.
- **Sun**: Open-Meteo **irradiance on the panels' plane**
  (`global_tilted_irradiance`, tilt 30, azimuth 0 = south) from seven models
  (ECMWF, UKMO, GEM, ICON, GFS, Météo-France, CMA) plus the 51-run ECMWF
  ensemble. PV W = 4.4 × GTI (what 5.35 × horizontal was in September–
  October; the plane follows the low winter sun: on clear December days the
  roof gets ~1.8× the horizontal). P50 = median of the top-3 models,
  pessimistic = min(ECMWF ensemble P10, median of the top-3). Until 19.10
  P50 was mean(ECMWF, median); against the panels on 09–18.10 (days without
  a full battery) ECMWF read more sun than came, and the median of the top
  3 alone was off by 0.83 kWh a day against 1.34 (bias −0.2 against +0.55).
  The
  top 3 are ranked once a day by how well their day-ahead forecasts
  (previous runs, on the same plane) matched the panels over 14 days, on
  hours with the battery under 95%.
- **Solcast** rooftop site `d6c1-af45-2be5-4e98` (south, 30°, DC 6.6,
  AC 6): **watched only**, six calls a day, compared with the panels in the
  morning review.
- **Load**: Deye `station/history/power`, one call a day (14 days): hourly
  mean and P80 over the last 7 days.

## The planning model

- **Window**: from now to 23:00 of the "tariff day" whose night is coming
  or under way (up to ~40 h), stretched to the end of any outage that
  starts inside it. Today and tomorrow are planned **together**.
- **A table per day.** Each date in the window gets its own table: the
  night target at 00:00 and 23:00, the day floor, and that day's own
  just-in-time charges. The simulation switches tables at midnight. The
  inverter holds one table: today's, and tomorrow's from the moment it
  does the same as today's for the rest of the day (23:00 when the night
  slot starts, earlier when tomorrow's charges lie before an hour already
  past today). A charge for tomorrow therefore never runs today.
- **Simulation**, 5-min steps, with the measured battery: with the grid
  present the battery is not discharged below the slot's SOC and, with
  grid charge on, is charged up to it at 2.7 kW from the grid (0.83 into
  SOC); PV first covers the load, then charges the battery (0.91, up to
  5 kW); what does not fit is lost; out of the battery 0.95. During an
  outage it runs down to 20%.
- **Two scenarios**: expected (mean load, P50 sun) and pessimistic
  (max(mean, P80) × 1.1 load, pessimistic sun). The pessimistic one must
  keep ≥25% SOC through every scheduled outage that has not started yet.
  A candidate's **price** is 0.7 × expected + 0.3 × pessimistic cost
  (energy left at the end valued at the night tariff), so a plan that only
  pays off when everything goes right does not win: the battery stays
  fuller by day and the day tariff is bought less.
- **Night target**: tried from 45 to 95% in steps of 5; the cheapest safe
  one wins, a higher target has to be cheaper by >0.3 UAH. 100% when
  nothing lower is safe, and once a week for SOC calibration.
- **Day floor**: 45% while the grid is there, the reserve for unscheduled
  outages.
- **Just-in-time charge**: an outage the night charge cannot cover gets a
  grid-charge slot right before it, in its own day's table: start = outage
  start − missing energy / 2.7 kW − 30 min, on a 15-min grid.
- **Six slots** per table: extra breakpoints are dropped where that costs
  least, missing ones are added by splitting the longest stretch.
- **Stickiness**: the previous tables stay unless the new ones are cheaper
  by more than 0.5 UAH.
- **Fallback**: no schedule, no forecast or no fresh reading keeps the last
  plan's tables for up to 6 h, then the old 90/45 table.

## Writing (DEYE_RESERVE_WRITE=true since 17.10 18:11)

- `POST /order/sys/tou/update` with `timeoutSeconds` 120; then
  `GET /order/{id}` until it is final. **Status 666 means written.** The
  table is not read back from `/config/tou`.
- A failed write (any error, or no answer within 150 s) is tried again
  5 min later, up to 3 more times; then the owner gets the table and what
  failed. The next replan at least 30 min later tries again.
- At most 6 tables a day, one command at a time. Writing does not wait
  for the grid or the end of an outage window, only for a fresh reading.
- A table is sent again only when the plan changes, including tomorrow's
  table at its switch time. A manual change in the Deye app stays until
  then.

## Messages (Telegram, the krem bot, owner's chat 370921204 only)

- **A day's table**, as a code block in Ukrainian, when the day's DTEK
  schedule is first published, or changes so that the day's table
  changes. A DTEK change that leaves the table as it was is quiet (on
  18.10 that would have been 2 messages instead of 8). Today and tomorrow
  can come in one message; tomorrow's says "⏭ Застосую о HH:MM" while it
  is not in the inverter yet. No line when it is in the inverter; "⏸"
  when blocked, "🚨" when failed, plus a separate alarm when a write fails
  with no schedule news.
- **The battery during an outage** ("🪫 Батареї може не вистачити до
  світла"): every 3 min the reading is looked at; without the grid, if at
  the house's load (mean of the last 30 min, minus PV) the battery reaches
  20% before the scheduled end + 30 min (an outage not in the schedule:
  2 h from now), the owner hears it once per outage, and again if DTEK
  moves the end. On 07–18.10 this fires exactly once: 16.10 09:05, 30% at
  887 W, empty ~10:10, outage until 11:30.

## When it replans

- 2.5 min after a webhook; later events push it back, but never past
  5 min after the first. An hourly replan waits for a batch that is
  still settling.
- When the 10-min fallback poll of the schedule sees a new DTEK stamp.
- Every hour, after each forecast refresh (05:30, 22:00), and at the time
  tomorrow's table takes over.
- Load history and the model ranking refresh at 00:20; Solcast at 05:34,
  08:34, 10:34, 12:34, 14:34 and 22:04.

## Observability

- `reserve-data/journal.jsonl`: every replan (inputs, candidates and their
  prices, expected and pessimistic results, the table now and every day's
  table, when tomorrow's takes over, write outcome), write, message and
  battery alert.
- `reserve-data/solcast.jsonl`: every Solcast forecast, hourly.
- `GET 127.0.0.1:9997/plan` and `/health`.
- `docker exec deye-reserve python -m deye_reserve.review [END]` lines a
  day's decisions up with Deye's history (SOC, grid bought by tariff, SOC
  in each outage), Solcast and the models against the panels, and the
  inverter's table.

## What the data showed (review of 18–19.10)

Closed-loop replays: a planner drives an emulated inverter (the measured
battery above) through real days, with the DTEK schedule versions as
published (Loki), Open-Meteo day-ahead forecasts as they were (previous
runs) and the 7 days of load before; PV potential in clipped hours from
the satellite. Scripts were kept out of the repo.

| 07–18.10, real | UAH/day | min SOC in outages | writes (12 days) |
|---|---|---|---|
| old 90/45 table | 14.05 | 35.9% | 0 |
| service before 19.10 | 9.59 | 35.7% | 9 |
| service from 19.10 | 9.43 | 35.9% | 8 |
| perfect knowledge of load, sun and outages | 9.01 | 35.0% | 43 |

- **Cost in autumn is near its floor**: ~0.4 UAH/day left. A perfect sun
  forecast would save only 0.06 UAH/day: on sunny days the battery fills
  anyway. A December-like stress (ERA5 sun, October load × 1.15, ~10 h of
  outages a day with extensions): 21.28 against 21.48 before (90/45:
  20.79, but with more load left unserved). Load × 2.5 around every
  outage: 22.14 against 23.08.
- **Outages outside the schedule are the main risk**: of 69 h without
  the grid on 07–18.10, 16.8 h (24%) were not in the schedule known an
  hour earlier — 08.10 18:00–21:00 (3 h early), 09.10 22:20–00:45,
  16.10 00:00–04:05 (4 h longer), 16.10 11:30–12:30, 07.10 19:20–20:55,
  and many 10–45 min overruns. No affordable margin in the table covers
  +3–4 h; the battery alert is the answer for those.
- **Load**: the 7-day hourly mean is as good as an exponentially weighted
  14 days (MAE 143 vs 142 W) and better than the same weekday (161).
  max(mean, P80) × 1.1 is exceeded in ~10% of 4-hour windows (P99: 2.8×).
  The last 2 hours predict the next 4 (correlation 0.5).
- **Sun**: on clean hours (SOC < 95%, below the charge limit) the
  day-ahead error of the P50 mix was 196 W on the horizontal, 172 W on
  the panels' plane; ECMWF ifs025 runs high (+97 W, its radiation is
  3-hourly). Clean hours are few and mostly mornings, so the panels'
  factor is only known to 4.3–5.5 W per W/m².
- **Writes**: all 540s (inverter did not answer) were with a 30 s
  timeout; with 120 s too few writes yet to judge.
- Tried and not taken: holding the night's charge until a day outage
  instead of a just-in-time charge (≈0 in replays), night targets up to
  100% (≈0), a terminal value of 0.86 (≈0), a hard +2–3 h extension
  margin (+0.7–1.4 UAH/day in winter, ~3 writes a day, 10–20% less
  unserved), a 35% day floor (−0.7 UAH/day but 26% minimum in outages),
  the live load in the plan (+0.36 UAH/day, −13% unserved under heavy
  load) and a 30% outage floor (≈0 now, +0.4 UAH/day in winter) — the
  last two are options to revisit before winter.

## Open work

1. The live load (last 2 h) in the pessimistic case for the next 4 h, and
   a 30% outage floor: options for the winter.
2. Tomorrow not yet published by 23:00 (DTEK sometimes publishes after
   midnight or at 06:30 for 07:00): the night target assumes no outages.
3. Solcast: decide on 25.10 from the morning reviews; its value for cost
   is small (see above).
4. The green tariff, when it comes: export instead of curtailment changes
   the economics of the night target.

## Operations

- **Server**: `root@192.168.0.185 -p 2222`, `/opt/dtek-service`, not
  git. Deploy = rsync the changed files plus
  `docker compose up -d --build reserve`, after a copy to
  `rollback/code-<date>`. Use short or detached SSH commands: sessions
  drop when the laptop sleeps.
- **Secrets** are in `/opt/dtek-service/.env`: DEYE_* (shared with
  dtek-service), BOT_SCHEDULEBOT_TOKEN, SOLCAST_API_KEY. Never print
  them. Settings are DEYE_RESERVE_* (see `.env.example`).
- **Loki** is at 127.0.0.1:3100 (containers `dtek-service`,
  `deye-reserve`); the krem collector logs every schedule version.
- **Tests**: no venv in the repo; install `requirements-dev.txt` into a
  temporary venv, then run `pytest`. Fixtures are real Deye history
  09–17.10 and Open-Meteo forecasts (horizontal of 17.10, tilted of 19.10).
- **Deye quota**: 100k calls a month for the app ID; dtek-service uses
  ~47–50k, deye-reserve ~1k. **Solcast**: 10 calls a day, the service
  uses ≤8.
