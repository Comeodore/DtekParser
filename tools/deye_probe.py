"""Polls Deye Cloud and reports how often station and inverter data actually change.

On the server, with the DEYE_* settings in .env (see .env.example):

    cd /opt/dtek-service
    docker run --rm --env-file .env -v "$PWD/tools:/tools:ro" dtek-service:latest \
        python /tools/deye_probe.py [minutes]

Without DEYE_STATION_ID it lists the account's stations with their inverters and exits.
Deye says station summaries refresh at most every 5 min, the logger every 1 or 5 min;
this compares station/latest with device/latest for the station's inverter.
"""
import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

POLL_S = 30
STATION_FIELDS = ("batterySOC", "batteryPower", "chargePower", "dischargePower", "consumptionPower",
                  "generationPower", "gridPower", "purchasePower", "wirePower")


def post(url: str, body: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    if not data.get("success"):
        raise SystemExit(f"{url.split('/v1.0/')[-1]} failed: {data.get('code')} {data.get('msg')}")
    return data


def password_hash() -> str:
    if os.environ.get("DEYE_PASSWORD_SHA256"):
        return os.environ["DEYE_PASSWORD_SHA256"].strip().lower()
    return hashlib.sha256(os.environ["DEYE_PASSWORD"].encode()).hexdigest()


def gaps(times: list[float]) -> list[int]:
    return [round(b - a) for a, b in zip(times, times[1:])]


def main(minutes: float) -> None:
    base = os.environ.get("DEYE_BASE_URL", "https://eu1-developer.deyecloud.com").rstrip("/") + "/v1.0"
    auth = post(f"{base}/account/token?appId={os.environ['DEYE_APP_ID']}", {
        "appSecret": os.environ["DEYE_APP_SECRET"],
        "email": os.environ["DEYE_EMAIL"],
        "password": password_hash(),
    })
    token = auth["accessToken"]
    print(f"token ok, expires in {auth.get('expiresIn')} s")

    stations = post(f"{base}/station/listWithDevice", {"page": 1, "size": 50, "deviceType": "INVERTER"},
                    token).get("stationList") or []
    for s in stations:
        sns = [d.get("deviceSn") for d in s.get("deviceListItems") or []]
        print(f"station {s.get('id')}: {s.get('name')} | {s.get('locationAddress', '')} | inverters {sns}")
    station = os.environ.get("DEYE_STATION_ID")
    if not station:
        print("set DEYE_STATION_ID to one of the ids above and run again")
        return
    match = next((s for s in stations if str(s.get("id")) == station), None)
    devices = (match or {}).get("deviceListItems") or []
    sn = os.environ.get("DEYE_DEVICE_SN") or (devices[0].get("deviceSn") if devices else None)
    print(f"watching station {station}, inverter {sn}, every {POLL_S} s for {minutes:g} min")

    first = True
    station_seen = device_seen = None
    station_changes, device_changes = [], []
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        now = datetime.now().strftime("%H:%M:%S")
        data = post(f"{base}/station/latest", {"stationId": int(station)}, token)
        if data.get("lastUpdateTime") != station_seen:
            values = " ".join(f"{k}={data.get(k)}" for k in STATION_FIELDS)
            print(f"{now} station lastUpdateTime={data.get('lastUpdateTime')} {values}", flush=True)
            station_seen = data.get("lastUpdateTime")
            station_changes.append(time.monotonic())
        if sn:
            item = (post(f"{base}/device/latest", {"deviceList": [sn]}, token).get("deviceDataList") or [{}])[0]
            if first:
                print("inverter data points:")
                for point in item.get("dataList") or []:
                    print(f"    {point.get('key')} = {point.get('value')} {point.get('unit') or ''}")
                first = False
            if item.get("collectionTime") != device_seen:
                at = datetime.fromtimestamp(item["collectionTime"]).strftime("%H:%M:%S") if item.get("collectionTime") else None
                print(f"{now} inverter collectionTime={at} state={item.get('deviceState')}", flush=True)
                device_seen = item.get("collectionTime")
                device_changes.append(time.monotonic())
        time.sleep(POLL_S)

    # The first entry is the value already there at start, so full intervals begin at the second.
    print(f"station: {len(station_changes) - 1} changes, s between them: {gaps(station_changes[1:])}")
    print(f"inverter: {len(device_changes) - 1} changes, s between them: {gaps(device_changes[1:])}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 15)
