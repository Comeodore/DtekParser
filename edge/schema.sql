-- One row per read.
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL,          -- ms since epoch
  source TEXT NOT NULL,         -- kem | krem
  interval INTEGER,             -- minutes between reads in force at that moment
  ok INTEGER NOT NULL,
  stage TEXT,                   -- where the run ended: cookies, getHomeNum, parse, done
  refreshed INTEGER NOT NULL,   -- 1 when the CSRF cookie had to be fetched anew
  challenged INTEGER,           -- 1 when Incapsula answered with its JS challenge
  http INTEGER,
  iinfo TEXT,                   -- Incapsula's x-iinfo header
  ms INTEGER,                   -- time spent on DTEK
  fact_update TEXT,             -- DisconSchedule.fact.update the run ended with
  fact_changed INTEGER NOT NULL,
  groups TEXT,
  outage TEXT,                  -- "start|end" of the house's outage notice, "" when there is none
  notice TEXT,                  -- type:sub_type of that notice
  updated_ts TEXT,              -- getHomeNum updateTimestamp
  mode TEXT,                    -- the reserve's turn: off, home, reserve, shadow (MODE=test), no push
  home_age INTEGER,             -- seconds since the home service's last push
  events INTEGER,               -- what dtek-brain detected, when the reserve had the turn (null: read unchanged, not asked)
  queued INTEGER,               -- messages that put in the outbox (one per chat)
  -- prod_*, match_*: the comparison with the home service, until the reserve went live on 02.11.2026
  prod_ok INTEGER,              -- null when the home service was not asked
  prod_healthy INTEGER,         -- the home service's own read of this source worked lately
  prod_fact TEXT,               -- what dtek-service showed at the same moment
  prod_outage TEXT,
  prod_error TEXT,
  match_fact INTEGER,
  match_outage INTEGER,
  error TEXT,
  colo TEXT,                    -- data center the read ran in, logged once an hour
  ip TEXT                       -- address it went out from, logged with colo
);
CREATE INDEX IF NOT EXISTS runs_source_at ON runs (source, at);

-- Cookies, the last fact.update and the challenge back-off, carried from run to run by the cron
-- version; the Durable Object keeps them in its own storage.
CREATE TABLE IF NOT EXISTS state (
  source TEXT PRIMARY KEY,
  cookies TEXT,
  fact_update TEXT,
  strikes INTEGER,              -- challenges in a row
  backoff_until INTEGER,        -- ms since epoch; runs before it are skipped
  block INTEGER,                -- the experiment hour the cookies and back-off belong to
  at INTEGER
);

-- The whole fact, each time DTEK published a new one.
CREATE TABLE IF NOT EXISTS facts (
  at INTEGER NOT NULL,
  source TEXT NOT NULL,
  fact_update TEXT,
  body TEXT
);

-- Since 05.11.2026 the stateless side keeps what it needs here, so that nothing wakes a Durable Object while the
-- home service is alive.

-- The home service's last push per source (POST /home/<source>, app/edge.py).
CREATE TABLE IF NOT EXISTS home (
  source TEXT PRIMARY KEY,
  at INTEGER,                   -- ms, the last push after a good cycle: the turn for DTEK's news follows it
  alive_at INTEGER,             -- ms, the last push of any kind (or claim of the power): the power's turn follows it
  body TEXT                     -- JSON, the last push after a good cycle: baseline, fingerprint, lang, notify_outages,
                                -- chat_ids, token (the source's bot), deye (kem: the home service's Deye token),
                                -- power ({on, since} as held at home, also updated by a sign of life)
);

-- The reserve's own per source.
CREATE TABLE IF NOT EXISTS edge (
  source TEXT PRIMARY KEY,
  since INTEGER,                -- ms, the reserve has had DTEK's news since then; null while the home service has
  loop_at INTEGER,              -- ms, the object's next alarm; null while it sleeps
  read TEXT,                    -- JSON, the object's last good read: house, house_ts, read_at, fact and names (the
                                -- house's groups only), for the pages and the power messages
  power TEXT,                   -- JSON {on, since}, the power at the address as judged here
  power_from INTEGER,           -- ms, the power has been watched here since then; null while the home service does
  page_at INTEGER               -- ms, a stand-in page last asked for its data
);

-- victron (kem's last VRM read), deye (every inverter's last device/latest), deye_token (one of its own), vrm_site.
CREATE TABLE IF NOT EXISTS kv (
  name TEXT PRIMARY KEY,
  value TEXT,                   -- JSON
  at INTEGER
);
