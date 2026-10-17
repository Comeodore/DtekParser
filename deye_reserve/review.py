"""What deye-reserve decided over a day and how the day really went, for a morning review.

    docker exec deye-reserve python -m deye_reserve.review [END]

END (ISO, Kyiv) defaults to 07:00 today: the 24 h before it are one day and
the night after it, i.e. the outages the table had to carry and the night
charge it set up. Two Deye calls: the station history and the TOU table.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, time, timedelta
from typing import Optional

import aiohttp

from deye_reserve.config import load_config
from deye_reserve.deye import DeyeClient
from deye_reserve.inputs import FULL_SOC, rank_models
from deye_reserve.journal import Journal
from deye_reserve.net import HttpSources
from deye_reserve.messages import table_line
from deye_reserve.planner import Params
from deye_reserve.state import load_state, read_json, table_from_list
from deye_reserve.timeutil import KYIV_TZ, now_kyiv


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(KYIV_TZ)


def _hm(t: datetime) -> str:
    return t.astimezone(KYIV_TZ).strftime("%d.%m %H:%M")


def decisions(rows: list[dict]) -> list[str]:
    """The replans that changed something, with what they were based on."""
    out, last = [], None
    for row in rows:
        if row["kind"] == "write":
            out.append(f"{_hm(_t(row['at']))} WRITE {'ok' if row['ok'] else 'FAILED: ' + row['detail']}"
                       f" order {row.get('order')}: {table_line(table_from_list(row['table']))}")
            continue
        if row["kind"] == "verify":
            out.append(f"{_hm(_t(row['at']))} VERIFY order {row.get('order')}: "
                       + (f"shows in the cloud after {row.get('after_min')} min" if row["ok"] else f"FAILED: {row.get('detail')}"))
            continue
        if row["kind"] == "message":
            out.append(f"{_hm(_t(row['at']))} MESSAGE day {row.get('day')} ({row.get('outcome')})")
            continue
        key = (row.get("table"), row.get("fallback"), row.get("outages"), tuple(row.get("outcome") or ()))
        key = repr(key)
        if key == last:
            continue
        last = key
        line = f"{_hm(_t(row['at']))} REPLAN ({row['reason']}) soc {row.get('soc')} grid {row.get('grid')}"
        if row.get("fallback"):
            line += f" FALLBACK: {row['fallback']}"
        else:
            line += (f" night {row.get('night')}% safe {row.get('safe')} kept_previous {row.get('kept_previous')}"
                     f" calibration {row.get('calibration')}")
        line += f"\n    table {table_line(table_from_list(row['table']))} -> {row.get('outcome')}"
        if row.get("outages") is not None:
            outs = ", ".join(f"{_hm(_t(a))}-{_t(b):%H:%M}" for a, b in row["outages"]) or "none"
            line += f"\n    outages {outs}; tomorrow published {row.get('tomorrow_published')} (DTEK {row.get('stamp')})"
        if row.get("forecast"):
            line += "\n    sun P50/pess kWh " + ", ".join(f"{d[8:10]}.{d[5:7]} {v[0]}/{v[1]}" for d, v in row["forecast"].items())
            if row.get("models"):
                line += " (median of " + "; ".join(f"{d[8:10]}.{d[5:7]} {', '.join(m)}" for d, m in row["models"].items()) + ")"
        if row.get("load_kwh"):
            line += f"; load mean/safe {row['load_kwh'][0]}/{row['load_kwh'][1]} kWh/day"
        if row.get("expected"):
            e, p = row["expected"], row["pessimistic"]
            line += (f"\n    expected buy day {e['buy_day']} night {e['buy_night']} kWh, cost* {e['cost_adj']} UAH"
                     f" (90/45: {row.get('baseline_cost_adj')}), lost sun {e['curtailed']} kWh;"
                     f" min SOC in outages exp {e['min_by']} pess {p['min_by']}")
            cands = row.get("candidates") or {}
            line += "\n    candidates " + " ".join(
                f"{n}:{'safe' if c[0] else 'UNSAFE'}/{c[1]}" for n, c in sorted(cands.items(), key=lambda x: int(x[0])))
        out.append(line)
    return out


def actual(items: list[dict], start: datetime, end: datetime, outages: list[tuple[datetime, datetime]],
           params: Params) -> list[str]:
    """The day as Deye recorded it: SOC, grid purchases by tariff, sun, load, and SOC in each outage."""
    points = sorted((datetime.fromtimestamp(float(i["timeStamp"]), KYIV_TZ), i) for i in items)
    points = [(t, i) for t, i in points if start <= t <= end]
    if not points:
        return ["no Deye history for the period"]
    buy = {"day": 0.0, "night": 0.0}
    pv = load = 0.0
    full_minutes = 0
    for (t, i), (t2, _) in zip(points, points[1:]):
        h = (t2 - t).total_seconds() / 3600
        if h > 0.5:
            continue        # a gap in the logger's data
        night = t.hour >= params.night_start_h or t.hour < params.night_end_h
        buy["night" if night else "day"] += (i.get("purchasePower") or 0) / 1000 * h
        pv += (i.get("generationPower") or 0) / 1000 * h
        load += (i.get("consumptionPower") or 0) / 1000 * h
        if (i.get("batterySOC") or 0) >= 99:
            full_minutes += round(h * 60)

    def soc_at(moment: datetime) -> Optional[float]:
        before = [i for t, i in points if t <= moment]
        return before[-1].get("batterySOC") if before else None

    low_t, low = min(((t, i.get("batterySOC")) for t, i in points if i.get("batterySOC") is not None),
                     key=lambda x: x[1], default=(None, None))
    lines = [f"SOC: start {soc_at(start)}%, lowest {low}% at {_hm(low_t) if low_t else '-'}, "
             f"23:00 {soc_at(start.replace(hour=23) if start.hour < 23 else start)}%, end {soc_at(end)}%"
             + (f"; at 99-100% for {full_minutes} min (sun may have been cut)" if full_minutes else ""),
             f"grid bought: day {buy['day']:.2f} kWh, night {buy['night']:.2f} kWh "
             f"= {buy['day'] * params.day_price + buy['night'] * params.night_price:.1f} UAH; "
             f"sun {pv:.2f} kWh; load {load:.2f} kWh"]
    for a, b in outages:
        inside = [i.get("batterySOC") for t, i in points if a <= t < b and i.get("batterySOC") is not None]
        if inside:
            lines.append(f"outage {_hm(a)}-{b:%H:%M}: SOC {inside[0]}% -> lowest {min(inside)}%")
    return lines


def panels_by_hour(items: list[dict], day) -> dict[int, float]:
    """The panels' mean W per hour of `day`, for hours the battery stayed below FULL_SOC (no cut by zero export)."""
    pts: dict[int, list[tuple[float, Optional[float]]]] = {}
    for item in items:
        t = datetime.fromtimestamp(float(item["timeStamp"]), KYIV_TZ)
        if t.date() == day:
            pts.setdefault(t.hour, []).append((float(item.get("generationPower") or 0), item.get("batterySOC")))
    return {h: sum(p for p, _ in v) / len(v) for h, v in pts.items()
            if len(v) >= 10 and all(s is not None and s < FULL_SOC for _, s in v)}


def solcast_lines(rows: list[dict], items: list[dict], day, models: Optional[list] = None) -> list[str]:
    """Solcast's day-ahead (last fetch before midnight) and morning (before 07:00) forecasts against the panels."""
    actual = panels_by_hour(items, day)
    total = sum(float(i.get("generationPower") or 0) for i in items
                if datetime.fromtimestamp(float(i["timeStamp"]), KYIV_TZ).date() == day) * 5 / 60 / 1000
    out = [f"panels {day:%d.%m}: {total:.2f} kWh; {len(actual)} hours usable (battery under {FULL_SOC:.0f}%)"]
    midnight = datetime.combine(day, time(0), KYIV_TZ)
    for name, before in (("day-ahead", midnight), ("morning", midnight + timedelta(hours=7))):
        fetched = [r for r in rows if _t(r["at"]) < before and day.isoformat() in (r.get("days") or {})]
        if not fetched:
            out.append(f"solcast {name}: no forecast")
            continue
        r = fetched[-1]
        p50, p10, p90 = r["days"][day.isoformat()]
        errs = []
        for h, got in actual.items():
            v = r["hours"].get(f"{day.isoformat()}T{h:02d}")
            if v is not None and not (v[0] < 20 and got < 20):
                errs.append(v[0] - got)
        mae = f"{sum(abs(e) for e in errs) / len(errs):.0f} W (bias {sum(errs) / len(errs):+.0f}) over {len(errs)} h" if errs else "-"
        out.append(f"solcast {name} ({_hm(_t(r['at']))}): P50 {p50} / P10 {p10} / P90 {p90} kWh; hourly error {mae}")
    if models:
        out.append("models' day-ahead, same hours: " + ", ".join(f"{s.model} {s.mae_w:.0f} W" for s in models))
    return out


async def review(end: datetime) -> str:
    cfg = load_config()
    start = end - timedelta(hours=24)
    journal = Journal(cfg.data_dir / "journal.jsonl")
    rows = journal.read(since=start - timedelta(hours=12), until=end)
    state = load_state(cfg.data_dir / "state.json")
    outages = {}
    for row in rows:
        for a, b in row.get("outages") or []:
            if _t(b) > start and _t(a) < end:
                outages[_t(a).date(), _t(a)] = (_t(a), _t(b))      # the latest version of each
    out = [f"deye-reserve review {_hm(start)} - {_hm(end)} (write {'ON' if cfg.write else 'off'})", "",
           "== decisions (from 12 h before)"]
    out += decisions(rows) or ["none in the journal"]
    out += ["", "== actual"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        deye = DeyeClient(cfg.base_url, cfg.app_id, cfg.app_secret, cfg.email, cfg.password_sha256, session,
                          cfg.data_dir / "token.json")
        items = []
        try:
            items = await deye.history_power(cfg.station_id, start, end)
            out += actual(items, start, end, sorted(outages.values()), cfg.params)
        except Exception as e:
            out.append(f"history failed: {type(e).__name__}: {e}")
        solcast = Journal(cfg.data_dir / "solcast.jsonl").read(since=start - timedelta(days=2), until=end)
        if solcast and items:
            day = start.date()
            try:
                previous = await HttpSources(cfg, session).previous_runs(day, day)
                models = rank_models(previous, items, cfg.pv_per_ghi, day, day, min_hours=1)
            except Exception as e:
                out.append(f"previous runs failed: {type(e).__name__}: {e}")
                models = None
            out += ["", "== sun: Solcast against the panels"] + solcast_lines(solcast, items, day, models)
        try:
            sn = cfg.device_sn or state.device_sn
            table, _ = await deye.read_tou(sn)
            written = table_from_list(state.written_table)
            out += ["", f"== inverter TOU now: {table_line(table)}",
                    f"   last confirmed write: {table_line(written) if written else '-'}"
                    f" ({'same' if written and tuple(written) == tuple(table) else 'DIFFERENT'})"]
        except Exception as e:
            out.append(f"TOU read failed: {type(e).__name__}: {e}")
    out.append(f"writes per day: {state.writes}; last write: {state.last_write}")
    models = read_json(cfg.data_dir / "models.json")
    if models:
        out.append(f"models by fit to the panels {models.get('days')}: " + ", ".join(
            f"{s['model']} {s['mae_w']} W (bias {s['bias_w']:+}, {s['hours']} h)" for s in models["scores"]))
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) > 1:
        end = datetime.fromisoformat(sys.argv[1])
        end = end if end.tzinfo else end.replace(tzinfo=KYIV_TZ)
    else:
        end = datetime.combine(now_kyiv().date(), time(7), KYIV_TZ)
    print(asyncio.run(review(end)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
