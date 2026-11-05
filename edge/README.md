# dtek-edge: the reserve of dtek-service on Cloudflare

The schedules keep coming when the homelab is off: `dtek-edge` reads DTEK from a
Cloudflare Worker, without a browser, and while the home service is away it messages that
service's chats with DTEK's news in its place, through the same bots and in the same words. It
started on 01.11.2026 as a test that only read (as `dtek-watch-test`, in `cf-watch/`), and has been the
reserve since 02.11.2026; renamed `dtek-edge` on 04.11.2026.

Since 02.11.2026 it polls the way airdanger's edge does: a Durable Object (`Watcher`, one per
source, kem and krem) reads, then sets its alarm for the next read 5 minutes later. The cron
(`*/10 * * * *`) only re-arms the loop if it ever stops. Before that each read was a cron run,
and cron runs land in a random Cloudflare data center (30 of them in 3 hours, Warsaw 6 times out
of 379), so the site saw one visitor hop between continents. The Durable Object is created with
the location hint `eeur` and stays where it was placed (Frankfurt, despite the hint); it logs
its data center and outgoing address (`colo`, `ip`) once an hour.

## Taking turns with the home service

Only one side messages a source's chats at a time, and only DTEK's news goes through the
reserve: schedules published, changed or cancelled, and the emergency or planned outage notice
at the address with its changes. Power going off or coming back stays with the home service.

- **The home service pushes.** After each good cycle (a successful DTEK read, about once a
  minute) a source of dtek-service sends the worker its detector baseline and the source's bot
  token, chats, language and `notify_outages` (`POST /home/<source>`, app/edge.py). The bots and
  chats therefore always are the ones in the home `.env`. The pushes are also how the worker
  knows the home service is alive: they go out to `workers.dev`, so a broken tunnel does not
  count as the service being away, while a home service that cannot read DTEK stops pushing.
- **The reserve takes over.** With `MODE=live`, a source whose last push is 5 minutes old
  (`LEASE_MIN`) is taken over on the next read: from then on every read goes through
  `dtek-brain`, starting from the last pushed baseline, and the texts go to the same chats
  through the same bot. `dtek-brain` (brain.py, a Python Worker on a service binding) runs the
  home service's own parser, detector and message texts (app/brain.py), so it decides and
  writes exactly as the home service would. It is asked only when the read differs from the
  last one (or a day or a notice is being confirmed gone); messages go out in runs of their own,
  three at a time, and are dropped after 15 minutes undelivered.
- **The home service claims it back.** A source that has not pushed for 3 minutes (or just
  started) first asks the worker (`POST /claim/<source>`). If the reserve took over meanwhile,
  the worker hands over the baseline it went on with and stops; the home service detects from
  that baseline, so what the reserve sent is not sent again and only later news follows.
- **Power gone or back: from here alone (05.11.2026).** Each source's object watches the power at
  its address and sends the messages that it is gone or back, whether the home service runs or
  not: kem from its Victron (svitlo-edge's VRM read; the GX logs once a minute, and since 05.11
  svitlo-edge reads a few seconds after each log becomes readable, learning that lag, and says when
  it reads next, which kem's look follows by 3 s), krem from its Deye inverter's grid voltage
  (≥ 100 V, every 90 s, the logger uploads every ~72 s). dtek-brain's `power` judges and words them with the home service's own `next_state`
  and `render_power` (duration, battery, the schedule's end or "unscheduled", DTEK's notice);
  they go through the same outbox, kept 6 h, as at home. A first look only sets the state (`power`
  in storage). The home service sends none of them while `EDGE_URL` is set (Router.for_grid) and
  says so in each push (`power_messages: false`); a home service of before that still sends its
  own, and while it is alive this side stays quiet, so the two never send the same. They come a
  minute or two after the change rather than seconds, as Home Assistant did. krem's state was
  seeded once on 05.11 (grid gone since 00:30, from Deye's 5-minute history).
- **The schedules themselves.** While the home service runs, its mini app shows today's and
  tomorrow's schedules; it is down with it. So while the reserve is in charge, the chats also get
  the schedules as text ("Графік на сьогодні" / "Today's schedule") whenever they have not seen
  them as they are: on taking a source over, when schedules apply again after an emergency
  without them, and when tomorrow is published with no outages. A day DTEK changed is already in
  its "оновлено" message and is not sent twice. What the chats were shown (`shown`) is kept apart
  from the turn, so a home service that comes and goes does not make the reserve repeat it. Added
  on 04.11.2026, after kem's emergency without schedules turned into an outage by the schedule at
  11:09 and the kem chats, with the lab off, had no way to see the day.
- **The mini app, stood in for (05.11.2026).** `dtek-schedule.comeodore.services` (kem) and
  `dtek-krem-schedule.comeodore.services` (krem) are Worker routes to this worker. Every request goes
  on to the home service through the tunnel as before; only when that answers 530 (no tunnel), 502–504
  or 520–524, or not within 8 s, `/` is the same page (app/static/index.html, uploaded with the
  worker as a Text module) and `/api/v1/schedule` comes from here: dtek-brain's `page` builds it from
  the last good read (kept as `house`, `read_at`), with `reserve: true`, which the page shows as a
  note with the time of that read. For kem it also has the power really there and the batteries row:
  the Victron from svitlo-edge (its `Power` entrypoint, over the `POWER` service binding: grid, since
  when, SOC and battery power from VRM, read there right after each GX log) and the building's Deye inverters
  (Elevator, Water). krem's page has the inverter diagram of Круглик (2511090098), without what
  needs the home service: today's grid by tariff (it shows the total), the sun forecast and until
  when the battery lasts. All three inverters come from one device/latest call (Deye counts calls),
  made by the kem object at most once a minute and only while a page asks; krem asks the kem object.
  Its token is the one the home service pushes with kem's baseline (`deye` in the push, app/edge.py)
  while that is good, else one of its own from the secrets `DEYE_APP_ID`, `DEYE_APP_SECRET`,
  `DEYE_EMAIL`, `DEYE_PASSWORD_SHA256` (the home service's `DEYE_*`), kept in its storage until a
  day before it expires. dtek-brain parses the readings with the home service's own
  `app/inverter/reading.py`. One dtek-brain call serves a page for 20 s. The page asks once a
  minute instead of every 10 s while its answers carry `reserve` (and leaves the owner's TOU table
  alone, which only the home service has), since every ask is a request of the account's free
  100,000 a day, to Workers and to Durable Objects alike (airdanger's alarm alone makes ~43,000 of
  the latter). Both routes fail open (`request_limit_fail_open`, 05.11.2026): past the daily
  Workers limit they go straight to the tunnel as before, not to error 1027.
- **Nothing piles up.** dtek-service now drops DTEK's news it could not deliver within 15
  minutes (`EVENT_TTL`) instead of 6 hours: after an outage its next cycle reports the difference
  as it is then.

Both ends fail open: a home service that cannot reach the worker works as it did without it,
and would rather repeat a message than lose one. A duplicate is possible only when the home
service is alive and can message Telegram but cannot reach `workers.dev` for 5 minutes.

`MODE=test` runs the reserve on every read regardless of the home service, from a baseline of
its own, and sends only to `TEST_CHAT_ID`, marked "🧪 reserve". `POST /test/<source>` with
`{"forget": "days" | "outage" | "shown" | "all"}` makes it forget what the home service has seen
(or the schedules it showed), so the next read (at once) reports the current state as news. `POST /status/<source>` shows the last
push (age, chats, language), the reserve's turn and the outbox; all endpoints take
`Authorization: Bearer <EDGE_KEY>`, the key in the home `.env`. Tested on 02.11.2026: both bots
delivered to the owner's chat, kem's emergency notice in English and krem's day in Ukrainian.

Then live, the same day, with dtek-service stopped from 14:53 to 15:07 (Kyiv time):
- **Takeover.** The reserve took both sources over at 15:06:29, its first read after the 10-minute lease.
- **A real change in between.** DTEK had added 23:00–24:00 to krem's day at 14:54, after the home service stopped. The reserve sent it as "Графік на сьогодні оновлено" to the 4 krem chats at 15:06:31.
- **Handover.** Back at 15:07, the home service claimed both sources and went on from the reserve's baseline. It sent nothing again.

So while the home service is away, news can be late by up to the lease plus one read interval: 15 minutes
with the lease of 10 minutes it then had. The user had it cut to 5 minutes the same day, so 10 minutes at
most since.

## How it reads DTEK

Incapsula challenges only the HTML page; `POST /ua/ajax` reaches the site. A request without a
CSRF token gets `400` and the Yii2 cookie `_csrf-dtek-<site>`. That cookie is signed, not
encrypted, so the worker takes the token from it and masks it the way Yii does (random mask +
mask XOR token, base64url) for `X-CSRF-Token`. `getHomeNum` then answers as it does for the
page: the houses with their groups and outage notice, and, when the `updateFact` it is given is
older than DTEK's, the whole `fact` and `preset` too. The cookies are kept between runs; a new
CSRF cookie is fetched only when the old one stops working (`refreshed = 1`), and Incapsula's
own cookies stay through that, so the worker remains one returning visitor.

Incapsula does not always let the POST through: now and then it answers with its JS challenge
(`challenged = 1`). The worker then does not ask again at once: while the challenges go on, the
next read waits the longer of the interval and 2, 4, 8, then 15 minutes.

## Finding the interval

One round on 01.11.2026, cron version, both sources together. Each interval was read for an hour,
with 20 quiet minutes between, and each hour started as a new visitor with no back-off. It was
stopped at 01:10 Kyiv time, once the random data centers came to light, so 1 and 3 minutes were
never read. Kyiv time, good reads out of all, and the longest stretch without data:

| Interval | Hour | krem | kem |
| --- | --- | --- | --- |
| 4 min | 22:00-23:00 | 13/14, 8 min | 12/14, 8 min |
| 5 min | 23:20-00:20 | 5/11, 25 min | 9/12, 10 min |
| 2 min | 00:40-01:10 | 14/16, 4 min | 8/12, 15 min |

About 23% of reads were challenged. The challenges came in streaks, and reading more often did
not draw more of them: 5 minutes was the worst hour. A new visitor was no clean start either:
three of the six hour starts were challenged on the first read. Every read that went through
matched the home service (fact and house notice). One hour per interval, at night, is a small
sample, so this shows large differences only. 5 minutes, the longest interval tried, is what the
Durable Object reads at.

The Durable Object's first night (02.11.2026, 01:40-10:51 Kyiv time, kem only) did much better:
103 of 111 reads went through, 7% were challenged, and every challenge was a single one, so the
longest stretch without data was 10 minutes. The CSRF cookie was fetched once, at the start, and
the outgoing address stayed the same all night. Every good read matched the home service, and
DTEK's new fact of 06:12 arrived on the 06:21 read, as it did at home. Both the place and the
visitor stayed the same through the night, while in the round both changed, so it is not known
which of the two made the difference.

Rows from `at >= 1793563394807` (20:03 UTC) up to 23:12 UTC belong to the round. The Durable
Object's rows come after and have `colo` set once an hour. Per interval, the challenge share and
the longest stretch without data:

```sql
WITH gaps AS (  -- minutes between two good reads of the same interval (one hour each)
  SELECT source, interval,
         (at - lag(at) OVER (PARTITION BY source, interval ORDER BY at)) / 60000.0 AS gap
  FROM runs WHERE at >= 1793563394807 AND ok = 1
), totals AS (
  SELECT source, interval, count(*) AS reads, sum(ok) AS ok, sum(challenged) AS challenged,
         count(DISTINCT at / 3600000) AS blocks
  FROM runs WHERE at >= 1793563394807 GROUP BY source, interval
)
SELECT t.interval, t.source, t.reads, t.ok, t.challenged,
       round(100.0 * t.challenged / t.reads, 1) AS challenged_pct,
       round(max(g.gap), 1) AS max_gap_min, round(avg(g.gap), 1) AS avg_gap_min, t.blocks
FROM totals t LEFT JOIN gaps g USING (source, interval)
GROUP BY t.interval, t.source ORDER BY t.interval, t.source;
```

## Budget on Workers Free (shared with airdanger)

Free limits are per account and per day (UTC) unless noted; going over one makes further
operations of that kind fail until midnight UTC, for airdanger too. airdanger as measured on
30.10-01.11.2026; the reserve as estimated for kem and krem every 5 minutes, with the home
service pushing each about once a minute.

| Limit (Free) | airdanger | dtek-edge + dtek-brain |
| --- | --- | --- |
| Worker requests, 100,000 | ~1,450 (edge cron) | ~3,000 (2 × 1,440 pushes, cron every 10 minutes, dtek-brain while in reserve) |
| Durable Objects duration, 13,000 GB-s | ~11,000 (Poller active all day) | ~30 (0.34 s active per read, 2 × 288 reads; the pushes a few ms each) |
| Durable Objects requests, 100,000 | ~43,000 | ~3,700 (2 × 288 alarms, 2 × 1,440 pushes, 2 × 144 cron calls) |
| Cron triggers, 5 per account | 1 | 1 |
| D1 rows written, 100,000 | 0 | ~600 (1 a read, 2 when the fact changes) |
| D1 rows read, 5,000,000 | 0 | only the queries below |
| CPU, 10 ms per invocation | edge p99 ~1.1 ms | 5 ms p50, 8 ms max per alarm (cron version: 6 p50, 12 max); dtek-brain 5-9 ms a call |
| Subrequests, 50 per invocation | | 3 to 5 a read (DTEK once or twice, D1, dtek-brain, trace once an hour); 3 messages a send run |

The Durable Objects duration is the tight line: airdanger's Poller is active all day and uses
about 85% of it. A Durable Object is billed only while it runs or is held in memory unable to
hibernate; this one only waits on its alarm, so it costs the seconds its reads take. Check it
with GraphQL `durableObjectsPeriodicGroups` (`activeTime`, microseconds, times 0.128 GB) per
`namespaceId`. CPU is per invocation: a read that goes over fails on its own and does not touch
airdanger. In the cron version 3 reads of 78 went over 10 ms and still finished, which is why
detection runs in dtek-brain's own invocation and messages in runs of their own. Pyodide makes a
cold dtek-brain call expensive (173 ms on the first deploy, against 0.06 ms for the same work
in CPython), so brain.py runs one pass through everything at import, which Python Workers keep
in their snapshot; that brought a call down to 5-9 ms.

## Reading the results

In the dashboard, D1 > `dtek-edge` > Console, or with
`npx wrangler d1 execute dtek-edge --remote --command "<SQL>"`. Everything from before the rename
was copied over (the old D1 `dtek-watch-test` was deleted on 05.11.2026).

Last runs (`mode`: `home` while the home service is in charge, `reserve` while the worker is;
`home_age`: seconds since the last push):

```sql
SELECT datetime(at / 1000, 'unixepoch') AS utc, source, ok, refreshed, challenged, http, ms,
       fact_update, outage, mode, home_age, events, queued, error
FROM runs ORDER BY at DESC LIMIT 20;
```

When the reserve had the turn:

```sql
SELECT datetime(min(at) / 1000, 'unixepoch') AS from_utc, datetime(max(at) / 1000, 'unixepoch') AS to_utc,
       source, count(*) AS reads, sum(events) AS events, sum(queued) AS messages
FROM runs WHERE mode = 'reserve' GROUP BY source, at / 3600000 ORDER BY 1;
```

A day per row:

```sql
SELECT date(at / 1000, 'unixepoch') AS day, source, count(*) AS runs, sum(ok) AS ok,
       sum(challenged) AS challenged, sum(refreshed) AS refreshed,
       sum(match_fact = 0) AS fact_differs, sum(match_outage = 0) AS outage_differs,
       sum(prod_ok = 0) AS home_down, max(ms) AS max_ms
FROM runs GROUP BY day, source ORDER BY day DESC, source;
```

`prod_*` and `match_*` compared each read with the home service until 02.11.2026; since the
reserve went live the worker no longer asks the home service, the home service tells it.

Where the reads ran from:

```sql
SELECT datetime(at / 1000, 'unixepoch') AS utc, colo, ip FROM runs
WHERE colo IS NOT NULL ORDER BY at DESC LIMIT 24;
```

CPU per read is in Workers Observability (`$workers.cpuTimeMs` on the `alarm` events of
`dtek-edge`). The data center of cron runs is the `coloCode` dimension of the GraphQL
`workersInvocationsAdaptive` dataset.

## Pausing and removing it

To pause, add a plain-text variable `PAUSED` = `1`: the next alarm does not set another, and the
cron clears any that is left. Remove the variable to start again; the Durable Object keeps its
cookies, so it is the same visitor. To begin as a new visitor, read through a new object
(another name passed to `idFromName`).

To stop only the messages, set `MODE` to `off`: the worker keeps reading and taking the pushes
but never takes a source over (claims then always answer that it did not). Removing `EDGE_URL`
and `EDGE_KEY` from the home `.env` stops the pushes; the worker, with `MODE=live`, would then
take both sources over after 5 minutes, so set `MODE=off` first.

To remove everything, delete the Workers `dtek-edge` (with its Durable Object namespace) and
`dtek-brain`, the D1 database `dtek-edge` (`npx wrangler delete` and
`npx wrangler d1 delete dtek-edge`), and `EDGE_URL`/`EDGE_KEY` from the home `.env`.

## The rename (04.11.2026)

`dtek-watch-test` was renamed in place (`PATCH /workers/workers/<id>` with the new name): the
Worker's id, its Durable Object namespace with all its storage (cookies, the last push, the turn,
the outbox), the cron and the secrets stayed; only the `workers.dev` address changed. The
namespace keeps its old name, `dtek-watch-test_Watcher`. The D1 database cannot be renamed, so
`dtek-edge` is a copy that the binding now points to.

Until the home service's `EDGE_URL` could be changed (the lab was off), a Worker named
`dtek-watch-test` stayed at the old address and forwarded every request to `dtek-edge`. On
05.11.2026 the forwarder and the old D1 database were deleted, and `EDGE_URL` became
`https://dtek-edge.maksymchuk-vladimir.workers.dev`. A home service that cannot reach this
worker cannot claim the sources back, so both would message the chats: keep `EDGE_URL` right.

To update dtek-brain after a change in the app modules it uses: `python edge/bundle.py out.json`,
then upload every file in it as a `text/x-python` module with `main_module` `worker.py` and the
flags `python_workers` and `disable_python_external_sdk`, before worker.js if both change.
