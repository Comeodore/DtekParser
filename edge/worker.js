// dtek-edge: the reserve of dtek-service. It reads the DTEK schedules from a Cloudflare Worker, without a
// browser, and while the home service is away it messages that service's chats in its place.
//
// Why no browser is needed: Incapsula challenges only the HTML page. POST /ua/ajax reaches the
// site, which answers a request without a CSRF token with 400 and sets the Yii2 cookie
// `_csrf-dtek-<site>`. The cookie is signed, not encrypted, so the token is read from it and
// masked the way Yii does (random mask + mask XOR token, base64url) for X-CSRF-Token. With an
// old updateFact the same call also returns the whole schedule (`fact`) and `preset`.
//
// The reading is a polling loop, as in airdanger's edge: one Durable Object per source reads,
// then sets its alarm for the next read. A cron only re-arms the loop if it ever stops. Cron
// runs land in a random data center each time, so the site saw the same visitor hop between
// continents; a Durable Object stays where it was created (Frankfurt, on 02.11.2026).
//
// Taking turns with the home service (its side is app/edge.py); only DTEK's news goes this way, schedules and
// outage notices, while power messages stay at home:
// - after each good cycle the home service pushes its baseline and the source's bot token, chats and language
//   (POST /home/<source>); the pushes are also how this side knows it is alive;
// - with MODE=live a source whose pushes stopped LEASE_MIN ago is taken over: each read goes through
//   dtek-brain (the home service's own detector and texts, brain.py) from the last pushed baseline, and the
//   texts go to the same chats through the same bot;
// - a home service that is back claims the source (POST /claim/<source>) before it detects anything and gets
//   the baseline the reserve went on with, so nothing is sent twice.
// While the reserve is in charge it also sends today's and tomorrow's schedules as they are whenever the chats
// have not seen them yet (taking over, schedules applying again, tomorrow published with no outages): the mini
// app that otherwise shows them is down with the home service. What the chats were shown is kept apart from
// the turn (`shown`), so a home service that comes and goes does not make it send the same again.
// MODE=test runs the reserve on every read from a baseline of its own and sends only to TEST_CHAT_ID, marked,
// without taking the turn from the home service; any other MODE only reads.
//
// The messages that the power is gone or back are this side's alone (05.11.2026), whether the home service runs or
// not: each source's object watches the power at its address (kem: the Victron from svitlo-edge, right after each
// of its VRM reads, about once a minute; krem: its Deye inverter every 90 s), and dtek-brain judges a change and
// words it as the home service did (`power`).
// A home service that pushes `power_messages: false` sends none of them; one of before still sends its own, so
// while it is alive this side stays quiet.
//
// The mini apps' addresses (dtek-schedule / dtek-krem-schedule.comeodore.services, Cloudflare Tunnel to the home
// service) are routed through here too. Every request goes on to the home service as before; only when it does not
// answer (the tunnel or the service down) the page and its /api/v1/schedule come from here instead: the same
// index.html, with data dtek-brain builds from the last read here; for kem the power really there and the
// batteries: the Victron as VRM has it (svitlo-edge, over a service binding) and the building's Deye inverters; for
// krem the inverter diagram. The kem object reads every Deye inverter in one call (Deye counts calls, not devices),
// with the token the home service pushed, else one of its own from the DEYE_* secrets.
//
// Budget on Workers Free, shared with airdanger (see README.md):
// - a Durable Object is billed only while it works (a read, a push, a send), well inside what
//   airdanger's Poller leaves of the free duration;
// - the full fact only when DTEK changed it, and messages in runs of their own, to stay below the
//   10 ms CPU a run gets;
// - one D1 write per read.

import { DurableObject } from "cloudflare:workers";
// Served as the mini app while the home service is away (a Text module).
import PAGE_HTML from "../app/static/index.html";

// page: the host of the source's mini app, stood in for while the home service is away; victron: its page shows
// the home's Victron (svitlo-edge); deye: the building's inverters its page shows (DEYE_BATTERIES at home), by name
// and serial; inverter: the serial of the inverter its diagram shows (krem, Круглик); power: what says whether
// the address has power, for the messages that it is gone or back.
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
// The Deye inverters a source's page shows, and all of them, read together by DEYE_HOLDER's object.
const deyeDevices = (source) => [
  ...(SOURCES[source].deye || []).map(([, sn]) => sn),
  ...(SOURCES[source].inverter ? [SOURCES[source].inverter] : []),
];
const ALL_DEYE = Object.keys(SOURCES).flatMap(deyeDevices);
// It holds the token the home service pushes (with DEYE_BATTERIES_SOURCE's baseline).
const DEYE_HOLDER = "kem";
const PAGES = Object.fromEntries(
  Object.entries(SOURCES).filter(([, s]) => s.page).map(([name, s]) => [s.page, name]),
);
// What Cloudflare answers for a tunnel or origin that is down: 530 is error 1033, no tunnel connected.
const ORIGIN_DOWN = new Set([502, 503, 504, 520, 521, 522, 523, 524, 530]);
const ORIGIN_TIMEOUT_MS = 8 * 1000;
// The page asks every 10 s; one dtek-brain call serves it this long.
const PAGE_CACHE_MS = 20 * 1000;
// The Deye loggers upload every ~72 s.
const DEYE_EVERY_MS = 60 * 1000;
const DEYE_TIMEOUT_MS = 10 * 1000;
// As DEYE_BASE_URL and DEYE_MIN_GRID_VOLTAGE at home.
const DEYE_BASE_URL = "https://eu1-developer.deyecloud.com";
const DEYE_MIN_GRID_V = 100;
// Deye's tokens live 60 days; one of its own is renewed a day before that.
const DEYE_TOKEN_MARGIN_MS = 24 * 3600 * 1000;
const NO_CACHE = { "cache-control": "no-cache, no-store, must-revalidate" };
// The page's data per source, held in this isolate for PAGE_CACHE_MS: an open page asks every 10 s, and each ask that
// reaches the object keeps it awake. The account's free Durable Object time is mostly airdanger's (13,000 GB-s a
// day, airdanger ~10,800), and ran out on 04.11.2026.
const PAGE_HELD = new Map();
// How often the power is looked at: svitlo-edge reads VRM right after each GX log (about a minute apart) and says
// when it reads next, which kem's look follows (POWER_AFTER_MS later; this is the fallback). Deye's logger uploads
// every ~72 s, and Deye counts calls (the home service makes ~43k of the 100k a month; every 90 s here adds ~29k).
const POWER_EVERY_MS = { victron: 60 * 1000, inverter: 90 * 1000 };
const POWER_AFTER_MS = 3 * 1000;
// A reading older than this says nothing about the power now.
const POWER_FRESH_MS = 10 * 60 * 1000;
// As MESSAGE_TTL at home, where power messages kept 6 h.
const POWER_TTL_MIN = 6 * 60;
const WATCHED = ["kem", "krem"];
// 5 minutes was the longest interval of the round on 01.11.2026, and reading more often drew no
// fewer challenges.
const INTERVAL_MIN = 5;
// The home service pushes every minute; this long without a push and the reserve takes the source over. It
// was 10 until 02.11.2026, which with a read every 5 minutes left news up to 15 minutes late.
const LEASE_MIN = 5;
// As EVENT_TTL at home: news not delivered by then is dropped rather than sent stale.
const MESSAGE_TTL_MIN = 15;
// Messages sent per run, each a subrequest of its own.
const SEND_PER_RUN = 3;
// Where each turn keeps the days the chats were shown as they are (dtek-brain's `shown`).
const SHOWN = { reserve: "shown", shadow: "shadow_shown" };
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

function address(env, source) {
  return source === "kem"
    ? { city: null, street: env.KEM_STREET, building: env.KEM_BUILDING }
    : { city: env.KREM_SETTLEMENT, street: env.KREM_STREET, building: env.KREM_BUILDING };
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

// One source. It reads, then sets its alarm for the next read. The cookies, the fact, the challenge streak,
// the home service's last push and the reserve's turn live in its own storage, so the site sees one visitor
// from the first read on; a new object starts as a new visitor.
export class Watcher extends DurableObject {
  mode() {
    return this.env.MODE === "live" || this.env.MODE === "test" ? this.env.MODE : "off";
  }

  // Called by the cron: starts the loop, or restarts it if it ever stopped.
  async ensure(source) {
    if ((await this.ctx.storage.get("source")) !== source) await this.ctx.storage.put("source", source);
    if ((await this.ctx.storage.getAlarm()) !== null) return "running";
    await this.ctx.storage.setAlarm(Date.now() + 1000);
    return "armed";
  }

  // Ends the loop; the cron calls it while the PAUSED variable is "1".
  async stop() {
    await this.ctx.storage.deleteAlarm();
    return "stopped";
  }

  // POST /home/<source>: the home service's push after a good cycle.
  async home(source, push) {
    await this.ctx.storage.put({ source, home: { ...push, at: Date.now() } });
    // A home service that is back claims first. One that pushes while the reserve still runs did not claim
    // (it could not reach here): from now on it is in charge again, from its own baseline.
    if (this.mode() === "live" && (await this.ctx.storage.get("reserve"))) {
      await this.ctx.storage.delete("reserve");
      console.warn(`${source}: the home service pushed without claiming; the reserve stops`);
    }
    return { ok: true };
  }

  // POST /claim/<source>: the home service is back and about to detect. If the reserve took the source over
  // meanwhile, it hands over the baseline it went on with, and the home service is in charge again.
  async claim(source) {
    const reserve = this.mode() === "live" ? await this.ctx.storage.get("reserve") : null;
    if (!reserve) return { baseline: null };
    const home = (await this.ctx.storage.get("home")) || {};
    // Should the home service fail again before its next push, the reserve goes on from here, not from older.
    await this.ctx.storage.put("home", { ...home, baseline: reserve.baseline, at: Date.now() });
    await this.ctx.storage.delete("reserve");
    console.warn(`${source}: claimed back; the reserve ran from ${new Date(reserve.since).toISOString()}, ${reserve.queued} messages`);
    return { baseline: reserve.baseline, since: reserve.since, queued: reserve.queued };
  }

  // POST /status/<source>: what this side knows, for checks; never the token.
  async status() {
    const got = await this.ctx.storage.get(["home", "reserve", "shadow", "outbox", "strikes", "next_read", "fact_update", "fact", "shown", "shadow_shown", "power"]);
    const home = got.get("home");
    const turn = (state) => state && { since: state.since, updated_at: state.updated_at, queued: state.queued };
    return {
      mode: this.mode(),
      home: home && {
        age_s: Math.round((Date.now() - home.at) / 1000),
        chats: (home.chat_ids || []).length,
        token: Boolean(home.token),
        lang: home.lang,
        notify_outages: home.notify_outages,
        days: (home.baseline?.days || []).map((d) => d.date),
        outage: Boolean(home.baseline?.outage),
      },
      reserve: turn(got.get("reserve")),
      shadow: turn(got.get("shadow")),
      shown: (got.get("shown") || []).map((d) => d.date),
      shadow_shown: (got.get("shadow_shown") || []).map((d) => d.date),
      outbox: (got.get("outbox") || []).length,
      strikes: got.get("strikes") || 0,
      next_read: got.get("next_read") || null,
      fact_update: got.get("fact_update") || null,
      fact_kept: Boolean(got.get("fact")),
      power: got.get("power") || null,
    };
  }

  // POST /test/<source>, MODE=test only: the shadow reserve forgets what the home service has seen ("days",
  // "outage" or "all") or the schedules it showed ("shown", also in "all") and reads at once, so the read
  // reports it to TEST_CHAT_ID as news.
  async test(source, body) {
    if (this.mode() !== "test") return { error: "MODE is not test" };
    const home = await this.ctx.storage.get("home");
    if (!home?.baseline) return { error: "no push from the home service yet" };
    const baseline = structuredClone(home.baseline);
    if (body.forget === "days" || body.forget === "all") Object.assign(baseline, { days: [], absent: {} });
    if (body.forget === "outage" || body.forget === "all") Object.assign(baseline, { outage: null, clear_streak: 0 });
    if (body.forget === "shown" || body.forget === "all") await this.ctx.storage.delete("shadow_shown");
    await this.ctx.storage.put({ shadow: { since: Date.now(), baseline, queued: 0 }, next_read: 0 });
    await this.ctx.storage.setAlarm(Date.now() + 500);
    return { ok: true, forgot: body.forget || "nothing" };
  }

  async alarm() {
    if (this.env.PAUSED === "1") return;
    const source = await this.ctx.storage.get("source");
    let nextRead = (await this.ctx.storage.get("next_read")) || 0;
    let nextPower = (await this.ctx.storage.get("next_power")) || 0;
    const every = POWER_EVERY_MS[SOURCES[source]?.power] || null;
    try {
      if (Date.now() >= nextRead) {
        const strikes = await this.read(source);
        nextRead = Date.now() + nextReadMin(strikes) * 60 * 1000;
      } else if (every && Date.now() >= nextPower) {
        nextPower = Date.now() + every;
        nextPower = (await this.watchPower(source)) || nextPower;
      } else {
        // Messages get runs of their own: a read and a few messages in one could pass the 10 ms of CPU.
        await this.send(source);
      }
    } catch (e) {
      console.error(`${source}: ${e}`);
      if (nextRead <= Date.now()) nextRead = Date.now() + INTERVAL_MIN * 60 * 1000;
    } finally {
      await this.ctx.storage.put({ next_read: nextRead, next_power: nextPower });
      const send = nextSend(await this.ctx.storage.get("outbox"), Date.now() + 1000);
      // A power look not made yet is due at once; an alarm wants a time ahead.
      const at = Math.min(nextRead, every ? nextPower : Infinity, send ?? Infinity);
      await this.ctx.storage.setAlarm(Math.max(at, Date.now() + 1000));
    }
  }

  // The power at the source's address. On a change (or at first) dtek-brain judges it and words the message as the
  // home service did; the message goes to the chats as DTEK's news does, kept 6 h. Returns when to look next, when
  // the source says (svitlo-edge's next read), else null.
  async watchPower(source) {
    const now = Date.now();
    const observed = SOURCES[source].power === "victron" ? await this.victronPower(now) : await this.inverterPower(source, now);
    const next = observed?.next > now && observed.next < now + 3 * 60 * 1000 ? observed.next + POWER_AFTER_MS : null;
    if (!observed?.at) return next;
    const got = await this.ctx.storage.get(["power", "home", "house", "house_ts", "read_at", "fact", "names", "outbox"]);
    const power = got.get("power");
    if (power && power.on === observed.on) return next;
    const home = got.get("home") || {};
    const house = got.get("house");
    const fact = got.get("fact");
    const { building } = address(this.env, source);
    const read = house && fact && got.get("read_at");
    const forBrain = read ? forHouse(fact, got.get("names") || {}, house.sub_type_reason || []) : null;
    const reply = JSON.parse(
      await this.env.BRAIN.power(
        JSON.stringify({
          source,
          now,
          lang: home.lang || "uk",
          building,
          ...(read
            ? {
                answer: { result: true, data: { [building]: house }, updateTimestamp: got.get("house_ts") },
                fact: forBrain.fact,
                names: forBrain.names,
                read_at: got.get("read_at"),
              }
            : {}),
          state: power || null,
          observed,
        }),
      ),
    );
    const chats = this.powerChats(home, now);
    const outbox = got.get("outbox") || [];
    for (const text of reply.texts) {
      const marked = this.mode() === "test" ? `🧪 <i>${source}, reserve</i>\n\n${text}` : text;
      for (const chat of chats) outbox.push({ chat: String(chat), text: marked, expires: now + POWER_TTL_MIN * 60 * 1000, attempts: 0 });
    }
    await this.ctx.storage.put({ power: reply.state, outbox });
    const what = power ? (observed.on ? "back" : "gone") : `first seen ${observed.on ? "on" : "off"}`;
    console.warn(`${source}: power ${what} at ${new Date(observed.at).toISOString()} (${observed.battery}); ${reply.texts.length * chats.length} messages`);
    return next;
  }

  // Who gets the power messages: the source's chats, always once the home service says it sends none, else only
  // while it is away (a home service of before sends its own); TEST_CHAT_ID with MODE=test; nobody otherwise.
  powerChats(home, now) {
    const mode = this.mode();
    if (mode === "test") return this.env.TEST_CHAT_ID ? [this.env.TEST_CHAT_ID] : [];
    if (mode !== "live") return [];
    const away = !home.at || now - home.at >= LEASE_MIN * 60 * 1000;
    return home.power_messages === false || away ? home.chat_ids || [] : [];
  }

  // kem: svitlo-edge's grid while its data is fresh, since the GX log that first showed it, and when it reads next
  // (alone, with no `at`, when the data is not fresh).
  async victronPower(now) {
    const v = await this.victron();
    if (!v) return null;
    if (v.grid === null || v.grid === undefined || !(now - v.grid_at <= POWER_FRESH_MS)) return { next: v.next_read_at };
    return { on: Boolean(v.grid), at: v.grid_since || v.grid_at, battery: percent(v.soc), next: v.next_read_at };
  }

  // krem: its inverter's grid voltage held against DEYE_MIN_GRID_V, as at home, from a fresh reading.
  async inverterPower(source, now) {
    const item = (await this.deyeFor(source))?.find((it) => it.deviceSn === SOURCES[source].inverter);
    const at = Number(item?.collectionTime) * 1000;
    if (!item || !(now - at <= POWER_FRESH_MS)) return null;
    const values = Object.fromEntries((item.dataList || []).map((point) => [point.key, point.value]));
    const gridV = [values.GridVoltageL1L2, values.GridVoltageL1].map(parseFloat).find(Number.isFinite);
    return { on: gridV !== undefined && gridV >= DEYE_MIN_GRID_V, at, battery: percent(parseFloat(values.SOC)) };
  }

  // One read, logged to D1. Returns the challenges in a row so far.
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
      // The last good read's house, for the page while the home service is away.
      ...(row.ok ? { house, house_ts: answer.updateTimestamp || null, read_at: started } : {}),
    });
    if (row.ok) Object.assign(row, await this.turn(source, where.building, answer, house, fact, names, started));
    if (traceRead) Object.assign(row, await traceRead);
    if (row.iinfo) row.iinfo = row.iinfo.slice(0, 100);

    const columns = Object.keys(row);
    const writes = [
      this.env.DB.prepare(`INSERT INTO runs (${columns.join(", ")}) VALUES (${columns.map(() => "?").join(", ")})`).bind(
        ...columns.map((c) => row[c] ?? null),
      ),
    ];
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
  // through dtek-brain and its texts into the outbox. No push or claim gets in between, so a home service that
  // comes back sees the baseline of exactly what was queued. Returns what the run logs.
  async turn(source, building, answer, house, fact, names, now) {
    const mode = this.mode();
    if (mode === "off") return { mode };
    if (mode === "test" && !this.env.TEST_CHAT_ID) return { mode, error: "TEST_CHAT_ID is not set" };
    return this.ctx.blockConcurrencyWhile(async () => {
      try {
        const got = await this.ctx.storage.get(["home", "reserve", "shadow", "outbox", ...Object.values(SHOWN)]);
        const home = got.get("home");
        if (!home) return { mode: "no push" };
        const homeAge = Math.round((now - home.at) / 1000);
        if (mode === "live" && homeAge < LEASE_MIN * 60) return { mode: "home", home_age: homeAge };
        const key = mode === "live" ? "reserve" : "shadow";
        const state = got.get(key);
        const shown = got.get(SHOWN[key]);
        // The same fact, house and day as the last time leave the baseline and what was shown as they were:
        // dtek-brain is asked only when something changed, since a cold call of it costs well over the 10 ms of
        // CPU. Nothing shown yet (a new turn, or the first read since this was deployed) asks it as well.
        const input = `${fact.update}|${KYIV_DATE.format(now)}|${JSON.stringify(house)}`;
        if (state && state.input === input && !confirming(state.baseline) && shown !== undefined) {
          return { mode: key, home_age: homeAge };
        }
        const forBrain = forHouse(fact, names, house.sub_type_reason || []);
        const reply = JSON.parse(
          await this.env.BRAIN.process(
            JSON.stringify({
              source,
              now,
              building,
              answer: { result: true, data: { [building]: house }, updateTimestamp: answer.updateTimestamp },
              fact: forBrain.fact,
              names: forBrain.names,
              baseline: state ? state.baseline : home.baseline,
              fingerprint: home.fingerprint,
              lang: home.lang,
              notify_outages: home.notify_outages,
              shown: shown || [],
            }),
          ),
        );
        const chats = mode === "live" ? home.chat_ids || [] : [this.env.TEST_CHAT_ID];
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

  // The mini app's /api/v1/schedule while the home service is away: dtek-brain's page from the last good read,
  // with the Victron (svitlo-edge) and the Deye inverters where the source has them. A JSON string, or null before
  // the first good read.
  async page(source) {
    const now = Date.now();
    if (this.pageCache && now - this.pageCache.at < PAGE_CACHE_MS) return this.pageCache.body;
    const got = await this.ctx.storage.get(["house", "house_ts", "read_at", "fact", "names", "home"]);
    const house = got.get("house");
    const fact = got.get("fact");
    const home = got.get("home") || {};
    if (!house || !fact || !got.get("read_at")) return null;
    const { building } = address(this.env, source);
    const forBrain = forHouse(fact, got.get("names") || {}, house.sub_type_reason || []);
    const [victron, items] = await Promise.all([
      SOURCES[source].victron ? this.victron() : null,
      deyeDevices(source).length ? this.deyeFor(source) : null,
    ]);
    const deye = items && {
      items,
      batteries: SOURCES[source].deye || [],
      main: SOURCES[source].inverter || null,
      min_grid_voltage: DEYE_MIN_GRID_V,
    };
    const body = await this.env.BRAIN.page(
      JSON.stringify({
        source,
        now,
        building,
        answer: { result: true, data: { [building]: house }, updateTimestamp: got.get("house_ts") },
        fact: forBrain.fact,
        names: forBrain.names,
        read_at: got.get("read_at"),
        lang: home.lang || "uk",
        victron,
        deye,
      }),
    );
    this.pageCache = { at: now, body };
    return body;
  }

  // svitlo-edge's last VRM read, as dtek-brain's power_now takes it; null when it cannot be had.
  async victron() {
    try {
      const p = await this.env.POWER.now();
      return {
        grid: p.grid,
        grid_since: p.gridSince,
        grid_at: p.gridAt,
        soc: p.soc,
        battery_w: p.batteryW,
        battery_at: p.batteryAt,
        next_read_at: p.nextReadAt,
      };
    } catch (e) {
      console.warn(`victron: ${e}`);
      return null;
    }
  }

  // This source's Deye inverters, from DEYE_HOLDER's last read of them all; null when there is none.
  async deyeFor(source) {
    try {
      const items = source === DEYE_HOLDER ? await this.deyeItems() : await watcher(this.env, DEYE_HOLDER).deyeItems();
      const mine = new Set(deyeDevices(source));
      return items && items.filter((item) => mine.has(item.deviceSn));
    } catch (e) {
      console.warn(`${source}: deye: ${e}`);
      return null;
    }
  }

  // DEYE_HOLDER's object: every inverter of ALL_DEYE from Deye Cloud (device/latest's deviceDataList), in one call
  // at most once a minute, with the token the home service pushed while it is good, else with one of its own
  // (deyeToken). Null without a token, or when the call fails.
  async deyeItems() {
    const now = Date.now();
    if (this.deyeCache && now - this.deyeCache.at < DEYE_EVERY_MS) return this.deyeCache.items;
    const pushed = (await this.ctx.storage.get("home"))?.deye;
    const base = pushed?.base_url || DEYE_BASE_URL;
    let items = null;
    try {
      const own = !(pushed?.token && pushed.expires_at > now);
      let token = own ? await this.deyeToken(base, now) : pushed.token;
      let data = token ? await deyeLatest(base, token, ALL_DEYE) : null;
      if (data && !data.success && own) {
        // A token of its own that Deye no longer takes (the home service got a new one, say): one more try.
        await this.ctx.storage.delete("deye_token");
        token = await this.deyeToken(base, now);
        data = await deyeLatest(base, token, ALL_DEYE);
      }
      if (data && !data.success) throw new Error(`device/latest: ${data.code} ${data.msg}`);
      if (data) items = data.deviceDataList || [];
    } catch (e) {
      console.warn(`deye: ${e}`);
    }
    this.deyeCache = { at: now, items };
    return items;
  }

  // A Deye token of its own, from the home service's credentials in the DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL and
  // DEYE_PASSWORD_SHA256 secrets; kept until a day before it expires. Null without them.
  async deyeToken(base, now) {
    const { DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL, DEYE_PASSWORD_SHA256 } = this.env;
    if (!DEYE_APP_ID || !DEYE_APP_SECRET || !DEYE_EMAIL || !DEYE_PASSWORD_SHA256) return null;
    const saved = await this.ctx.storage.get("deye_token");
    if (saved && saved.until > now) return saved.token;
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
    const until = now + Math.max(0, Number(data.expiresIn || 0) * 1000 - DEYE_TOKEN_MARGIN_MS);
    await this.ctx.storage.put("deye_token", { token: data.accessToken, until });
    console.log(`deye: a token of its own until ${new Date(until).toISOString()}`);
    return data.accessToken;
  }

  // Up to SEND_PER_RUN queued messages, in order per chat; a chat that has to wait holds back its later ones.
  async send(source) {
    const got = await this.ctx.storage.get(["outbox", "home"]);
    const token = got.get("home")?.token;
    const now = Date.now();
    const keep = [];
    const held = new Set();
    let tried = 0;
    for (const item of got.get("outbox") || []) {
      if (item.expires <= now) {
        console.error(`${source}: dropping a message to ${item.chat}, not delivered in ${MESSAGE_TTL_MIN} min`);
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
        body = await watcher(env, source).page(source);
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
  // Every 10 minutes: keeps each source's loop running (or stopped while PAUSED is "1").
  async scheduled(controller, env) {
    for (const source of WATCHED) {
      const stub = watcher(env, source);
      const result = env.PAUSED === "1" ? await stub.stop() : await stub.ensure(source);
      if (result !== "running") console.warn(`${source}: ${result}`);
    }
    const now = controller.scheduledTime;
    if (new Date(now).getUTCHours() === 3 && Math.floor(now / 60000) % 60 === 0) await prune(env, now);
  },

  // The mini apps' hosts (standIn), and the home service's side of the turns (app/edge.py), with EDGE_KEY:
  // POST /home|claim|status|test/<source>.
  async fetch(request, env) {
    const page = PAGES[new URL(request.url).hostname];
    if (page) return standIn(request, env, page);
    const match = new URL(request.url).pathname.match(/^\/(home|claim|status|test)\/([a-z]+)$/);
    if (request.method !== "POST" || !match || !SOURCES[match[2]]) return new Response("not found", { status: 404 });
    if (!authorized(request, env)) return new Response("unauthorized", { status: 401 });
    const [, action, source] = match;
    const body = action === "home" || action === "test" ? await request.json() : {};
    const stub = watcher(env, source);
    const result =
      action === "home" ? await stub.home(source, body)
      : action === "claim" ? await stub.claim(source)
      : action === "test" ? await stub.test(source, body)
      : await stub.status();
    return Response.json(result);
  },
};
