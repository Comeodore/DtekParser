// dtek-edge: the reserve of dtek-service. It reads the DTEK schedules from a Cloudflare Worker, without a
// browser, and while the home service is away it messages that service's chats in its place.
//
// Why no browser is needed: Incapsula challenges only the HTML page. POST /ua/ajax reaches the
// site, which answers a request without a CSRF token with 400 and sets the Yii2 cookie
// `_csrf-dtek-<site>`. The cookie is signed, not encrypted, so the token is read from it and
// masked the way Yii does (random mask + mask XOR token, base64url) for X-CSRF-Token. With an
// old updateFact the same call also returns the whole schedule (`fact`) and `preset`.
//
// While the home service is alive this side does nothing but note what it is told, and wakes no Durable Object
// (05.11.2026): the account's free Durable Object time and requests are shared with airdanger, whose Poller alone
// takes ~83% of the time, and running out of them on 04.11.2026 took airdanger down for 75 minutes. So:
// - the home service's pushes (POST /home/<source>, app/edge.py) go to D1 (`home`): one after each good cycle, a
//   sign of life (`alive`) after a failed one, each with the power state it holds there;
// - a cron, every minute, reads D1 only, and does more only for a source the home service has gone quiet on;
// - the mini app pages it stands in for are built from D1 too, never from an object.
//
// The turns with the home service (its side is app/edge.py):
// - DTEK's news (schedules, outage notices). With MODE=live, once a source's last good push is LEASE_MIN old the
//   cron wakes that source's object (`Watcher`), which reads DTEK every 5 minutes from then on, takes the source
//   over and sends each read through dtek-brain (the home service's own detector and texts, brain.py) from the last
//   pushed baseline, to the same chats through the same bot. A home service that is back claims the source
//   (POST /claim/<source>) before it detects anything and gets the baseline the reserve went on with, so nothing is
//   sent twice; the object then goes back to sleep. While in charge the reserve also sends today's and tomorrow's
//   schedules as they are whenever the chats have not seen them yet (taking over, schedules applying again,
//   tomorrow published with no outages), since the mini app that otherwise shows them is down with the home
//   service; what the chats were shown is kept apart from the turn (`shown`).
// - The power gone or back. The home service sends these while it is alive. Once it has not been heard from at all
//   for LEASE_MIN (neither a push nor a sign of life), the cron watches the power at each address itself, with no
//   Durable Object: kem's Victron from VRM every minute, krem's Deye inverter every 90 s (each cron that is 90 s past
//   the last call), going on from the state the home service last pushed. A change is judged and worded by
//   dtek-brain as at home (`power`) and queued in the source's object, which sends it. A home service that starts
//   claims the power back first (POST /claim/<source> with {power: true}) and goes on from the state left here.
// MODE=test runs the reserve always, from a baseline of its own, and sends only to TEST_CHAT_ID, marked, without
// taking the turn from the home service; any other MODE only reads while the home service is away.
//
// The mini apps' addresses (dtek-schedule / dtek-krem-schedule.comeodore.services, Cloudflare Tunnel to the home
// service) are routed through here too. Every request goes on to the home service as before; only when it does not
// answer (the tunnel or the service down) the page and its /api/v1/schedule come from here instead: the same
// index.html, with data dtek-brain builds from the object's last read (in D1), and for kem the Victron as VRM has
// it and the building's Deye inverters, for krem the inverter diagram. A page served here also has the cron and the
// object read for it for PAGE_WANTED_MIN, in case the home service is alive and only the tunnel is down.
//
// Budget on Workers Free (see README.md): while the home service is alive, no Durable Object time or requests at
// all; ~2,900 Worker requests and D1 writes a day for the pushes and 1,440 cron runs. While it is away: a read every
// 5 minutes per source in its object (well under a second each), the VRM and Deye calls in the cron.

import { DurableObject } from "cloudflare:workers";
// Served as the mini app while the home service is away (a Text module).
import PAGE_HTML from "../app/static/index.html";

// page: the host of the source's mini app, stood in for while the home service is away; victron: its page shows
// the home's Victron; deye: the building's inverters its page shows (DEYE_BATTERIES at home), by name and serial;
// inverter: the serial of the inverter its diagram shows (krem, Круглик); power: what says whether the address has
// power, for the messages that it is gone or back.
const SOURCES = {
  kem: {
    site: "https://www.dtek-kem.com.ua",
    page: "dtek-schedule.comeodore.services",
    victron: true,
    power: "victron",
    deye: [
      ["elevator", "2407244424"],
      ["water", "2407244735"],
    ],
  },
  krem: {
    site: "https://www.dtek-krem.com.ua",
    page: "dtek-krem-schedule.comeodore.services",
    inverter: "2511090098",
    power: "inverter",
  },
};
// The Deye inverters a source's page shows, and all of them, read together in one call.
const deyeDevices = (source) => [
  ...(SOURCES[source].deye || []).map(([, sn]) => sn),
  ...(SOURCES[source].inverter ? [SOURCES[source].inverter] : []),
];
const ALL_DEYE = Object.keys(SOURCES).flatMap(deyeDevices);
// Its push carries the home service's Deye token (with DEYE_BATTERIES_SOURCE's baseline).
const DEYE_HOLDER = "kem";
const PAGES = Object.fromEntries(
  Object.entries(SOURCES).filter(([, s]) => s.page).map(([name, s]) => [s.page, name]),
);
const WATCHED = ["kem", "krem"];
// What Cloudflare answers for a tunnel or origin that is down: 530 is error 1033, no tunnel connected.
const ORIGIN_DOWN = new Set([502, 503, 504, 520, 521, 522, 523, 524, 530]);
const ORIGIN_TIMEOUT_MS = 8 * 1000;
// The page asks once a minute while its answers carry `reserve`; one dtek-brain call serves it this long.
const PAGE_CACHE_MS = 20 * 1000;
// A page served here has the cron and the object read for it this long, and marks itself at most this often.
const PAGE_WANTED_MS = 10 * 60 * 1000;
const PAGE_MARK_MS = 60 * 1000;
const NO_CACHE = { "cache-control": "no-cache, no-store, must-revalidate" };
// The page's data per source, held in this isolate for PAGE_CACHE_MS.
const PAGE_HELD = new Map();
// The home service pushes every minute; this long without a push and the reserve takes the source over. It
// was 10 until 02.11.2026, which with a read every 5 minutes left news up to 15 minutes late. The same lease,
// counted from any sign of life, holds for the power.
const LEASE_MIN = 5;
const LEASE_MS = LEASE_MIN * 60 * 1000;
// A claim goes to the object only once the pushes are this close to the lease (or the reserve has the turn): before
// that it cannot have taken the source over.
const CLAIM_MARGIN_MS = 60 * 1000;
// The object's next alarm is in D1 (`loop_at`); one this far behind means the loop stopped, and the cron restarts it.
const LOOP_LATE_MS = 2 * 60 * 1000;
// VRM: the GX logs once a minute; a reading older than POWER_FRESH_MS says nothing about the power now.
const VRM_URL = "https://vrmapi.victronenergy.com/v2";
const VRM_TIMEOUT_MS = 15 * 1000;
const POWER_FRESH_MS = 10 * 60 * 1000;
// Two VRM reads at most this far apart are unbroken: a grid that differs between them changed at the later log.
const VRM_UNBROKEN_MS = 3 * 60 * 1000;
// Deye: the logger uploads every ~72 s, and Deye counts calls (the home service makes ~43k of the 100k a month).
const DEYE_EVERY_MS = 90 * 1000;
const DEYE_TIMEOUT_MS = 10 * 1000;
// As DEYE_BASE_URL and DEYE_MIN_GRID_VOLTAGE at home.
const DEYE_BASE_URL = "https://eu1-developer.deyecloud.com";
const DEYE_MIN_GRID_V = 100;
// Deye's tokens live 60 days; one of its own is renewed a day before that.
const DEYE_TOKEN_MARGIN_MS = 24 * 3600 * 1000;
// As MESSAGE_TTL at home, where power messages keep 6 h.
const POWER_TTL_MIN = 6 * 60;
// 5 minutes was the longest interval of the round on 01.11.2026, and reading more often drew no
// fewer challenges.
const INTERVAL_MIN = 5;
// As EVENT_TTL at home: news not delivered by then is dropped rather than sent stale.
const MESSAGE_TTL_MIN = 15;
// Messages sent per run, each a subrequest of its own.
const SEND_PER_RUN = 3;
// Where each turn keeps the days the chats were shown as they are (dtek-brain's `shown`).
const SHOWN = { reserve: "shown", shadow: "shadow_shown" };
// What the object kept before 05.11.2026 and now lives in D1 or nowhere; dropped once (`layout`).
const OBSOLETE = ["home", "power", "next_power", "deye_token", "house", "house_ts", "read_at"];
const LAYOUT = 2;
// Asked for Eastern Europe; the kem object was still placed in Frankfurt. It stays there either way.
const LOCATION_HINT = "eeur";
// Where the object runs and the address it goes out from, logged once an hour.
const TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace";
const TRACE_EVERY_MIN = 60;
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
// Any date before DTEK's current one makes getHomeNum send the whole fact back.
const NO_FACT = "01.01.2020 00:00";
const SITE_TIMEOUT_MS = 15 * 1000;
const TRACE_TIMEOUT_MS = 5 * 1000;
const TELEGRAM_TIMEOUT_MS = 10 * 1000;
const KEEP_DAYS = 30;
// Incapsula's own cookies identify the visitor and survive a CSRF refresh. It sets a session
// cookie per proxy node (incap_ses_<node>_<site>); only the newest few are kept, or a jar that
// lives for weeks would grow without end.
const INCAPSULA_COOKIE = /^(visid_incap|incap_ses)_/;
const MAX_INCAP_SESSIONS = 4;
// Minutes a source rests after Incapsula answers with its JS challenge, doubling while it keeps
// doing so: a challenged visitor that knocks every minute is the one that gets blocked. The next
// read waits for the longer of this and the interval.
const BACKOFF_MIN = [2, 4, 8, 15];

// Minutes until the next read after `strikes` challenges in a row.
export function nextReadMin(strikes) {
  const rest = strikes ? BACKOFF_MIN[Math.min(strikes, BACKOFF_MIN.length) - 1] : 0;
  return Math.max(INTERVAL_MIN, rest);
}

function modeOf(env) {
  return env.MODE === "live" || env.MODE === "test" ? env.MODE : "off";
}

function address(env, source) {
  return source === "kem"
    ? { city: null, street: env.KEM_STREET, building: env.KEM_BUILDING }
    : { city: env.KREM_SETTLEMENT, street: env.KREM_STREET, building: env.KREM_BUILDING };
}

const iso = (ms) => (ms ? new Date(ms).toISOString() : "never");

function json(text) {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
}

// D1 holds what the stateless side needs, so that nothing wakes an object while the home service is alive:
// `home` the home service's last push per source (at: the last good one, alive_at: the last of any kind), `edge` the
// reserve's own per source (since: it has DTEK's news since then; loop_at: its object's next alarm; read: the object's
// last good read; power, power_from: the power as judged here, and since when it is watched here; page_at: a page
// last served), and `kv` the Victron and Deye readings and Deye's token.
function parseHome(row) {
  if (!row) return null;
  return { at: row.at ?? null, alive_at: row.alive_at ?? null, body: json(row.body) || {} };
}

async function rowsFor(env, source) {
  const [home, edge] = await env.DB.batch([
    env.DB.prepare("SELECT at, alive_at, body FROM home WHERE source = ?").bind(source),
    env.DB.prepare("SELECT * FROM edge WHERE source = ?").bind(source),
  ]);
  return { home: parseHome(home.results[0]), edge: edge.results[0] || {} };
}

async function allRows(env) {
  const [homes, edges, kv] = await env.DB.batch([
    env.DB.prepare("SELECT source, at, alive_at, body FROM home"),
    env.DB.prepare("SELECT * FROM edge"),
    env.DB.prepare("SELECT name, value FROM kv"),
  ]);
  return {
    homes: Object.fromEntries(homes.results.map((row) => [row.source, parseHome(row)])),
    edges: Object.fromEntries(edges.results.map((row) => [row.source, row])),
    kv: Object.fromEntries(kv.results.map((row) => [row.name, json(row.value)])),
  };
}

// Sets these columns of a source's `edge` row, adding the row if need be; a statement for .run() or a batch.
function setEdge(env, source, fields) {
  const columns = Object.keys(fields);
  return env.DB.prepare(
    `INSERT INTO edge (source, ${columns.join(", ")}) VALUES (?${", ?".repeat(columns.length)}) ` +
      `ON CONFLICT (source) DO UPDATE SET ${columns.map((c) => `${c} = excluded.${c}`).join(", ")}`,
  ).bind(source, ...columns.map((c) => fields[c] ?? null));
}

function putKv(env, name, value, now) {
  return env.DB.prepare(
    "INSERT INTO kv (name, value, at) VALUES (?, ?, ?) ON CONFLICT (name) DO UPDATE SET value = excluded.value, at = excluded.at",
  ).bind(name, JSON.stringify(value), now);
}

const pageWanted = (edge, now) => Boolean(edge?.page_at && now - edge.page_at < PAGE_WANTED_MS);

// Whether a source's object must read DTEK: always with MODE=test; otherwise while the reserve has the turn, while
// the home service has pushed no good cycle for the lease, and while a page served here is wanted. A home service
// never heard from (no `home` row: a new D1, as at the deploy of 05.11.2026) is not taken for one away. Never while
// PAUSED.
function readsWanted(env, home, edge, now) {
  if (env.PAUSED === "1") return false;
  if (modeOf(env) === "test" || edge.since) return true;
  if (home?.at && now - home.at >= LEASE_MS) return true;
  return pageWanted(edge, now);
}

// Cookie jar is name -> raw value, newest last; Max-Age=0 or a past Expires removes the cookie.
export function merge(jar, response) {
  for (const line of response.headers.getSetCookie()) {
    const [pair, ...attrs] = line.split(";");
    const eq = pair.indexOf("=");
    const name = pair.slice(0, eq).trim();
    const value = pair.slice(eq + 1).trim();
    const gone = attrs.some((attr) => {
      const [key, val = ""] = attr.split("=").map((s) => s.trim());
      if (/^max-age$/i.test(key)) return Number(val) <= 0;
      if (/^expires$/i.test(key)) return Date.parse(val) < Date.now();
      return false;
    });
    delete jar[name];
    if (!gone && value) jar[name] = value;
  }
  const sessions = Object.keys(jar).filter((name) => name.startsWith("incap_ses_"));
  for (const name of sessions.slice(0, -MAX_INCAP_SESSIONS)) delete jar[name];
}

// Incapsula's JS challenge instead of the site's answer.
export function isChallenge(answer) {
  return !answer.json && answer.text.includes("_Incapsula_Resource");
}

function cookieHeader(jar) {
  return Object.entries(jar)
    .map(([name, value]) => `${name}=${value}`)
    .join("; ");
}

// Yii's cookie holds `<hmac>a:2:{i:0;s:N:"_csrf-...";i:1;s:32:"<token>";}`; the header wants
// the token masked: base64url(mask + (mask XOR token)).
export function csrfToken(jar) {
  const name = Object.keys(jar).find((key) => key.startsWith("_csrf"));
  if (!name) return null;
  const match = decodeURIComponent(jar[name]).match(/s:\d+:"([^"]+)";\}$/);
  if (!match) return null;
  const token = new TextEncoder().encode(match[1]);
  const mask = crypto.getRandomValues(new Uint8Array(token.length));
  const masked = new Uint8Array(token.length * 2);
  masked.set(mask);
  token.forEach((byte, i) => {
    masked[token.length + i] = mask[i] ^ byte;
  });
  return btoa(String.fromCharCode(...masked)).replace(/\+/g, "-").replace(/\//g, "_");
}

// The request discon-schedule.js sends: jQuery form encoding, X-Requested-With, X-CSRF-Token.
async function getHomeNum(site, jar, fields, token) {
  const body = new URLSearchParams({ method: "getHomeNum" });
  fields.forEach(([name, value], i) => {
    body.append(`data[${i}][name]`, name);
    body.append(`data[${i}][value]`, value);
  });
  const headers = {
    "user-agent": UA,
    accept: "application/json, text/javascript, */*; q=0.01",
    "accept-language": "uk-UA,uk;q=0.9",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "x-requested-with": "XMLHttpRequest",
    origin: site,
    referer: `${site}/ua/shutdowns`,
    cookie: cookieHeader(jar),
  };
  if (token) headers["x-csrf-token"] = token;
  return within(SITE_TIMEOUT_MS, async (signal) => {
    const response = await fetch(`${site}/ua/ajax`, { method: "POST", body, headers, redirect: "manual", signal });
    merge(jar, response);
    const text = await response.text();
    let json = null;
    try {
      json = JSON.parse(text);
    } catch {}
    return { status: response.status, iinfo: response.headers.get("x-iinfo"), json, text };
  });
}

// Runs `fn(signal)` with a deadline. Not AbortSignal.timeout: its timer runs on after the answer
// came and keeps the Durable Object awake, and billed, until it fires (15 s a read on 02.11.2026).
async function within(ms, fn) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error(`timed out after ${ms} ms`)), ms);
  try {
    return await fn(controller.signal);
  } finally {
    clearTimeout(timer);
  }
}

// Where this object runs and the address it goes out from, from Cloudflare's own trace page.
async function readTrace() {
  try {
    const text = await within(TRACE_TIMEOUT_MS, async (signal) => (await fetch(TRACE_URL, { signal })).text());
    const field = (name) => (text.match(new RegExp(`^${name}=(.*)$`, "m")) || [])[1] || null;
    return { colo: field("colo"), ip: field("ip") };
  } catch {
    return { colo: null, ip: null };
  }
}

// Only the house's groups go to dtek-brain: the whole fact has some 60, and parsing them all costs CPU. The
// names keep one entry when none of the house's groups has a name, so that brain still sees names it lacks, as
// the home service does.
export function forHouse(fact, names, groups) {
  const keep = new Set(groups);
  const only = (byGroup) => Object.fromEntries(Object.entries(byGroup || {}).filter(([group]) => keep.has(group)));
  const data = Array.isArray(fact.data)
    ? fact.data
    : Object.fromEntries(Object.entries(fact.data || {}).map(([day, byGroup]) => [day, only(byGroup)]));
  let houseNames = only(names);
  if (!Object.keys(houseNames).length && Object.keys(names || {}).length) houseNames = { "-": "-" };
  return { fact: { ...fact, data }, names: houseNames };
}

// One message through the Bot API, as the home service's TelegramGateway sends it. The token is in the URL, so
// errors are reported without it.
async function telegram(token, chat, text) {
  try {
    return await within(TELEGRAM_TIMEOUT_MS, async (signal) => {
      const response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: chat, text, parse_mode: "HTML" }),
        signal,
      });
      const data = await response.json().catch(() => ({}));
      if (data.ok) return { ok: true };
      const error = `HTTP ${response.status}: ${String(data.description || "").slice(0, 120)}`;
      if (response.status === 429) return { error, retryAfter: data.parameters?.retry_after ?? 30 };
      // Blocked, chat gone, bad text: asking again changes nothing.
      if (response.status === 400 || response.status === 403) return { error, permanent: true };
      return { error };
    });
  } catch (e) {
    return { error: String(e.name || "Error") + ": " + String(e.message || e).replace(token, "<token>").slice(0, 120) };
  }
}

const KYIV_DATE = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv" });

// A day gone from the answer or an outage notice gone is reported only once it stays gone for a few reads, so
// the reads in between must reach dtek-brain even when nothing else changed.
function confirming(baseline) {
  return Object.keys(baseline?.absent || {}).length > 0 || (baseline?.clear_streak || 0) > 0;
}

// Earliest moment a queued message may be sent, or null when none is queued.
function nextSend(outbox, now) {
  if (!outbox || !outbox.length) return null;
  return Math.min(...outbox.map((item) => Math.max(item.next || 0, now)));
}

// One source, awake only while it is wanted (readsWanted) or has messages to send. It reads, then sets its alarm for
// the next read; with nothing left to do it sets none and sleeps until the cron wakes it. The cookies, the fact, the
// challenge streak and the reserve's turn live in its own storage, so the site sees one visitor from the first read
// on; a new object starts as a new visitor.
export class Watcher extends DurableObject {
  mode() {
    return modeOf(this.env);
  }

  // The next alarm, in D1 for the cron (null: asleep). A failure here must not fail the alarm, which would be retried.
  async marked(source, at) {
    try {
      await setEdge(this.env, source, { loop_at: at }).run();
    } catch (e) {
      console.warn(`${source}: loop_at not kept: ${e}`);
    }
  }

  // Called by the cron while the object is wanted and its loop is not running: starts it.
  async ensure(source) {
    const got = await this.ctx.storage.get(["source", "layout"]);
    if (got.get("source") !== source) await this.ctx.storage.put("source", source);
    if (got.get("layout") !== LAYOUT) {
      await this.ctx.storage.delete(OBSOLETE);
      await this.ctx.storage.put("layout", LAYOUT);
    }
    // An alarm long past never fired: the account's Durable Objects were refused when it was due (04.11.2026, 23:50
    // UTC, and it still stood at 01:40).
    const alarm = await this.ctx.storage.getAlarm();
    if (alarm !== null && alarm > Date.now() - LOOP_LATE_MS) {
      await this.marked(source, alarm);
      return "running";
    }
    const at = Date.now() + 1000;
    await this.ctx.storage.setAlarm(at);
    await this.marked(source, at);
    return alarm === null ? "armed" : `re-armed (its alarm was for ${iso(alarm)})`;
  }

  // Ends the loop; the cron calls it while the PAUSED variable is "1".
  async stop(source) {
    await this.ctx.storage.deleteAlarm();
    await this.marked(source, null);
    return "stopped";
  }

  // POST /claim/<source>, passed on while this object may have the turn: the home service is back and about to
  // detect. If the reserve took the source over meanwhile, it hands over the baseline it went on with, and the home
  // service is in charge again.
  async claim(source) {
    const reserve = this.mode() === "live" ? await this.ctx.storage.get("reserve") : null;
    if (!reserve) return { baseline: null };
    const now = Date.now();
    // Should the home service fail again before its next push, the reserve goes on from here, not from older.
    await this.env.DB.batch([
      this.env.DB.prepare(
        "UPDATE home SET at = ?, body = json_set(coalesce(body, '{}'), '$.baseline', json(?)) WHERE source = ?",
      ).bind(now, JSON.stringify(reserve.baseline), source),
      setEdge(this.env, source, { since: null }),
    ]);
    await this.ctx.storage.delete("reserve");
    console.warn(`${source}: claimed back; the reserve ran from ${iso(reserve.since)}, ${reserve.queued} messages`);
    return { baseline: reserve.baseline, since: reserve.since, queued: reserve.queued };
  }

  // The power messages the cron judged while the home service is away, sent in order with DTEK's news.
  async enqueue(source, items) {
    if ((await this.ctx.storage.get("source")) !== source) await this.ctx.storage.put("source", source);
    const outbox = (await this.ctx.storage.get("outbox")) || [];
    outbox.push(...items);
    await this.ctx.storage.put("outbox", outbox);
    const at = Date.now() + 500;
    const alarm = await this.ctx.storage.getAlarm();
    if (alarm === null || alarm > at) {
      await this.ctx.storage.setAlarm(at);
      await this.marked(source, at);
    }
    return { queued: items.length };
  }

  // POST /status/<source>: what this side knows, for checks; never a token.
  async status(source) {
    const got = await this.ctx.storage.get(["reserve", "shadow", "outbox", "strikes", "next_read", "fact_update", "fact", "shown", "shadow_shown"]);
    const { home, edge } = await rowsFor(this.env, source);
    const now = Date.now();
    const push = home?.body || {};
    const ago = (ms) => (ms ? Math.round((now - ms) / 1000) : null);
    const turn = (state) => state && { since: state.since, updated_at: state.updated_at, queued: state.queued };
    return {
      mode: this.mode(),
      home: home && {
        age_s: ago(home.at),
        heard_s: ago(home.alive_at),
        chats: (push.chat_ids || []).length,
        token: Boolean(push.token),
        lang: push.lang,
        notify_outages: push.notify_outages,
        days: (push.baseline?.days || []).map((d) => d.date),
        outage: Boolean(push.baseline?.outage),
        power: push.power || null,
        deye_token: Boolean(push.deye?.token),
      },
      reserve: turn(got.get("reserve")),
      reserve_since: edge.since || null,
      shadow: turn(got.get("shadow")),
      shown: (got.get("shown") || []).map((d) => d.date),
      shadow_shown: (got.get("shadow_shown") || []).map((d) => d.date),
      outbox: (got.get("outbox") || []).length,
      alarm: await this.ctx.storage.getAlarm(),
      loop_at: edge.loop_at || null,
      strikes: got.get("strikes") || 0,
      next_read: got.get("next_read") || null,
      fact_update: got.get("fact_update") || null,
      fact_kept: Boolean(got.get("fact")),
      read_at: json(edge.read)?.read_at || null,
      power: json(edge.power),
      power_watched_from: edge.power_from || null,
      page_at: edge.page_at || null,
    };
  }

  // POST /test/<source>, MODE=test only: the shadow reserve forgets what the home service has seen ("days",
  // "outage" or "all") or the schedules it showed ("shown", also in "all") and reads at once, so the read
  // reports it to TEST_CHAT_ID as news.
  async test(source, body) {
    if (this.mode() !== "test") return { error: "MODE is not test" };
    const { home } = await rowsFor(this.env, source);
    if (!home?.body?.baseline) return { error: "no push from the home service yet" };
    const baseline = structuredClone(home.body.baseline);
    if (body.forget === "days" || body.forget === "all") Object.assign(baseline, { days: [], absent: {} });
    if (body.forget === "outage" || body.forget === "all") Object.assign(baseline, { outage: null, clear_streak: 0 });
    if (body.forget === "shown" || body.forget === "all") await this.ctx.storage.delete("shadow_shown");
    await this.ctx.storage.put({ source, shadow: { since: Date.now(), baseline, queued: 0 }, next_read: 0 });
    await this.ctx.storage.setAlarm(Date.now() + 500);
    return { ok: true, forgot: body.forget || "nothing" };
  }

  async alarm() {
    const source = await this.ctx.storage.get("source");
    if (!source) return;
    if (this.env.PAUSED === "1") {
      await this.marked(source, null);
      return;
    }
    const { home, edge } = await rowsFor(this.env, source);
    const reads = readsWanted(this.env, home, edge, Date.now());
    let nextRead = (await this.ctx.storage.get("next_read")) || 0;
    try {
      if (reads && Date.now() >= nextRead) {
        const strikes = await this.read(source);
        nextRead = Date.now() + nextReadMin(strikes) * 60 * 1000;
      } else {
        // Messages get runs of their own: a read and a few messages in one could pass the 10 ms of CPU.
        await this.send(source, home);
      }
    } catch (e) {
      console.error(`${source}: ${e}`);
      if (nextRead <= Date.now()) nextRead = Date.now() + INTERVAL_MIN * 60 * 1000;
    } finally {
      await this.ctx.storage.put("next_read", nextRead);
      const send = nextSend(await this.ctx.storage.get("outbox"), Date.now() + 1000);
      const at = Math.min(reads ? nextRead : Infinity, send ?? Infinity);
      if (at === Infinity) {
        await this.marked(source, null);
        console.log(`${source}: nothing to do, asleep until the cron wakes it`);
      } else {
        // An alarm wants a time ahead.
        const next = Math.max(at, Date.now() + 1000);
        await this.ctx.storage.setAlarm(next);
        await this.marked(source, next);
      }
    }
  }

  // One read, logged to D1, its house and the house's part of the fact kept there for the pages and the power
  // messages. Returns the challenges in a row so far.
  async read(source) {
    const { site } = SOURCES[source];
    const where = address(this.env, source);
    const saved = await this.ctx.storage.get(["cookies", "fact_update", "fact", "names", "strikes", "trace_at"]);
    const started = Date.now();
    const row = { at: started, source, interval: INTERVAL_MIN, ok: 0, stage: "cookies", refreshed: 0, challenged: 0, fact_changed: 0 };
    const traceDue = started - (saved.get("trace_at") || 0) >= TRACE_EVERY_MIN * 60 * 1000;
    const traceRead = traceDue ? readTrace() : null;
    const jar = saved.get("cookies") || {};
    // Without the whole fact kept, ask for it again: dtek-brain needs it on every read.
    let fact = saved.get("fact") || null;
    let names = saved.get("names") || {};
    let factUpdate = fact ? saved.get("fact_update") || null : null;
    let strikes = saved.get("strikes") || 0;
    let fresh = false;
    let answer = null;
    let house = null;

    const challenge = (reply) => {
      row.challenged = 1;
      throw new Error(`Incapsula challenge (HTTP ${reply.status})`);
    };
    // A request without a token: the 400 it gets carries a fresh CSRF cookie. Only the site's
    // cookies go; Incapsula's stay, so this is the same visitor and not a new one.
    const refresh = async () => {
      row.stage = "cookies";
      row.refreshed = 1;
      for (const name of Object.keys(jar)) if (!INCAPSULA_COOKIE.test(name)) delete jar[name];
      const first = await getHomeNum(site, jar, [["street", ""]], null);
      row.http = first.status;
      row.iinfo = first.iinfo;
      if (isChallenge(first)) challenge(first);
      const token = csrfToken(jar);
      if (!token) throw new Error(`no CSRF cookie (HTTP ${first.status}): ${first.text.slice(0, 80)}`);
      return token;
    };

    try {
      const fields = [
        ...(where.city ? [["city", where.city]] : []),
        ["street", where.street],
        ["updateFact", factUpdate || NO_FACT],
      ];
      let token = csrfToken(jar) || (await refresh());
      row.stage = "getHomeNum";
      let reply = await getHomeNum(site, jar, fields, token);
      // A token the site rejected gets one new cookie. A challenge does not: asking again at once
      // only makes it worse.
      if (!reply.json && !isChallenge(reply) && !row.refreshed) {
        token = await refresh();
        row.stage = "getHomeNum";
        reply = await getHomeNum(site, jar, fields, token);
      }
      row.http = reply.status;
      row.iinfo = reply.iinfo;
      if (isChallenge(reply)) challenge(reply);
      answer = reply.json;
      if (!answer) throw new Error(`not JSON (HTTP ${reply.status}): ${reply.text.slice(0, 80)}`);
      if (answer.result !== true) throw new Error(`result=${answer.result}: ${String(answer.text || "").slice(0, 80)}`);

      row.stage = "parse";
      house = answer.data && answer.data[where.building];
      if (!house) throw new Error(`house ${where.building} is not in the answer`);
      row.groups = (house.sub_type_reason || []).join(",");
      const reason = String(house.sub_type || "").trim();
      const start = String(house.start_date || "").trim();
      const end = String(house.end_date || "").trim();
      // Same test the site uses to show a notice instead of "no outage".
      row.outage = reason || start || end ? `${start}|${end}` : "";
      row.notice = reason ? `${house.type || ""}:${reason}`.slice(0, 120) : null;
      row.updated_ts = answer.updateTimestamp ? String(answer.updateTimestamp) : null;
      if (answer.fact && typeof answer.fact === "object") {
        row.fact_changed = answer.fact.update !== factUpdate ? 1 : 0;
        fact = answer.fact;
        factUpdate = fact.update;
        if (answer.preset && typeof answer.preset.sch_names === "object") names = answer.preset.sch_names || {};
        fresh = true;
      }
      if (!fact) throw new Error("no fact kept and none in the answer");
      row.fact_update = factUpdate;
      row.ok = 1;
      row.stage = "done";
    } catch (e) {
      row.error = String(e).slice(0, 300);
    }
    row.ms = Date.now() - started;
    if (row.challenged) strikes += 1;
    else if (row.ok) strikes = 0;

    await this.ctx.storage.put({
      cookies: jar,
      fact_update: factUpdate,
      strikes,
      ...(fresh ? { fact, names } : {}),
      ...(traceDue ? { trace_at: started } : {}),
    });
    const kept = row.ok ? forHouse(fact, names, house.sub_type_reason || []) : null;
    if (row.ok) Object.assign(row, await this.turn(source, where.building, answer, house, fact, kept, started));
    if (traceRead) Object.assign(row, await traceRead);
    if (row.iinfo) row.iinfo = row.iinfo.slice(0, 100);

    const columns = Object.keys(row);
    const writes = [
      this.env.DB.prepare(`INSERT INTO runs (${columns.join(", ")}) VALUES (${columns.map(() => "?").join(", ")})`).bind(
        ...columns.map((c) => row[c] ?? null),
      ),
    ];
    if (row.ok) {
      const read = { house, house_ts: answer.updateTimestamp || null, read_at: started, fact: kept.fact, names: kept.names };
      writes.push(setEdge(this.env, source, { read: JSON.stringify(read) }));
    }
    if (fresh && row.fact_changed) {
      writes.push(
        this.env.DB.prepare("INSERT INTO facts (at, source, fact_update, body) VALUES (?, ?, ?, ?)").bind(
          started,
          source,
          factUpdate,
          JSON.stringify(fact),
        ),
      );
    }
    await this.env.DB.batch(writes);
    return strikes;
  }

  // The reserve's part of a good read: nothing while the home service is in charge; otherwise the read goes
  // through dtek-brain and its texts into the outbox. No claim gets in between, so a home service that comes back
  // sees the baseline of exactly what was queued. Returns what the run logs.
  async turn(source, building, answer, house, fact, kept, now) {
    const mode = this.mode();
    if (mode === "off") return { mode };
    if (mode === "test" && !this.env.TEST_CHAT_ID) return { mode, error: "TEST_CHAT_ID is not set" };
    return this.ctx.blockConcurrencyWhile(async () => {
      try {
        const { home, edge } = await rowsFor(this.env, source);
        const push = home?.body || {};
        if (!push.baseline) return { mode: "no push" };
        const homeAge = home.at ? Math.round((now - home.at) / 1000) : null;
        const got = await this.ctx.storage.get(["reserve", "shadow", "outbox", ...Object.values(SHOWN)]);
        if (mode === "live" && homeAge !== null && homeAge < LEASE_MIN * 60) {
          // It pushed without claiming (it could not reach here): from now on it is in charge again, from its own
          // baseline.
          if (got.get("reserve")) {
            await setEdge(this.env, source, { since: null }).run();
            await this.ctx.storage.delete("reserve");
            console.warn(`${source}: the home service pushed without claiming; the reserve stops`);
          }
          return { mode: "home", home_age: homeAge };
        }
        const key = mode === "live" ? "reserve" : "shadow";
        const state = got.get(key);
        const shown = got.get(SHOWN[key]);
        if (key === "reserve" && !edge.since) {
          // Before anything is queued: from now on a claim comes to this object, and waits for this turn.
          await setEdge(this.env, source, { since: state ? state.since : now }).run();
        }
        // The same fact, house and day as the last time leave the baseline and what was shown as they were:
        // dtek-brain is asked only when something changed, since a cold call of it costs well over the 10 ms of
        // CPU. Nothing shown yet (a new turn, or the first read since this was deployed) asks it as well.
        const input = `${fact.update}|${KYIV_DATE.format(now)}|${JSON.stringify(house)}`;
        if (state && state.input === input && !confirming(state.baseline) && shown !== undefined) {
          return { mode: key, home_age: homeAge };
        }
        const reply = JSON.parse(
          await this.env.BRAIN.process(
            JSON.stringify({
              source,
              now,
              building,
              answer: { result: true, data: { [building]: house }, updateTimestamp: answer.updateTimestamp },
              fact: kept.fact,
              names: kept.names,
              baseline: state ? state.baseline : push.baseline,
              fingerprint: push.fingerprint,
              lang: push.lang,
              notify_outages: push.notify_outages,
              shown: shown || [],
            }),
          ),
        );
        const chats = mode === "live" ? push.chat_ids || [] : [this.env.TEST_CHAT_ID];
        const texts = mode === "live" ? reply.texts : reply.texts.map((text) => `🧪 <i>${source}, reserve</i>\n\n${text}`);
        const outbox = got.get("outbox") || [];
        for (const text of texts) {
          for (const chat of chats) outbox.push({ chat: String(chat), text, expires: now + MESSAGE_TTL_MIN * 60 * 1000, attempts: 0 });
        }
        const queued = texts.length * chats.length;
        if (!state) console.warn(`${source}: the ${key} takes over; last push ${homeAge} s ago`);
        await this.ctx.storage.put({
          [key]: { since: state ? state.since : now, baseline: reply.baseline, input, updated_at: now, queued: (state?.queued || 0) + queued },
          [SHOWN[key]]: reply.shown || [],
          outbox,
        });
        return { mode: key, home_age: homeAge, events: reply.events.length, queued };
      } catch (e) {
        return { mode, error: `reserve: ${String(e).slice(0, 280)}` };
      }
    });
  }

  // Up to SEND_PER_RUN queued messages, in order per chat; a chat that has to wait holds back its later ones.
  async send(source, home) {
    const outbox = (await this.ctx.storage.get("outbox")) || [];
    if (!outbox.length) return;
    const token = home?.body?.token;
    const now = Date.now();
    const keep = [];
    const held = new Set();
    let tried = 0;
    for (const item of outbox) {
      if (item.expires <= now) {
        console.error(`${source}: dropping a message to ${item.chat}, not delivered in time`);
        continue;
      }
      if (!token || tried >= SEND_PER_RUN || held.has(item.chat) || (item.next || 0) > now) {
        held.add(item.chat);
        keep.push(item);
        continue;
      }
      tried += 1;
      const result = await telegram(token, item.chat, item.text);
      if (result.ok) continue;
      if (result.permanent) {
        console.error(`${source}: message to ${item.chat} failed for good: ${result.error}`);
        continue;
      }
      item.attempts += 1;
      item.next = now + (result.retryAfter ?? Math.min(10 * 2 ** item.attempts, 600)) * 1000;
      console.warn(`${source}: message to ${item.chat}: ${result.error}; again in ${Math.round((item.next - now) / 1000)} s`);
      held.add(item.chat);
      keep.push(item);
    }
    await this.ctx.storage.put("outbox", keep);
  }
}

// A battery's charge as the power messages show it, as parse_battery at home.
function percent(soc) {
  return Number.isFinite(soc) ? `${Math.round(soc)}%` : "N/A";
}

// --- The Victron, from VRM (svitlo-edge's reading until 05.11.2026, folded in here) ---

// GET a VRM API path as JSON with the access token.
async function vrmGet(env, path) {
  if (!env.VRM_TOKEN) throw new Error("VRM_TOKEN is not set");
  return within(VRM_TIMEOUT_MS, async (signal) => {
    const response = await fetch(VRM_URL + path, { headers: { "x-authorization": `Token ${env.VRM_TOKEN}` }, signal });
    if (!response.ok) {
      await response.body?.cancel();
      throw new Error(`VRM ${path.split("?")[0]}: HTTP ${response.status}`);
    }
    return response.json();
  });
}

// VRM_SITE_ID, else the only installation the token sees (kept in kv).
async function vrmSite(env, kv, writes, now) {
  if (env.VRM_SITE_ID) return env.VRM_SITE_ID;
  if (kv.vrm_site) return kv.vrm_site;
  const me = await vrmGet(env, "/users/me");
  const sites = (await vrmGet(env, `/users/${me.user.id}/installations`)).records || [];
  if (sites.length !== 1) throw new Error(`the VRM token sees ${sites.length} installations; set VRM_SITE_ID`);
  kv.vrm_site = String(sites[0].idSite);
  writes.push(putKv(env, "vrm_site", kv.vrm_site, now));
  return kv.vrm_site;
}

// What is read of the Victron: VRM's graph of the last POWER_FRESH_MS for three attributes of the system service, a
// point per GX log (~600 bytes, against ~100 KB for the diagnostics svitlo-edge read). 471: /Ac/ActiveIn/Source,
// the AC input (0 n/a, 1 grid, 2 generator, 3 shore, 240 inverting); 144: the battery's SOC; 243: its power, positive
// while charging.
const VRM_GRID = "471";
const VRM_SOC = "144";
const VRM_BATTERY_W = "243";
const AC_INPUTS = new Set([0, 1, 2, 3, 240]);

// The newest point of each attribute; a grid value that is none of AC_INPUTS (one averaged over a change) is passed
// over. grid: true, false, or null when there is no point.
export function readGraph(data) {
  const last = (id, ok) => {
    const points = (data && data[id]) || [];
    for (let i = points.length - 1; i >= 0; i--) {
      const [at, value] = points[i];
      if (ok(value)) return { at: at * 1000, value };
    }
    return null;
  };
  const grid = last(VRM_GRID, (v) => AC_INPUTS.has(v));
  const soc = last(VRM_SOC, Number.isFinite);
  const power = last(VRM_BATTERY_W, Number.isFinite);
  return {
    grid: grid ? grid.value === 1 || grid.value === 3 : null,
    value: grid ? grid.value : null,
    grid_at: grid ? grid.at : null,
    soc: soc ? soc.value : null,
    battery_w: power ? power.value : null,
    battery_at: (soc || power)?.at ?? null,
  };
}

// kem's Victron now, kept in kv as `victron`: the grid (and since when, while the reads go on unbroken), the battery;
// the last one kept when VRM cannot be read.
async function readVictron(env, kv, writes, now) {
  const before = kv.victron || null;
  try {
    const site = await vrmSite(env, kv, writes, now);
    const end = Math.floor(now / 1000);
    const ids = [VRM_GRID, VRM_SOC, VRM_BATTERY_W].map((id) => `attributeIds%5B%5D=${id}`).join("&");
    const answer = await vrmGet(env, `/installations/${site}/widgets/Graph?${ids}&instance=0&start=${end - POWER_FRESH_MS / 1000}&end=${end}`);
    const read = readGraph(answer.records?.data);
    const unbroken = before && now - before.at <= VRM_UNBROKEN_MS && typeof before.grid === "boolean" && typeof read.grid === "boolean";
    const since = !unbroken ? null : before.grid === read.grid ? before.grid_since ?? null : read.grid_at;
    const victron = { at: now, ...read, grid_since: since };
    kv.victron = victron;
    writes.push(putKv(env, "victron", victron, now));
    return victron;
  } catch (e) {
    console.warn(`victron: ${e}`);
    return before;
  }
}

// kem's power from the Victron, while its reading is fresh.
function victronPower(victron, now) {
  if (!victron || typeof victron.grid !== "boolean" || !(now - victron.grid_at <= POWER_FRESH_MS)) return null;
  return { on: victron.grid, at: victron.grid_at, battery: percent(victron.soc) };
}

// --- The Deye inverters ---

// device/latest for these inverters' serials; Deye's answer, or one with success false when it gave no JSON.
async function deyeLatest(base, token, serials) {
  return within(DEYE_TIMEOUT_MS, async (signal) => {
    const response = await fetch(`${base}/v1.0/device/latest`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify({ deviceList: serials }),
      signal,
    });
    return response.json().catch(() => ({ success: false, code: `HTTP ${response.status}`, msg: "not JSON" }));
  });
}

// A Deye token of its own, from the home service's credentials in the DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL and
// DEYE_PASSWORD_SHA256 secrets; kept in kv until a day before it expires. Null without them.
async function deyeToken(env, base, kv, writes, now) {
  const { DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL, DEYE_PASSWORD_SHA256 } = env;
  if (!DEYE_APP_ID || !DEYE_APP_SECRET || !DEYE_EMAIL || !DEYE_PASSWORD_SHA256) return null;
  if (kv.deye_token && kv.deye_token.until > now) return kv.deye_token.token;
  const data = await within(DEYE_TIMEOUT_MS, async (signal) => {
    const response = await fetch(`${base}/v1.0/account/token?appId=${encodeURIComponent(DEYE_APP_ID)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ appSecret: DEYE_APP_SECRET, email: DEYE_EMAIL, password: DEYE_PASSWORD_SHA256 }),
      signal,
    });
    return response.json();
  });
  if (!data.success || !data.accessToken) throw new Error(`account/token: ${data.code} ${data.msg}`);
  kv.deye_token = { token: data.accessToken, until: now + Math.max(0, Number(data.expiresIn || 0) * 1000 - DEYE_TOKEN_MARGIN_MS) };
  writes.push(putKv(env, "deye_token", kv.deye_token, now));
  console.log(`deye: a token of its own until ${iso(kv.deye_token.until)}`);
  return data.accessToken;
}

// Every inverter of ALL_DEYE from Deye Cloud (device/latest's deviceDataList) in one call, kept in kv as `deye`, at
// most every DEYE_EVERY_MS; with the token the home service pushed while it is good, else one of its own. The last
// one kept when the call fails.
async function readDeye(env, homes, kv, writes, now) {
  if (kv.deye && now - kv.deye.at < DEYE_EVERY_MS) return kv.deye;
  const pushed = homes[DEYE_HOLDER]?.body?.deye;
  const base = pushed?.base_url || DEYE_BASE_URL;
  try {
    const own = !(pushed?.token && pushed.expires_at > now);
    let token = own ? await deyeToken(env, base, kv, writes, now) : pushed.token;
    let data = token ? await deyeLatest(base, token, ALL_DEYE) : null;
    if (data && !data.success && own) {
      // A token of its own that Deye no longer takes (the home service got a new one, say): one more try.
      kv.deye_token = null;
      token = await deyeToken(env, base, kv, writes, now);
      data = token ? await deyeLatest(base, token, ALL_DEYE) : null;
    }
    if (data && !data.success) throw new Error(`device/latest: ${data.code} ${data.msg}`);
    if (!data) return kv.deye || null;
    kv.deye = { at: now, items: data.deviceDataList || [] };
    writes.push(putKv(env, "deye", kv.deye, now));
  } catch (e) {
    console.warn(`deye: ${e}`);
  }
  return kv.deye || null;
}

// krem: its inverter's grid voltage held against DEYE_MIN_GRID_V, as at home, from a fresh reading.
function inverterPower(deye, source, now) {
  const item = (deye?.items || []).find((it) => it.deviceSn === SOURCES[source].inverter);
  const at = Number(item?.collectionTime) * 1000;
  if (!item || !(now - at <= POWER_FRESH_MS)) return null;
  const values = Object.fromEntries((item.dataList || []).map((point) => [point.key, point.value]));
  const gridV = [values.GridVoltageL1L2, values.GridVoltageL1].map(parseFloat).find(Number.isFinite);
  return { on: gridV !== undefined && gridV >= DEYE_MIN_GRID_V, at, battery: percent(parseFloat(values.SOC)) };
}

// --- The power while the home service is away (from the cron) ---

// Each minute: for every source the home service has not been heard from for the lease (with MODE=live; every
// source with MODE=test), the power at its address, judged against the state it is watched from; a change goes to
// dtek-brain and its message to the source's object. A page served lately has the Victron and Deye read for it too.
async function watchPower(env, now, homes, edges, kv) {
  const mode = modeOf(env);
  const watch = {};
  const wanted = {};
  for (const source of WATCHED) {
    // Never heard from at all: not away, just not known (see readsWanted).
    const heard = homes[source]?.alive_at;
    watch[source] = mode === "test" || (mode === "live" && Boolean(heard) && now - heard >= LEASE_MS);
    wanted[source] = pageWanted(edges[source], now);
  }
  const writes = [];
  const victron = watch.kem || wanted.kem ? await readVictron(env, kv, writes, now) : null;
  const deye = watch.krem || wanted.kem || wanted.krem ? await readDeye(env, homes, kv, writes, now) : null;
  for (const source of WATCHED) {
    const edge = edges[source] || {};
    if (!watch[source]) {
      // Heard from again without claiming the power (a home service of before 05.11.2026): it watches it.
      if (edge.power_from) {
        writes.push(setEdge(env, source, { power_from: null }));
        console.warn(`${source}: the home service is back; the reserve stops watching the power`);
      }
      continue;
    }
    const observed = SOURCES[source].power === "victron" ? victronPower(victron, now) : inverterPower(deye, source, now);
    try {
      await judgePower(env, source, homes[source], edge, observed, now, writes);
    } catch (e) {
      console.error(`${source}: power: ${e}`);
    }
  }
  if (writes.length) await env.DB.batch(writes);
}

// One source's power while it is watched here. A turn starts from the home service's last state (none: the first
// look only sets it, marked `first`, since when it began is not known); a change is worded by dtek-brain with the
// schedule of the object's last read, or of the home service's last baseline when that is newer.
async function judgePower(env, source, home, edge, observed, now, writes) {
  const push = home?.body || {};
  let state = json(edge.power);
  let from = edge.power_from || null;
  if (!from) {
    from = now;
    state = push.power || null;
    const last = state ? `${state.on ? "on" : "off"} since ${iso(state.since)}` : "none";
    console.warn(`${source}: the home service is away; the reserve watches the power (its last state: ${last})`);
  }
  const keep = (next) => {
    if (from !== edge.power_from || JSON.stringify(next) !== edge.power) {
      writes.push(setEdge(env, source, { power: next ? JSON.stringify(next) : null, power_from: from }));
    }
  };
  if (!observed || (state && state.on === observed.on)) return keep(state);
  if (!state) {
    console.warn(`${source}: power first seen ${observed.on ? "on" : "off"} at ${iso(observed.at)}`);
    return keep({ on: observed.on, since: Math.min(observed.at, now), first: true });
  }
  const { building } = address(env, source);
  const read = json(edge.read);
  const context =
    read && (!push.baseline || !home.at || read.read_at >= home.at)
      ? {
          answer: { result: true, data: { [building]: read.house }, updateTimestamp: read.house_ts },
          fact: read.fact,
          names: read.names,
          read_at: read.read_at,
        }
      : push.baseline
        ? { baseline: push.baseline, read_at: home.at }
        : {};
  const reply = JSON.parse(
    await env.BRAIN.power(JSON.stringify({ source, now, lang: push.lang || "uk", building, ...context, state, observed })),
  );
  const test = modeOf(env) === "test";
  const chats = test ? (env.TEST_CHAT_ID ? [env.TEST_CHAT_ID] : []) : push.chat_ids || [];
  const items = [];
  for (const text of reply.texts) {
    const marked = test ? `🧪 <i>${source}, reserve</i>\n\n${text}` : text;
    for (const chat of chats) items.push({ chat: String(chat), text: marked, expires: now + POWER_TTL_MIN * 60 * 1000, attempts: 0 });
  }
  // Queued before the state is kept: rather repeated next minute than lost.
  if (items.length) await watcher(env, source).enqueue(source, items);
  keep(reply.state);
  console.warn(`${source}: power ${observed.on ? "back" : "gone"} at ${iso(observed.at)} (${observed.battery}); ${items.length} messages`);
}

// --- The mini apps ---

// The mini app's /api/v1/schedule while the home service's own is down: dtek-brain's page from the object's last good
// read, with the Victron and the Deye inverters where the source has them, all from D1. A JSON string, or null before
// the first good read. Each ask marks the page wanted, so the cron and the object read for it.
async function pageBody(env, source, now) {
  const [edgeRow, homeRow, kvRows] = await env.DB.batch([
    env.DB.prepare("SELECT read, power, page_at FROM edge WHERE source = ?").bind(source),
    env.DB.prepare("SELECT body FROM home WHERE source = ?").bind(source),
    env.DB.prepare("SELECT name, value FROM kv WHERE name IN ('victron', 'deye')"),
  ]);
  const edge = edgeRow.results[0] || {};
  if (!(edge.page_at > now - PAGE_MARK_MS)) await setEdge(env, source, { page_at: now }).run();
  const read = json(edge.read);
  if (!read) return null;
  const push = json(homeRow.results[0]?.body) || {};
  const kv = Object.fromEntries(kvRows.results.map((row) => [row.name, json(row.value)]));
  const { building } = address(env, source);
  let victron = null;
  if (SOURCES[source].victron && kv.victron) {
    const v = kv.victron;
    // Since when, from the reads themselves, else from the reserve's or the home service's state when it agrees.
    const agreeing = [json(edge.power), push.power].find((p) => p && p.on === v.grid);
    victron = { grid: v.grid, grid_since: v.grid_since ?? agreeing?.since ?? null, grid_at: v.grid_at, soc: v.soc, battery_w: v.battery_w, battery_at: v.battery_at };
  }
  const mine = new Set(deyeDevices(source));
  const deye = mine.size && kv.deye && {
    items: (kv.deye.items || []).filter((item) => mine.has(item.deviceSn)),
    batteries: SOURCES[source].deye || [],
    main: SOURCES[source].inverter || null,
    min_grid_voltage: DEYE_MIN_GRID_V,
  };
  return env.BRAIN.page(
    JSON.stringify({
      source,
      now,
      building,
      answer: { result: true, data: { [building]: read.house }, updateTimestamp: read.house_ts },
      fact: read.fact,
      names: read.names,
      read_at: read.read_at,
      lang: push.lang || "uk",
      victron,
      deye: deye || null,
    }),
  );
}

// A request to a mini app's host: to the home service as always; while that does not answer, the page and its
// data from here.
async function standIn(request, env, source) {
  let origin = null;
  try {
    origin = await within(ORIGIN_TIMEOUT_MS, (signal) => fetch(request, { signal }));
  } catch {}
  if (origin && !ORIGIN_DOWN.has(origin.status)) return origin;
  const { pathname } = new URL(request.url);
  if (request.method === "GET" && pathname === "/") {
    await origin?.body?.cancel();
    return new Response(PAGE_HTML, { headers: { "content-type": "text/html; charset=utf-8", ...NO_CACHE } });
  }
  if (request.method === "GET" && pathname === "/api/v1/schedule") {
    const held = PAGE_HELD.get(source);
    let body = held && Date.now() - held.at < PAGE_CACHE_MS ? held.body : null;
    try {
      if (!body) {
        body = await pageBody(env, source, Date.now());
        if (body) PAGE_HELD.set(source, { at: Date.now(), body });
      }
    } catch (e) {
      console.error(`${source}: page: ${e}`);
    }
    if (body) {
      await origin?.body?.cancel();
      return new Response(body, { headers: { "content-type": "application/json", ...NO_CACHE } });
    }
  }
  return origin || new Response("dtek-service is away", { status: 503, headers: NO_CACHE });
}

// --- The home service's side (app/edge.py) ---

// POST /home/<source>: a push after a good cycle (the whole body kept), or a sign of life after a failed one
// (`alive`: only when it was heard from, and the power state it holds).
async function homePush(env, source, body, now) {
  if (body.alive) {
    const power = body.power ? JSON.stringify(body.power) : null;
    await env.DB.prepare(
      "INSERT INTO home (source, alive_at, body) VALUES (?, ?, ?) ON CONFLICT (source) DO UPDATE SET " +
        "alive_at = excluded.alive_at, body = CASE WHEN ? IS NULL THEN home.body " +
        "ELSE json_set(coalesce(home.body, '{}'), '$.power', json(?)) END",
    ).bind(source, now, power ? JSON.stringify({ power: body.power }) : null, power, power).run();
    return { ok: true };
  }
  await env.DB.prepare(
    "INSERT INTO home (source, at, alive_at, body) VALUES (?, ?, ?, ?) ON CONFLICT (source) DO UPDATE SET " +
      "at = excluded.at, alive_at = excluded.alive_at, body = excluded.body",
  ).bind(source, now, now, JSON.stringify(body)).run();
  return { ok: true };
}

// POST /claim/<source>. With {power: true}, at the home service's start: the power state left here when the power was
// watched here meanwhile (not one only first seen here: its since is not known, and the home service's own is
// better), and it is watched here no more. Else, before a cycle that follows a time away: the baseline the reserve
// went on with, from the object only when it may have the turn.
async function claimSource(env, source, body, now) {
  const { home, edge } = await rowsFor(env, source);
  if (body.power) {
    const watched = edge.power_from ? json(edge.power) : null;
    const left = watched && !watched.first ? { on: watched.on, since: watched.since } : null;
    await env.DB.batch([
      env.DB.prepare(
        "INSERT INTO home (source, alive_at) VALUES (?, ?) ON CONFLICT (source) DO UPDATE SET alive_at = excluded.alive_at",
      ).bind(source, now),
      setEdge(env, source, { power_from: null }),
    ]);
    if (left) console.warn(`${source}: the power claimed back, watched here from ${iso(edge.power_from)}; ${left.on ? "on" : "off"} since ${iso(left.since)}`);
    return { power: left, since: edge.power_from || null };
  }
  const near = !home?.at || now - home.at >= LEASE_MS - CLAIM_MARGIN_MS;
  if (modeOf(env) !== "live" || (!edge.since && !near)) return { baseline: null };
  return watcher(env, source).claim(source);
}

function watcher(env, source) {
  return env.WATCHER.get(env.WATCHER.idFromName(source), { locationHint: LOCATION_HINT });
}

function authorized(request, env) {
  if (!env.EDGE_KEY) return false;
  const given = new TextEncoder().encode(request.headers.get("authorization") || "");
  const wanted = new TextEncoder().encode(`Bearer ${env.EDGE_KEY}`);
  return given.byteLength === wanted.byteLength && crypto.subtle.timingSafeEqual(given, wanted);
}

async function prune(env, now) {
  const cutoff = now - KEEP_DAYS * 24 * 3600 * 1000;
  await env.DB.batch([
    env.DB.prepare("DELETE FROM runs WHERE at < ?").bind(cutoff),
    env.DB.prepare("DELETE FROM facts WHERE at < ?").bind(cutoff),
  ]);
}

export default {
  // Every minute, from D1 alone while the home service is alive: wakes a source's object when it must read DTEK and
  // its loop is not running, and watches the power while the home service is away.
  async scheduled(controller, env) {
    const now = Date.now();
    const { homes, edges, kv } = await allRows(env);
    for (const source of WATCHED) {
      const edge = edges[source] || {};
      try {
        if (env.PAUSED === "1") {
          if (edge.loop_at) console.warn(`${source}: ${await watcher(env, source).stop(source)}`);
          continue;
        }
        if (!readsWanted(env, homes[source], edge, now) || edge.loop_at > now - LOOP_LATE_MS) continue;
        const result = await watcher(env, source).ensure(source);
        const at = homes[source]?.at;
        console.warn(`${source}: the object woken (${result}); the last good push ${at ? Math.round((now - at) / 1000) + " s ago" : "never"}`);
      } catch (e) {
        console.error(`${source}: ${e}`);
      }
    }
    if (env.PAUSED !== "1") {
      try {
        await watchPower(env, now, homes, edges, kv);
      } catch (e) {
        console.error(`power: ${e}`);
      }
    }
    const at = new Date(controller.scheduledTime);
    if (at.getUTCHours() === 3 && at.getUTCMinutes() === 0) await prune(env, now);
  },

  // The mini apps' hosts (standIn), and the home service's side of the turns (app/edge.py), with EDGE_KEY:
  // POST /home|claim|status|test/<source>.
  async fetch(request, env) {
    const url = new URL(request.url);
    const page = PAGES[url.hostname];
    if (page) return standIn(request, env, page);
    const match = url.pathname.match(/^\/(home|claim|status|test)\/([a-z]+)$/);
    if (request.method !== "POST" || !match || !SOURCES[match[2]]) return new Response("not found", { status: 404 });
    if (!authorized(request, env)) return new Response("unauthorized", { status: 401 });
    const [, action, source] = match;
    const body = (await request.json().catch(() => null)) || {};
    const now = Date.now();
    const result =
      action === "home" ? await homePush(env, source, body, now)
      : action === "claim" ? await claimSource(env, source, body, now)
      : action === "test" ? await watcher(env, source).test(source, body)
      : await watcher(env, source).status(source);
    return Response.json(result);
  },
};
