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

`tests/fixtures` holds real DTEK responses from 01.10.2026.
