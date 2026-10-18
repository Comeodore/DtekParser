# deye-reserve: what it is for and how it works

State as of 18.10.2026. The code is in `deye_reserve/`, tests in
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
   outage; keep a reserve for unscheduled ones.
2. **Cost.** Buy at the night tariff rather than the day one, use the
   sun rather than throw it away (zero export cuts PV once the battery
   is full).
3. **Quiet.** Few writes, and the owner hears only what matters.

## The system

| | |
|---|---|
| Inverter | Deye single-phase hybrid 6 kW AC, SN 2511090098, productId 0_5407_1, max PV input 7.8 kW; Deye station 61918417 |
| Battery | 2 × Deye SE-G5.1 Pro-B = 10.24 kWh; shutdown 20% |
| Panels | 11 × 615 W = 6.77 kWp, south roof slope (ridge runs west–east), tilt ≈ 30° |
| Site | 50.3084, 30.4067 (Круглик, Kyiv region), DTEK queue GPV2.2 (krem) |
| Wiring | the whole house, router included, is on the Load port; the logger keeps reporting through outages |
| Grid | zero export, no green (feed-in) tariff yet |
| Tariff | 4.32 UAH/kWh day, 2.16 UAH/kWh night (23:00–07:00) |
| Load | ~9 kWh/day; mornings can reach ~900 W for hours (16–17.10) |

## Inputs

- **DTEK schedule**: dtek-service `GET 127.0.0.1:9998/api/v1/schedule`
  (krem). Today and tomorrow; tomorrow without outages counts as not
  published. dtek-service posts `/events/schedule` when krem's schedule
  data changes and `/events/grid` when the inverter loses or regains the
  grid.
- **Live SOC and grid**: dtek-service `GET /api/v1/inverter/latest`
  (it already polls Deye `device/latest` every ~72 s; deye-reserve does
  not poll Deye for readings).
- **Sun**: Open-Meteo, seven models (ECMWF, UKMO, GEM, ICON, GFS,
  Météo-France, CMA) plus the 51-run ECMWF ensemble.
  P50 = mean(ECMWF, median of the top-3 models), pessimistic = min(ECMWF
  ensemble P10, median of the top-3). The top 3 are ranked once a day by
  how well their day-ahead forecasts (previous runs) matched the panels
  over 14 days, on hours with the battery under 95% (zero export cuts
  PV above that). PV W = 5.35 × GHI (horizontal; calibrated on these
  panels, not yet using tilt/azimuth).
- **Solcast** rooftop site `d6c1-af45-2be5-4e98` (south, 30°, DC 6.6,
  AC 6): **watched only**. Six calls a day, logged hour by hour with
  P10/P50/P90, compared with the panels in the morning review. It is
  not used for planning.
- **Load**: Deye `station/history/power`, one call a day (14 days): hourly
  mean and P80 over the last 7 days.

## The planning model

- **Window**: from now to 23:00 of the "tariff day" whose night is coming
  or under way (up to ~40 h), stretched to the end of any outage that
  starts inside it.
- **Simulation**, 5-min steps: with the grid present the battery is not
  discharged below the slot's SOC and, with grid charge on, is charged
  up to it at 2.7 kW; during an outage it runs down to 20%. PV first
  covers the load, then charges the battery; what does not fit is lost.
  One-way efficiency 0.95.
- **Two scenarios**: expected (mean load, P50 sun) prices a candidate;
  pessimistic (max(mean, P80) × 1.1 load, pessimistic sun) must keep
  ≥25% SOC through every scheduled outage that has not started yet.
  Energy left at the end is valued at the night tariff.
- **Night target**: tried from 45 to 95% in steps of 5; the cheapest safe
  one wins, a higher target has to be cheaper by >0.3 UAH. 100% when
  nothing lower is safe, and once a week for SOC calibration (last full
  charge from history or live readings; next due on the night to 21.10).
- **Day floor**: 45% while the grid is there, the reserve for unscheduled
  outages.
- **Just-in-time charge**: an outage the night charge cannot cover gets a
  grid-charge slot right before it: start = outage start − missing
  energy / 2.7 kW − 30 min, on a 15-min grid.
- **Six slots**: the table starts at 00:00 with increasing times; extra
  breakpoints are dropped where that costs least, missing ones are added
  by splitting the longest stretch.
- **Stickiness**: the previous table stays unless the new one is cheaper
  by more than 0.5 UAH (fewer writes and messages).
- **Fallback**: no schedule, no forecast or no fresh reading means the
  old 90/45 table.

## Writing (DEYE_RESERVE_WRITE=true since 17.10 18:11)

- `POST /order/sys/tou/update` with `timeoutSeconds` 120; then
  `GET /order/{id}` until it is final. **Status 666 means written.** The
  table is not read back from `/config/tou`; it shows the new table only
  minutes later (13 min on 17.10).
- A failed write (any error, or no answer within 150 s) is tried again
  5 min later, up to 3 more times. With 30 s, three orders on 18.10 came
  back 500/540 ("not answered in time"); with 120 s the same table was
  done in 5 s.
- If all four tries fail, the owner gets the table and what failed. The
  next replan at least 30 min later tries again.
- At most 6 tables a day, one command at a time. Writing does not wait
  for the grid or the end of an outage window, only for a fresh reading
  (logger online).
- A table is sent again only when the plan changes. A manual change in
  the Deye app stays until then.

## Messages (Telegram, the krem bot, owner's chat 370921204 only)

- Only for a day whose DTEK schedule was published or changed. Several a
  day are fine when DTEK changes the schedule several times.
- Content: the day's final TOU table as a code block, in Ukrainian. No
  line when it is in the inverter. A line only when it is not ("⏸" for
  blocked, "🚨" for failed), plus a separate alarm when a write fails
  with no schedule news.

## When it replans

- 2.5 min after a webhook; later events push it back, but never past
  5 min after the first. An hourly replan waits for a batch that is
  still settling.
- When the 10-min fallback poll of the schedule sees a new DTEK stamp.
- Every hour, and after each forecast refresh (05:30, 22:00).
- Load history and the model ranking refresh at 00:20; Solcast at 05:34,
  08:34, 10:34, 12:34, 14:34 and 22:04.

## Observability

- `reserve-data/journal.jsonl`: every replan (inputs, candidates,
  expected and pessimistic results, table, write outcome), write and
  message.
- `reserve-data/solcast.jsonl`: every Solcast forecast, hourly.
- `GET 127.0.0.1:9997/plan` and `/health`.
- `docker exec deye-reserve python -m deye_reserve.review [END]` lines a
  day's decisions up with Deye's history (SOC, grid bought by tariff, SOC
  in each outage), Solcast and the models against the panels, and the
  inverter's table.

## What the data showed (09–18.10)

- **16.10 replay** (closed loop, real load, sun and outages, schedule
  versions as published): with the service, minimum SOC in the outages
  would have been 47/42/36% against the real 48/19/78%, at ~15 against
  ~49 UAH. That day the inverter had no night charge, and the battery
  entered a 09:05 outage with 30%.
- **The weak spot**: a heavy load (~900 W, 3× the profile) before and
  during an outage that DTEK extends mid-outage (09:00–11:30 became
  09:00–15:00, published at 11:20) still reaches 20% in the replay. A
  2-hour margin for extensions plus the live load would not have caught
  it in time either. Parked by the owner on 17.10.
- **Sun, hourly error on clean hours 11–17.10**: service day-ahead P50
  138 W, Open-Meteo satellite (5.35 × GHI) 169 W, Solcast estimated
  actuals 175–219 W depending on settings. Top-3 by fit is currently
  Météo-France, ICON, UKMO; ECMWF is 4th but keeps its half of P50.
- **Inverter behaviour** under the service's table matches the model:
  the battery discharges to the day floor with the grid present and
  holds the night target at night.

## Open work

1. **Separate tables per day (agreed 18.10, not started).** The TOU
   table repeats daily, so writing tomorrow's just-in-time charge today
   also fires it today (e.g. a 22:00 charge for tomorrow would run
   tonight at the day tariff), and the chat showed the same table for
   today and tomorrow. Wanted: plan today and tomorrow **together** (the
   night target and today's charges may depend on tomorrow, e.g. a long
   outage), but write only **today's** table now: today's charges and
   the night target from 23:00. Right after 00:00, replan and write the
   new day's table with its charges. Simulate with today's table until
   midnight and tomorrow's after. In the chat, "на завтра" shows
   tomorrow's own table, with "застосую після 00:00" when it differs
   from what is in the inverter.
2. A full review of the approaches, with web research, to raise
   effectiveness (see the session brief).
3. Use the known tilt/azimuth (global tilted irradiance) instead of
   5.35 × GHI; decide on Solcast after a week of logged forecasts.
4. The heavy-load and mid-outage-extension case above.
5. The green tariff, when it comes: export instead of curtailment
   changes the economics of the night target.

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
  `deye-reserve`).
- **Tests**: no venv in the repo; install `requirements-dev.txt` into a
  temporary venv, then run `pytest`. Fixtures are real Deye history
  09–17.10 and Open-Meteo forecasts.
- **Deye quota**: 100k calls a month for the app ID; dtek-service uses
  ~47–50k, deye-reserve ~1k. **Solcast**: 10 calls a day, the service
  uses ≤8.
