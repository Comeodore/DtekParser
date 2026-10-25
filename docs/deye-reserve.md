# deye-reserve: what it is for and how it works

State as of 19.10.2026. The code is in `deye_reserve/`, tests in
`tests/test_reserve_*.py`; dtek-service's side is `app/reserve_hook.py`,
`app/api.py` (`/api/v1/inverter/latest`, the owner's `/api/v1/reserve/table`),
`app/webapp_auth.py`, `app/collector.py` and `app/inverter/deye.py` (webhooks).

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
| Battery | 2 × Deye SE-G5.1 Pro-B = 10.24 kWh; Batt Shutdown 10%, where the lights go out in an outage (the planner took 20% until 24.10); Max A Charge 100 A, Grid Charge 80 A with Grid Peak Shaving 4.5 kW (since 20.10 15:49; 50 A before, the grid breaker being unknown) |
| Panels | 11 × 615 W = 6.77 kWp, south roof slope (ridge runs west–east), tilt ≈ 30° |
| Site | 50.3084, 30.4067 (Круглик, Kyiv region), DTEK queue GPV2.2 (krem) |
| Wiring | the whole house, router included, is on the Load port; the logger keeps reporting through outages |
| Grid | zero export, no green (feed-in) tariff yet; while the battery carries the house the inverter still imports ~39 W (zero-export power), ~1.2 UAH/day |
| Tariff | 4.32 UAH/kWh day, 2.16 UAH/kWh night (23:00–07:00) |
| Load | 9.1 kWh/day in October (7.5 in September), no weekday pattern; mornings can reach ~900 W for hours (16–17.10) |

Measured on this battery (Deye history 09–10.2026): the load gets 9.75 kWh
per 100% of SOC; filling 100% takes 11.75 kWh at the battery, i.e. 12.3 kWh
from the grid or 11.2 kWh of PV surplus. Grid charging runs at ~2.6 kW into
the battery (2.9 kW drawn with the house) at the old 50 A. Until 18.10 Max A
Charge was 50 A and PV charging stopped at ~2.7 kW whatever the SOC; with
100 A the sun charged 3.0 kW on 20.10 at 93–97%, so the BMS does not hold it
at 2.7. Since 20.10 Grid Charge is 80 A (~4.3 kW) and Grid Peak Shaving keeps
the whole import, house and charger, under 4.5 kW.

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
  roof gets ~1.8× the horizontal). Since 24.10 **P50 = median of
  Météo-France, ICON and UKMO** (`DEYE_RESERVE_SUN_MODELS`; a model with
  gaps on a day is left out), pessimistic = min(ECMWF ensemble P10, that
  median). Day-ahead against the panels on 08–24.10, hours with room in
  the battery: this median was off by 170 W an hour and 0.90 kWh a day and
  about right on the whole (×0.97); mean(ECMWF, median of the top-3 by a
  14-day ranking), used until then, by 212 W and 1.48 kWh, 24% high
  (ECMWF alone 236 W, +36%; the ranking's best model 266 W: a fortnight's
  fit chases noise). All seven models are still ranked once a day against
  the panels (hours with the battery under 95%), only to watch: `/health`
  and the review show it.
- **Solcast** was watched 18–24.10 and dropped: its day-ahead was off by
  164–302 W an hour, never better than the day's best Open-Meteo model,
  always high, with a useless P10–P90 band (0.7–24 kWh).
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
  grid charge on, is charged up to it at 4.3 kW from the grid (80 A), or
  what the 4.5 kW import limit leaves after the house (0.83 into SOC); PV first covers the load, then charges the battery (0.91, up to
  5 kW); what does not fit is lost; out of the battery 0.95. During an
  outage it runs down to 10% (Batt Shutdown).
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
  start − missing energy / min(4.3 kW, 4.5 kW − the house's safe load) −
  30 min, on a 15-min grid.
- **Charge rate as measured**: every 3-min reading with the grid on, the
  battery taking ≥1 kW and next to no sun is a grid-charge sample
  (journal `charge`, state.json `charge_samples`, 7 days). From 10 samples
  below 90% the plan charges at their median, never above the configured
  4.3 kW (`/health` → `charge`). The first grid charge after the 20.10
  change is told once in the chat after 20 minutes: import, battery
  charge, the house, and whether peak shaving held 4.5 kW.
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

## The table in the krem mini app (owner only, since 25.10)

- At the bottom of the krem mini app, for the owner's Telegram user only
  (`DEYE_RESERVE_OWNER_ID`, else `DEYE_RESERVE_CHAT_ID` when it is a
  private chat). The page sends `Telegram.WebApp.initData` in
  `X-Telegram-Init-Data`; dtek-service checks its hash with the krem bot's
  token (`KREM_BOT`) by Telegram's rule, takes the user from it, and
  refuses initData over a day old. Anyone else gets 403 once and the page
  stays as it was; a plain browser has no initData and never asks.
- `GET /api/v1/reserve/table` reads deye-reserve's `/plan` (cached 30 s):
  the table the inverter holds now, as ranges (slots in a row with the
  same SOC and grid charge are one; the last runs on past midnight into
  the first when they are the same: 23–07), the one now marked; tomorrow's
  too when it differs, with the time it takes over (`switch_at`), or one
  table "today and tomorrow" when they are the same; whether it is in the
  inverter (`written_table`, order status 666), only a recommendation
  (writing off), not in yet, or the last write failed; the fallback, if
  any; and the reasons of the last table news of a shown day (state.json
  `why`).

## Messages (Telegram, the krem bot, owner's chat 370921204 only)

- **Table messages are off since 25.10** (`DEYE_RESERVE_TABLE_MESSAGES`,
  default false): the mini app shows the tables. The news below is still
  worked out and kept: state.json `sent_tables`, `told` and `why`, and a
  journal `message` row with `sent: false` (the review shows it as TABLE
  … not sent). Every day's table news, chat or not, is also an INFO line
  in `docker logs deye-reserve`: `table for 26.10: 07–23 45% · 23–07 70% ⚡;
  in the inverter; Причина: …; chat: off` (`grep 'table for'`; or "takes
  over at 23:00", "write failed: …"). What still goes to the chat: the write alarm, now on every
  write that fails for good (a table message used to carry it), and the
  battery alert during an outage.
- **A day's table** (with table messages on), as a code block in Ukrainian, when the day's DTEK
  schedule is first published, and again whenever the table changes,
  whatever changed it (since 19.10; before, only DTEK changes were told,
  so on 19.10 the chat showed night 55% while the inverter ran 85%).
  Today, and tomorrow once DTEK published it or its table took over at
  the switch. A day with no message of its own is held to the last table
  shown, so midnight with the same table is quiet; so is any change that
  leaves the table as it was. Today and tomorrow can come in one message,
  as one table ("на сьогодні і завтра, 19–20.10") when they are the same:
  they differ only by a day's own top-up, the night slots are the same
  night. Tomorrow's says "⏭ Застосую о HH:MM" while it is not in the
  inverter yet. No line when it is in the inverter; "⏸" when blocked, "🚨" when
  failed, plus a separate alarm when a write fails with no table news.
- **Why**, under the table(s), a blank line apart: "Причина: …", what
  changed since the replan before, which still had the old table
  (state.json `plan_ctx`); only when nothing did, since the last table
  message (`told`): DTEK published / changed / withdrew a day; tomorrow
  not published yet (a plan without outages); calibration on or done
  ("батарея зарядилась до 100%: калібрування вже не потрібне"); the night
  gone by (after 07:00 only the night slots changed: they now serve the
  coming night); the sun forecast per day (P50 and pessimistic, when
  either moved ≥ 0.3 kWh); the house's weekly load (≥ 0.3 kWh/day); the
  battery ≥ 5% off what the last message's plan expected for this hour;
  no data for a plan / data back. With none of these: a new forecast,
  a restart, or "перерахунок за поточним зарядом N%". When the night
  slots moved, the last item says which day the night is for and its sun:
  "ніч під 21.10: прогноз сонця 8.0 кВт·год (песимістично 5.0)" (after
  07:00 the night ahead is tomorrow's). Until 20.10 the reason was the
  whole diff since the last message: at 12:45 it listed a schedule change
  of 23:14 the evening before and today's sun, while the night moved
  50 → 60% because the battery reached 100% and the calibration ended.
  Expect about 1–3 extra messages a day, the morning night change among
  them.
- **The battery during an outage** ("🪫 Батареї може не вистачити до
  ввімкнення світла", no full stops; to every krem chat since 25.10:
  the owner's and `KREM_CHAT_IDS`, or `DEYE_RESERVE_ALERT_CHAT_IDS`):
  every 3 min the reading is looked at; without the grid, if at the
  house's load (mean of the last 30 min, minus PV) the battery reaches
  the 10% shutdown (20% until 24.10) before the scheduled end + 30 min
  (an outage not in the schedule: 2 h from now), the chats hear it once
  per outage, and again if DTEK moves the end. It is not tied to a SOC:
  the day's 45% floor only holds while the grid is there, in an outage the
  battery runs on down to 10%. On 07–18.10 this fires exactly once: 16.10
  09:05, 30% at 887 W, empty ~10:10, outage until 11:30. From the alert
  until the grid is back, deye-reserve's `/battery` gives the time it
  lasts at the present load (each 3-min look), and the krem mini app shows
  it to everyone in the diagram: "🪫 При поточному споживанні заряду
  вистачить до 10:10".

## When it replans

- 2.5 min after a webhook; later events push it back, but never past
  5 min after the first. An hourly replan waits for a batch that is
  still settling.
- When the 10-min fallback poll of the schedule sees a new DTEK stamp.
- Every hour, after each forecast refresh (05:30, 22:00), and at the time
  tomorrow's table takes over.
- Load history and the model ranking refresh at 00:20.
- The forecast is also fetched at start when the cached one is over 12 h
  old or from other models than the sun models. A new forecast keeps the hours already over as the old one had
  them (since 19.10): 22:00 is for tomorrow's night charge, and today's
  sun in the krem mini app no longer moves after sunset (on 19.10 a
  restart at 20:38 took it from 5.27 to 3.51 kWh).

## Observability

- `reserve-data/journal.jsonl`: every replan (inputs, candidates and their
  prices, expected and pessimistic results, the table now and every day's
  table, when tomorrow's takes over, write outcome), write, message and
  battery alert.
- `reserve-data/solcast.jsonl`: Solcast's forecasts of 18–24.10, kept as
  they were; nothing writes there any more.
- `GET 127.0.0.1:9997/plan` (with `written_table`, `last_write` and
  `why` for the mini app) and `/health`.
- `docker exec deye-reserve python -m deye_reserve.review [END]` lines a
  day's decisions up with Deye's history (SOC, grid bought by tariff, SOC
  in each outage), the plan's own day-ahead and morning sun and every
  model's day-ahead against the panels ("mix" is the sun models' median),
  and the inverter's table.

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
3. The green tariff, when it comes: export instead of curtailment changes
   the economics of the night target.

## Operations

- **Server**: `root@192.168.0.185 -p 2222`, `/opt/dtek-service`, not
  git. Deploy = rsync the changed files plus
  `docker compose up -d --build reserve`, after a copy to
  `rollback/code-<date>`. Use short or detached SSH commands: sessions
  drop when the laptop sleeps.
- **Secrets** are in `/opt/dtek-service/.env`: DEYE_* (shared with
  dtek-service), BOT_SCHEDULEBOT_TOKEN. Never print
  them. Settings are DEYE_RESERVE_* (see `.env.example`).
- **Loki** is at 127.0.0.1:3100 (containers `dtek-service`,
  `deye-reserve`); the krem collector logs every schedule version.
- **Tests**: no venv in the repo; install `requirements-dev.txt` into a
  temporary venv, then run `pytest`. Fixtures are real Deye history
  09–17.10 and Open-Meteo forecasts (horizontal of 17.10, tilted of 19.10).
- **Deye quota**: 100k calls a month for the app ID; dtek-service uses
  ~47–50k, deye-reserve ~1k.
