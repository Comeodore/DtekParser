# dtek-edge: the reserve of dtek-service on Cloudflare

The schedules keep coming when the homelab is off: `dtek-edge` reads DTEK from a
Cloudflare Worker, without a browser, and while the home service is away it messages that
service's chats in its place, with DTEK's news and the power gone or back, through the same bots
and in the same words. It
started on 01.11.2026 as a test that only read (as `dtek-watch-test`, in `cf-watch/`), and has been the
reserve since 02.11.2026; renamed `dtek-edge` on 04.11.2026.

Since 05.11.2026 it spends nothing of the account's free Durable Object time while the home service
is alive (see "No Durable Object while home is alive" below). The reading itself is still the way
airdanger's edge polls, but only while it is needed: a Durable Object (`Watcher`, one per source,
kem and krem) reads, then sets its alarm for the next read 5 minutes later, and with nothing left to
do it sets none and sleeps. Cron runs land in a random Cloudflare data center (30 of them in 3 hours,
Warsaw 6 times out of 379), so a read from the cron would show the site one visitor hopping between
continents; the Durable Object, created with the location hint `eeur`, stays where it was placed
(Frankfurt, despite the hint) and keeps its cookies, and logs its data center and outgoing address
(`colo`, `ip`) once an hour.

## Taking turns with the home service

Only one side messages a source's chats at a time. While the home service runs, it does everything:
DTEK's news, the power gone or back, the mini apps. The reserve steps in for what it can only once
the home service has gone quiet.

- **The home service pushes.** After each good cycle (a successful DTEK read, about once a
  minute) a source of dtek-service sends the worker its detector baseline and the source's bot
  token, chats, language, `notify_outages`, the power state it holds for the source (`power`: on,
  since when) and, for kem, its Deye token (`deye`) (`POST /home/<source>`, app/edge.py). A cycle that
  failed sends only a sign of life with the power state (`{alive: true, power}`, at most once a
  minute). The worker keeps them in D1 (`home`: `at` the last good push, `alive_at` the last of any
  kind, `body` the push), and nothing else: no Durable Object is woken. The pushes go out to
  `workers.dev`, so a broken tunnel does not count as the service being away.
- **The cron, every minute, reads D1 only** (`home`, `edge`, `kv`) and does more only for a source
  whose home service has gone quiet: it wakes that source's object once the object must read
  (readsWanted: the last good push is 5 minutes old, or the reserve has the turn, or a stand-in page
  was asked for in the last 10 minutes) and its loop is not running (`edge.loop_at`, the object's next
  alarm, missing or more than 2 minutes behind), and it watches the power (below). A source never
  heard from (no `home` row) is not taken for one away.
- **The reserve takes over DTEK's news.** With `MODE=live`, the object woken for a source whose last
  good push is 5 minutes old (`LEASE_MIN`) takes it over on that read: from then on every read goes
  through `dtek-brain`, starting from the last pushed baseline, and the texts go to the same chats
  through the same bot. `dtek-brain` (brain.py, a Python Worker on a service binding) runs the
  home service's own parser, detector and message texts (app/brain.py), so it decides and
  writes exactly as the home service would. It is asked only when the read differs from the
  last one (or a day or a notice is being confirmed gone); messages go out in runs of their own,
  three at a time, and are dropped after 15 minutes undelivered. Taking over, the object first notes
  it in D1 (`edge.since`).
- **The home service claims it back.** A source that has not pushed for 3 minutes (or just
  started) first asks the worker (`POST /claim/<source>`). The claim goes to the object only while it
  may have the turn (`edge.since`, or the last good push at least 4 minutes old); then, if the
  reserve took over meanwhile, the object hands over the baseline it went on with and stops, and the
  home service detects from that baseline, so what the reserve sent is not sent again and only later
  news follows. With the home service in charge again the object sleeps after its next alarm.
- **Power gone or back: home first, the reserve only while it is away (05.11.2026).** The home
  service sends these while it runs (Home Assistant for kem, the Deye inverter for krem). Once a
  source has not been heard from at all for 5 minutes (`alive_at`: no push and no sign of life), the
  cron watches the power at its address itself, with no Durable Object: kem's Victron from VRM every
  minute (the graph of three attributes for the last 10 minutes, ~600 bytes: AC input 471, SOC 144,
  battery power 243; `VRM_TOKEN`, `VRM_SITE_ID` 514696), krem's inverter (grid voltage ≥ 100 V) from
  Deye's `device/latest` at most every 90 s (each cron that is 90 s past the last call; all three
  inverters in one call), going on from the state the home service last pushed. A change is judged
  and worded by dtek-brain's `power` with the home service's own `next_state` and `render_power`
  (duration, battery, the schedule's end or "unscheduled", DTEK's notice; the schedule of the
  object's last read, or of the home service's last baseline when that is newer) and queued in the
  source's object, which sends it with DTEK's news, kept 6 h as at home. A turn with no pushed state
  only sets one at its first look (`first`). A home service that starts claims the power back before
  it watches it again (`POST /claim/<source>` with `{"power": true}`, Edge.take_back_power): it keeps
  the state left here (not a `first` one), so a change the reserve has reported is not reported
  again. This took over svitlo-edge's VRM reading (svitlo-edge, with its SvitloBot pings, was deleted
  on 05.11.2026; the user had removed the SvitloBot channel).
- **The schedules themselves.** While the home service runs, its mini app shows today's and
  tomorrow's schedules; it is down with it. So while the reserve is in charge, the chats also get
  the schedules as text ("Графік на сьогодні" / "Today's schedule") whenever they have not seen
  them as they are: on taking a source over, when schedules apply again after an emergency
  without them, and when tomorrow is published with no outages. A day DTEK changed is already in
  its "оновлено" message and is not sent twice. What the chats were shown (`shown`) is kept apart
  from the turn, so a home service that comes and goes does not make the reserve repeat it. Added
  on 04.11.2026, after kem's emergency without schedules turned into an outage by the schedule at
  11:09 and the kem chats, with the lab off, had no way to see the day.
- **The mini app, stood in for (05.11.2026).** The Worker routes `dtek-schedule.comeodore.services/` (kem)
  and `dtek-krem-schedule.comeodore.services/` (krem) cover the page alone, `/` with no query (Telegram
  opens it so, its own data going after `#`); since 05.11.2026 ~04:41 Kyiv, before that `/*`. So the
  page's requests for data, every 10 s while it is open, go straight to the home service through the
  tunnel and cost the reserve nothing; only opening the page is a request here, which goes on to the
  home service as before. When that does not answer (530 no tunnel, 502–504 or 520–524, or nothing
  within 8 s), `/` is the same page from here (app/static/index.html, uploaded with the worker as a Text
  module), and the page, finding the home server down the same way (or no answer in 8 s), asks
  `GET https://dtek-edge.maksymchuk-vladimir.workers.dev/page/<source>` for its data (CORS for that
  source's host only; `RESERVE_API` in the page). That data is built from D1 alone: dtek-brain's `page`
  from the object's last good read (`edge.read`: the house, `read_at`, the house's part of the fact),
  with `reserve: true`, which the page shows as a note with the time of that read. For kem it also has
  the power really there and the batteries row: the Victron as the cron last read it from VRM
  (`kv.victron`) and the building's Deye inverters (Elevator, Water); krem's page has the inverter
  diagram of Круглик (2511090098), and since 07.11.2026 today's grid by tariff (night / day) and the
  day's sun forecast as at home: krem's pushes carry them (`inverter`: the inverter's daily grid
  counter at 07:00 and 23:00 as the home service noted it, GridDay, and deye-reserve's P50 for each of
  the three days its forecast covers, so a lab off past midnight still has the next day's), and while
  the home service is away the cron notes the counter at those hours itself from its Deye reads
  (`kv.grid_day`, worker.js followGridDay, the same rule as at home: the last reading before the hour,
  the one after at most 10 minutes later). A tariff hour neither side saw leaves the total alone, as at
  home. Only until when the battery lasts still needs the home service (deye-reserve). The Deye readings are the cron's
  (`kv.deye`), with the token the home service pushes with kem's baseline while that is good, else one
  of its own from the secrets `DEYE_APP_ID`, `DEYE_APP_SECRET`, `DEYE_EMAIL`, `DEYE_PASSWORD_SHA256`
  (the home service's `DEYE_*`), kept in `kv` until a day before it expires. dtek-brain parses the
  readings with the home service's own `app/inverter/reading.py`. A page served from here marks itself
  wanted (`edge.page_at`, at most once a minute), and for 10 minutes the cron reads the Victron and
  Deye and the object DTEK for it, in case the home service is alive and only the tunnel is down; asking
  `/page/<source>` marks nothing, so nothing on workers.dev can wake a read. One dtek-brain call serves
  the data for 20 s in an isolate. The page asks once a minute instead of every 10 s while its answers
  carry `reserve`, and leaves the owner's TOU table alone, which only the home service has. Both routes
  fail open (`request_limit_fail_open`): past the daily Workers limit the page goes straight to the
  tunnel as before, not to error 1027.
- **Nothing piles up.** dtek-service now drops DTEK's news it could not deliver within 15
  minutes (`EVENT_TTL`) instead of 6 hours: after an outage its next cycle reports the difference
  as it is then.

Both ends fail open: a home service that cannot reach the worker works as it did without it,
and would rather repeat a message than lose one. A duplicate is possible only when the home
service is alive and can message Telegram but cannot reach `workers.dev` for 5 minutes (for the
power: when its internet is down that long, so that its own messages go out only once it is back).

`MODE=test` runs the reserve on every read regardless of the home service, from a baseline of
its own, and sends only to `TEST_CHAT_ID`, marked "🧪 reserve". `POST /test/<source>` with
`{"forget": "days" | "outage" | "shown" | "all"}` makes it forget what the home service has seen
(or the schedules it showed), so the next read (at once) reports the current state as news. `POST /status/<source>` shows the last
push (age, chats, language, power), the reserve's turn, the outbox, the object's alarm and the power as
watched here; it wakes the object, so it is for checks only; all endpoints take
`Authorization: Bearer <EDGE_KEY>`, the key in the home `.env`. Tested on 02.11.2026: both bots
delivered to the owner's chat, kem's emergency notice in English and krem's day in Ukrainian.

Then live, the same day, with dtek-service stopped from 14:53 to 15:07 (Kyiv time):
- **Takeover.** The reserve took both sources over at 15:06:29, its first read after the 10-minute lease.
- **A real change in between.** DTEK had added 23:00–24:00 to krem's day at 14:54, after the home service stopped. The reserve sent it as "Графік на сьогодні оновлено" to the 4 krem chats at 15:06:31.
- **Handover.** Back at 15:07, the home service claimed both sources and went on from the reserve's baseline. It sent nothing again.

So while the home service is away, news could be late by up to the lease plus one read interval: 15
minutes with the lease of 10 minutes it then had. The user had it cut to 5 minutes the same day. Since
05.11.2026 the cron wakes the object within a minute of the lease running out, and the object reads at
once, so the first read after the home service went quiet comes 5 to 6 minutes after its last push.

## No Durable Object while home is alive (05.11.2026)

Until 05.11.2026 every push of the home service was a call to its source's object, and the objects read
DTEK every 5 minutes and watched the power all the time: ~3,000 Durable Object requests a day and a
loop that never slept, on an account where airdanger's Poller takes ~83% of the free time. Since then
(commit 1bfc65e, deployed 03:40 Kyiv) the objects run only while the home service is away, and the
power is watched only then too, from the cron. Measured per 15 minutes (GraphQL, Kyiv time):

| | DO requests | DO GB-s |
| --- | --- | --- |
| before, the home service pushing (03:00–03:40) | 36, 34, 24 | 0.16–0.34 |
| after, the home service alive (03:42–03:59) | 0 | 0 |

The cron then takes 1–3 ms of CPU a run (D1 alone), a push 1–3 ms.

The takeover tested the same night, dtek-service stopped from 03:55:54 to 04:05:07 (Kyiv time), its last
push at 03:55:43:
- **Power.** Watched from 04:01:23, the first cron 5 minutes after the last sign of life, from the
  home service's own states (kem on since 02:37:09, krem off since 00:30); VRM read as the graph
  (~600 bytes), the cron at 5 ms of CPU.
- **DTEK.** Both objects had been woken at 03:59:23, since both mini apps were opened at 03:58 and
  served from here (their data still 502: no read of the objects was in D1 yet), and they took both
  sources over on their next read, at 04:04:24. Such a read now comes no later than the lease running
  out, so the takeover is 5 to 6 minutes after the last push either way. Nothing had changed: no
  messages. The alarms took 9–17 ms of CPU (an object's limit is 30 s).
- **Back.** At 04:05:08 the home service took back the power states left there (the same) and the
  baselines ("queued 0 messages"), and sent nothing again; the objects read for the open page another
  10 minutes and went to sleep.

Found on the way: the objects' alarms had stood in the past since 04.11.2026 23:50 UTC (the Durable
Object time ran out then, and the alarms never fired afterwards, while `getAlarm()` still returned
them), so the old loop had not read since; the cron now re-arms an alarm more than 2 minutes late. And
at the deploy D1 had no pushes yet, so the first cron took the home service for away and watched the
power from a first look, which the home service's start then took over as its own (kem and krem
"since" an hour off, restored by hand from VRM and Deye): a source never heard from is no longer taken
for away, and a state only first seen here is not handed back.

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
operations of that kind fail until midnight UTC, for airdanger too (on 05.11.2026 the Durable
Objects stayed refused until 01:01 UTC, not 00:00). airdanger as measured on 30.10–05.11.2026;
dtek-edge as designed since 05.11.2026, with the home service pushing each source about once a
minute.

| Limit (Free) | airdanger | dtek-edge + dtek-brain, home alive | dtek-edge + dtek-brain, home away |
| --- | --- | --- | --- |
| Worker requests, 100,000 | ~1,450 (edge cron) | ~4,300 (2 × 1,440 pushes, 1,440 cron runs) plus one a mini app opened | 1,440 cron runs, dtek-brain on changes, the stand-in pages and their data once a minute |
| Durable Objects duration, 13,000 GB-s | ~10,800 (Poller active all day) | 0 | ~30 a day (a read every 5 minutes per source, well under a second each) |
| Durable Objects requests, 100,000 | ~43,200 | 0 | ~700 a day (2 × 288 alarms, the sends, a wake-up) |
| Cron triggers, 5 per account | 1 | 1 (every minute) | |
| D1 rows written, 100,000 | 0 | ~2,900 (a push is one upsert) | ~600 reads + ~1,500 power readings |
| D1 rows read, 5,000,000 | 0 | ~10,000 (the cron's 3 queries) | about the same |
| CPU, 10 ms per invocation | edge p99 ~1.1 ms | push 1–3 ms; cron ~1 ms | cron: D1, VRM (~600 B), Deye (~14 KB, every other minute), D1; alarm 5 ms p50, 8 max; dtek-brain 5–9 ms a call |
| Subrequests, 50 per invocation | | 1 | cron 3–5; a read 3 to 5 (DTEK once or twice, D1, dtek-brain, trace once an hour); 3 messages a send run |

The Durable Objects duration is the tight line: airdanger's Poller is active all day and uses
about 83% of it, so a Durable Object anywhere else on the account that is held awake (a timer, a
hanging call) can run it out for everyone, as on 04.11.2026. A Durable Object is billed only while
it runs or is held in memory unable to hibernate; this one waits on its alarm, or on nothing when it
sleeps. Check it with GraphQL `durableObjectsPeriodicGroups` (`activeTime`, microseconds, times
0.125 GB) and `durableObjectsInvocationsAdaptiveGroups` (`requests`) per `namespaceId` (dtek-edge's is
`eedf1aa9b3cc4f26bcfb4ba71b332a2d`, still named `dtek-watch-test_Watcher`). CPU is per invocation: a
run that goes over fails on its own and does not touch airdanger. In the cron version 3 reads of 78
went over 10 ms and still finished, which is why detection runs in dtek-brain's own invocation and
messages in runs of their own; the first cron of 05.11.2026, with VRM's 105 KB diagnostics, a new
Deye token and two wake-ups, took 18 ms, which is why VRM is read as a graph of three attributes.
Pyodide makes a cold dtek-brain call expensive (173 ms on the first deploy, against 0.06 ms for the
same work in CPython), so brain.py runs one pass through everything at import, which Python Workers
keep in their snapshot; that brought a call down to 5-9 ms.

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

To pause, add a plain-text variable `PAUSED` = `1`: the next alarm does not set another, the cron
clears any that is left and watches no power. Remove the variable to start again; the Durable Object
keeps its cookies, so it is the same visitor. To begin as a new visitor, read through a new object
(another name passed to `idFromName`).

To stop only the messages, set `MODE` to `off`: the worker keeps taking the pushes and reading while
the home service is away, but never takes a source over or watches the power (claims then always
answer that it did not). Removing `EDGE_URL`
and `EDGE_KEY` from the home `.env` stops the pushes; the worker, with `MODE=live`, would then
take both sources over after 5 minutes, so set `MODE=off` first.

To remove everything, delete the Workers `dtek-edge` (with its Durable Object namespace) and
`dtek-brain`, the D1 database `dtek-edge` (`npx wrangler delete` and
`npx wrangler d1 delete dtek-edge`), and `EDGE_URL`/`EDGE_KEY` from the home `.env`. D1's `home`
table holds the bots' tokens from the pushes (and `kv` a Deye token): the database goes with them.

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
