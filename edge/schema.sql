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
