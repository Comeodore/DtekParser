// dtek-watch-test: the reserve of dtek-service. It reads the DTEK schedules from a Cloudflare Worker, without a
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
// MODE=test runs the reserve on every read from a baseline of its own and sends only to TEST_CHAT_ID, marked,
// without taking the turn from the home service; any other MODE only reads.
//
// Budget on Workers Free, shared with airdanger (see README.md):
// - a Durable Object is billed only while it works (a read, a push, a send), well inside what
//   airdanger's Poller leaves of the free duration;
// - the full fact only when DTEK changed it, and messages in runs of their own, to stay below the
//   10 ms CPU a run gets;
// - one D1 write per read.

import { DurableObject } from "cloudflare:workers";

const SOURCES = {
  kem: { site: "https://www.dtek-kem.com.ua" },
  krem: { site: "https://www.dtek-krem.com.ua" },
};
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
    const got = await this.ctx.storage.get(["home", "reserve", "shadow", "outbox", "strikes", "next_read", "fact_update", "fact"]);
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
      outbox: (got.get("outbox") || []).length,
      strikes: got.get("strikes") || 0,
      next_read: got.get("next_read") || null,
      fact_update: got.get("fact_update") || null,
      fact_kept: Boolean(got.get("fact")),
    };
  }

  // POST /test/<source>, MODE=test only: the shadow reserve forgets what the home service has seen ("days",
  // "outage" or "all") and reads at once, so the read reports it to TEST_CHAT_ID as news.
  async test(source, body) {
    if (this.mode() !== "test") return { error: "MODE is not test" };
    const home = await this.ctx.storage.get("home");
    if (!home?.baseline) return { error: "no push from the home service yet" };
    const baseline = structuredClone(home.baseline);
    if (body.forget === "days" || body.forget === "all") Object.assign(baseline, { days: [], absent: {} });
    if (body.forget === "outage" || body.forget === "all") Object.assign(baseline, { outage: null, clear_streak: 0 });
    await this.ctx.storage.put({ shadow: { since: Date.now(), baseline, queued: 0 }, next_read: 0 });
    await this.ctx.storage.setAlarm(Date.now() + 500);
    return { ok: true, forgot: body.forget || "nothing" };
  }

  async alarm() {
    if (this.env.PAUSED === "1") return;
    const source = await this.ctx.storage.get("source");
    let nextRead = (await this.ctx.storage.get("next_read")) || 0;
    try {
      if (Date.now() >= nextRead) {
        const strikes = await this.read(source);
        nextRead = Date.now() + nextReadMin(strikes) * 60 * 1000;
      } else {
        // Messages get runs of their own: a read and a few messages in one could pass the 10 ms of CPU.
        await this.send(source);
      }
    } catch (e) {
      console.error(`${source}: ${e}`);
      if (nextRead <= Date.now()) nextRead = Date.now() + INTERVAL_MIN * 60 * 1000;
    } finally {
      await this.ctx.storage.put("next_read", nextRead);
      const send = nextSend(await this.ctx.storage.get("outbox"), Date.now() + 1000);
      await this.ctx.storage.setAlarm(send === null ? nextRead : Math.min(nextRead, send));
    }
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
        const got = await this.ctx.storage.get(["home", "reserve", "shadow", "outbox"]);
        const home = got.get("home");
        if (!home) return { mode: "no push" };
        const homeAge = Math.round((now - home.at) / 1000);
        if (mode === "live" && homeAge < LEASE_MIN * 60) return { mode: "home", home_age: homeAge };
        const key = mode === "live" ? "reserve" : "shadow";
        const state = got.get(key);
        // The same fact, house and day as the last time leave the baseline as it was: dtek-brain is asked only
        // when something changed, since a cold call of it costs well over the 10 ms of CPU.
        const input = `${fact.update}|${KYIV_DATE.format(now)}|${JSON.stringify(house)}`;
        if (state && state.input === input && !confirming(state.baseline)) return { mode: key, home_age: homeAge };
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
          outbox,
        });
        return { mode: key, home_age: homeAge, events: reply.events.length, queued };
      } catch (e) {
        return { mode, error: `reserve: ${String(e).slice(0, 280)}` };
      }
    });
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

  // The home service's side of the turns (app/edge.py), with EDGE_KEY: POST /home|claim|status|test/<source>.
  async fetch(request, env) {
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
