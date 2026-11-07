"""dtek-brain: dtek-service's own detection and texts (app/brain.py), for the reserve in worker.js.

A Python Worker that bundle.py packs with the app modules app/brain.py imports; worker.js calls `process` (each
read), `power` (the power gone or back while the home service is away) and `page` (the mini app, while the home
service's is down) over a service binding. Python Workers come without tzdata, so Europe/Kyiv is bundled as its tz file.
"""
import base64
import json
import os
import zoneinfo

import kyiv_tzif

TZ_DIR = "/tmp/zoneinfo"
os.makedirs(f"{TZ_DIR}/Europe", exist_ok=True)
with open(f"{TZ_DIR}/Europe/Kyiv", "wb") as tz_file:
    tz_file.write(base64.b64decode(kyiv_tzif.DATA))
zoneinfo.reset_tzpath(to=[TZ_DIR])

from workers import Response, WorkerEntrypoint  # noqa: E402

from app import brain  # noqa: E402


def warm_up() -> None:
    """One pass through everything a read can touch (strptime, the regexes, both languages' texts). Python
    Workers snapshot the interpreter after this module runs, so a cold call starts warm: the first call of the
    first deploy took 173 ms of CPU, against a 10 ms limit."""
    day = 1790802000    # 01.10.2026 00:00 in Kyiv
    hours = {str(h): "no" if h in (9, 10) else "yes" for h in range(1, 25)}
    house = {"sub_type_reason": ["GPV1.1"], "type": "2", "sub_type": "Екстренні відключення",
             "start_date": "07:00 01.10.2026", "end_date": "12:00 01.10.2026"}
    request = {
        "source": "-", "now": (day + 8 * 3600) * 1000, "building": "1",
        "answer": {"result": True, "updateTimestamp": "01.10.2026 07:00", "data": {"1": house}},
        "fact": {"update": "01.10.2026 07:00", "today": day, "data": {str(day): {"GPV1.1": hours}}},
        "names": {"GPV1.1": "Черга 1.1"}, "baseline": None, "fingerprint": "-", "lang": "uk", "notify_outages": True,
        "shown": [],
    }
    first = brain.process(json.loads(json.dumps(request)))
    for lang in ("uk", "en"):
        json.dumps(brain.process(dict(request, lang=lang, baseline=dict(first["baseline"], days=[], outage=None))))
        victron = {"grid": False, "grid_since": request["now"] - 3600_000, "grid_at": request["now"],
                   "soc": 80, "battery_w": -300, "battery_at": request["now"]}
        item = {"deviceSn": "1", "collectionTime": request["now"] // 1000, "dataList": [
            {"key": key, "value": value} for key, value in (
                ("SOC", "50"), ("BatteryPower", "-300"), ("GridVoltageL1L2", "0"), ("TotalConsumptionPower", "400"),
                ("TotalDCInputPower", "0"), ("TotalGridPower", "0"), ("DCPowerPV1", "0"), ("DCVoltagePV1", "0"),
                ("DailyEnergyPurchased", "1.5"))]}
        deye = {"items": [item], "batteries": [["elevator", "1"]], "main": "1", "min_grid_voltage": 100}
        json.dumps(brain.page(dict(request, lang=lang, read_at=request["now"], victron=victron, deye=deye)))
        # krem's diagram, with the grid by tariff and the sun forecast the home service pushed.
        inverter = {"grid_day": {"day": "2026-10-01", "marks": {"7": 1.0}, "night_start_h": 23, "night_end_h": 7},
                    "sun": {"2026-10-01": 5.0}}
        json.dumps(brain.page(dict(request, lang=lang, read_at=request["now"], deye=deye, inverter=inverter,
                                   grid_day={"day": "2026-10-01", "marks": {"7": 1.0}})))
        state = {"on": True, "since": request["now"] - 7200_000}
        for on in (False, True):
            observed = {"on": on, "at": request["now"], "battery": "80%"}
            reply = brain.power(dict(request, lang=lang, read_at=request["now"], state=state, observed=observed))
            json.dumps(reply)
            # Worded from the home service's pushed baseline, as at the start of the reserve's turn.
            pushed = {key: value for key, value in request.items() if key not in ("answer", "fact", "names")}
            json.dumps(brain.power(dict(pushed, lang=lang, baseline=first["baseline"], read_at=request["now"],
                                        state=state, observed=observed)))


warm_up()


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response("not found", status=404)

    async def process(self, request):
        return json.dumps(brain.process(json.loads(request)), ensure_ascii=False)

    async def power(self, request):
        return json.dumps(brain.power(json.loads(request)), ensure_ascii=False)

    async def page(self, request):
        return json.dumps(brain.page(json.loads(request)), ensure_ascii=False)
