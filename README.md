# DTEK service

One process that watches DTEK outage schedules and the home power supply and
tells Telegram about it. It replaces four containers: `dtek-parser-kem`,
`dtek-parser-krem`, `lightbot` and `schedulebot`.

## What it does

- **Schedules.** For each address ("source") it reads the hourly schedule
  and the current outage notice from dtek-kem / dtek-krem. Changes go to that
  source's chats through its bot: kem → `@light_comeodore_bot` (English),
  krem → `@dtek_parser_bot` (Ukrainian).
- **Power.** It watches the Victron AC-input voltage in Home Assistant and
  reports outages and restores to the kem chats, with schedule context.
- **Inverter (krem).** It reads the Deye inverter in Круглик from Deye Cloud
  (`device/latest`) and shows in the krem mini app where the house's power
  comes from: the house in the middle, sun, grid and battery around it, the
  links coloured and arrowed by what flows where. When the inverter loses or
  regains the grid it messages the krem chats the way the Victron does for
  kem: duration, battery, and whether the schedule has power off now (with
  its end) or the outage is unscheduled (`DEYE_NOTIFY_GRID`; state in
  `power_events` row 2). The logger uploads every ~72 s and Deye has no push, so polls are
  timed to land just after the next reading reaches the cloud (`/health`
  shows the cadence, the lag and the calls per reading). Settings are
  `DEYE_*`; `tools/deye_probe.py` lists the stations and measures the
  refresh rate.
  The krem badge itself follows the schedule, as dtek-krem posts no outage
  notices.
- **Batteries (kem).** The kem mini app shows three batteries: Home (Victron,
  live from the same Home Assistant subscription as the power monitor) and
  the building's Elevator and Water Deye inverters, read in the very same
  Deye call as the krem one (`DEYE_BATTERIES`), so they cost no extra quota.
- **Battery plan (krem), a container of its own: `deye-reserve`.** It works
  out the Deye inverter's Time of Use table (six slots: start, target SOC,
  grid charge) for Круглик from the DTEK schedule (queue `DEYE_RESERVE_GROUP`,
  read from dtek-service on 127.0.0.1:9998), the sun (Open-Meteo: P50 =
  mean of ECMWF and the median of the three models whose day-ahead
  forecasts matched the panels best over the last two weeks, pessimistic =
  min of the ECMWF-ensemble P10 and that median; PV W = 5.35 × GHI) and the
  house's load (a week). Both come from one Deye history call a day; the
  model ranking leaves out hours with the battery near full, when zero
  export holds the panels down. The night target is the
  cheapest one from 45 to 95% that keeps ≥25% through every outage in the
  pessimistic case (P80 load ×1.1); by day the battery stays at ≥45% for
  unscheduled outages, and outages the night charge cannot cover get a
  grid charge right before them. Once a week the night goes to 100% to
  keep the SOC calibrated. Without a schedule, a forecast or a fresh
  inverter reading it falls back to the old 90/45 table. dtek-service
  posts to it when krem's schedule data changes and when the inverter
  loses or regains the grid (`DEYE_RESERVE_URL`, `/api/v1/inverter/latest`
  gives it SOC and grid); it replans 2.5 min after such an event (batches),
  polls the schedule every 10 min as a fallback, replans hourly and after
  the 05:30 and 22:00 forecast refresh. The owner's own chat
  (`DEYE_RESERVE_CHAT_ID`, through the krem bot) hears about it
  only for a day whose DTEK schedule was published or changed: that day's
  final table and whether it is in the inverter (plus an alarm when a
  write fails). With `DEYE_RESERVE_WRITE=true` each replan writes a changed
  table (`/order/sys/tou/update`, then waiting for the order and for
  `/config/tou` to show it), only with the grid present, outside an outage,
  at most 6 times a day, one command at a time. Every replan, write and
  message goes to `reserve-data/journal.jsonl` with what it was based on;
  `docker exec deye-reserve python -m deye_reserve.review` lines a day's
  decisions up with what Deye recorded. `GET 127.0.0.1:9997/plan` shows
  the current plan, `/health` the loop.
- **Mini app and API.** Each source's port serves its Telegram mini app and
  `/api/v1/*`: kem on 9999, krem on 9998.
- **Wake-on-LAN.** `POST /api/v1/wol` is kept for HA's
  `rest_command.wake_pc`.

## How data is read

The site is behind Incapsula, so a headless Chromium keeps one page open per
site. Data comes from the same sources the page itself uses:

- `DisconSchedule.fact`, embedded in the page: the schedule for every group.
- `POST /ua/ajax method=getHomeNum`, called from inside the page. It returns
  the house's groups and the outage notice (reason, start, estimated
  restoration). Passing the known `updateFact` makes it also return a newer
  `fact` when DTEK publishes one.

The address form is never filled in. A house fed by two lines (`krem`, вул.
Садова 10) gets both lines.

Cookies of the last successful load are kept in `data/state-<source>.json`
and seed every new browser context, so resets and restarts keep the
Incapsula session. If Incapsula still escalates to an hCaptcha (the log says
"Incapsula shows an hCaptcha"), pass it by hand over VNC with
`tools/solve_captcha.sh <source>` on the server; see the script for the
tunnel. The service picks the saved cookies up on its next attempt.

## Reliability rules

- Change detection (`app/engine/detector.py`) matches days by date, so
  midnight is not a special case.
- A failed fetch never reaches the detector, so "DTEK did not answer" can't
  look like "nothing is published".
- A day disappearing, or an outage notice clearing, has to be seen several
  times in a row before it counts.
- For today only the remaining hours are compared.
- Snapshot, detector baseline and outgoing messages are written in one
  database transaction. A crash or restart never re-sends or loses a
  notification.
- Messages go through `notification_outbox`. Delivery retries with backoff,
  keeps per-chat order, honours Telegram rate limits, and drops a message
  after 6 h rather than sending it stale.
- A Postgres advisory lock allows only one instance per schema.
- The power monitor uses HA `subscribe_entities`. After a reconnect it gets
  the current state with its `last_changed`, so an outage that began during
  the gap is still reported, with its real start time.
- Every loop is supervised and restarted on a crash.
- One heartbeat, `dtek-service`, is sent every 30 s, but only while every
  check passes: both addresses fetched within 3 minutes, Home Assistant
  connected, the delivery loop cycling. `GET /api/v1/health` lists which
  check fails, and the log says so the moment the heartbeat pauses.

## Run

```bash
cp .env.example .env   # fill in tokens and chats
docker compose up -d --build
```

Health is at `GET /api/v1/health`. It returns 503 if a source has had no
successful fetch for 5 minutes, or if Home Assistant is disconnected.

To stage next to production, set `DB_SCHEMA=dtek_staging` (separate tables
and lock), `NOTIFY_DRY_RUN=true`, and other `*_PORT` values.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/fixtures` holds real DTEK responses from 01.10.2026, and for
deye-reserve the Deye station history of 09-17.10.2026 and Open-Meteo
day-ahead forecasts (previous runs) of those days.
