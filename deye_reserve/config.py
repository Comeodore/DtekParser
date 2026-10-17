"""Settings of deye-reserve, read once from the environment (the same .env as dtek-service).

Deye credentials are dtek-service's DEYE_*; everything of this service is
DEYE_RESERVE_*. Writing to the inverter is off unless DEYE_RESERVE_WRITE=true.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from datetime import time
from pathlib import Path
from typing import Mapping, Optional

from deye_reserve.planner import Params


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    # Deye Cloud (shared with dtek-service)
    app_id: str
    app_secret: str
    email: str
    password_sha256: str
    station_id: int
    device_sn: Optional[str]
    base_url: str
    # Telegram: the krem bot, the owner's own chat only
    bot_token: str
    chat_id: str
    dry_run: bool
    # Where things are
    dtek_url: str               # dtek-service, krem's port
    group: str                  # DTEK queue of the house
    latitude: float
    longitude: float
    pv_per_ghi: float           # PV W per W/m2 of global horizontal irradiance
    top_models: int             # the sun's median is of this many models that fit the panels best
    rank_days: int              # ...judged over this many days
    host: str
    port: int
    data_dir: Path
    # Planning
    params: Params
    calibration_days: int
    schedule_max_age_h: float
    # Timing
    debounce_s: float           # after a webhook: DTEK publishes in batches
    max_wait_s: float           # ...but never wait longer than this after the first one
    poll_s: float               # fallback look at the schedule
    replan_s: float
    forecast_at: tuple[time, ...]
    # Writing
    write: bool
    max_writes_per_day: int


def _get(env: Mapping[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    value = env.get(key)
    if value is None or not value.strip():
        return default
    return value.strip()


def _require(env: Mapping[str, str], key: str) -> str:
    value = _get(env, key)
    if value is None:
        raise ConfigError(f"{key} is required")
    return value


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    value = _get(env, key)
    if value is None:
        return default
    if value.lower() in ("1", "true", "yes", "on"):
        return True
    if value.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key}={value!r} is not a boolean")


def _num(env: Mapping[str, str], key: str, default, kind=float):
    value = _get(env, key)
    if value is None:
        return default
    try:
        return kind(value)
    except ValueError as e:
        raise ConfigError(f"{key}={value!r} is not a number") from e


def _times(value: str) -> tuple[time, ...]:
    try:
        return tuple(sorted(time.fromisoformat(v.strip()) for v in value.split(",") if v.strip()))
    except ValueError as e:
        raise ConfigError(f"DEYE_RESERVE_FORECAST_AT={value!r} is not a list of HH:MM") from e


# Planner parameters that can be tuned without a release: DEYE_RESERVE_<NAME>.
PARAM_ENV = {
    "CAPACITY_KWH": "capacity_kwh", "EFFICIENCY": "efficiency", "CHARGE_KW": "charge_kw",
    "SHUTDOWN_SOC": "shutdown_soc", "OUTAGE_MIN_SOC": "outage_min_soc", "DAY_FLOOR": "day_floor",
    "NIGHT_MIN": "night_min", "NIGHT_MAX": "night_max", "DAY_PRICE": "day_price", "NIGHT_PRICE": "night_price",
    "NIGHT_START": "night_start_h", "NIGHT_END": "night_end_h", "SLOT_POWER": "slot_power_w",
    "SAFETY_LOAD_FACTOR": "safety_load_factor",
}


def _params(env: Mapping[str, str]) -> Params:
    types = {f.name: f.type for f in fields(Params)}
    values = {}
    for suffix, name in PARAM_ENV.items():
        kind = int if types[name] in (int, "int") else float
        value = _num(env, "DEYE_RESERVE_" + suffix, None, kind)
        if value is not None:
            values[name] = value
    params = Params(**values)
    if not params.shutdown_soc < params.outage_min_soc <= params.day_floor <= params.night_min <= params.night_max <= 100:
        raise ConfigError("need SHUTDOWN_SOC < OUTAGE_MIN_SOC <= DAY_FLOOR <= NIGHT_MIN <= NIGHT_MAX <= 100")
    return params


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    env = os.environ if env is None else env
    password = _require(env, "DEYE_PASSWORD_SHA256").lower()
    if len(password) != 64 or any(c not in "0123456789abcdef" for c in password):
        raise ConfigError("DEYE_PASSWORD_SHA256 must be the 64-character sha256 hex of the password")
    bot = _get(env, "DEYE_RESERVE_BOT", "schedulebot").upper()
    return Config(
        app_id=_require(env, "DEYE_APP_ID"),
        app_secret=_require(env, "DEYE_APP_SECRET"),
        email=_require(env, "DEYE_EMAIL"),
        password_sha256=password,
        station_id=_num(env, "DEYE_STATION_ID", None, int) if _require(env, "DEYE_STATION_ID") else 0,
        device_sn=_get(env, "DEYE_RESERVE_DEVICE_SN") or _get(env, "DEYE_DEVICE_SN"),
        base_url=_get(env, "DEYE_BASE_URL", "https://eu1-developer.deyecloud.com").rstrip("/"),
        bot_token=_require(env, f"BOT_{bot}_TOKEN"),
        chat_id=_require(env, "DEYE_RESERVE_CHAT_ID"),
        dry_run=_bool(env, "DEYE_RESERVE_DRY_RUN", False),
        dtek_url=_get(env, "DEYE_RESERVE_DTEK_URL", "http://127.0.0.1:9998").rstrip("/"),
        group=_get(env, "DEYE_RESERVE_GROUP", "GPV2.2"),
        latitude=_num(env, "DEYE_RESERVE_LATITUDE", 50.3084),
        longitude=_num(env, "DEYE_RESERVE_LONGITUDE", 30.4067),
        pv_per_ghi=_num(env, "DEYE_RESERVE_PV_PER_GHI", 5.35),
        top_models=_num(env, "DEYE_RESERVE_TOP_MODELS", 3, int),
        rank_days=_num(env, "DEYE_RESERVE_RANK_DAYS", 14, int),
        host=_get(env, "DEYE_RESERVE_HOST", "127.0.0.1"),
        port=_num(env, "DEYE_RESERVE_PORT", 9997, int),
        data_dir=Path(_get(env, "DEYE_RESERVE_DATA_DIR", "data")),
        params=_params(env),
        calibration_days=_num(env, "DEYE_RESERVE_CALIBRATION_DAYS", 7, int),
        schedule_max_age_h=_num(env, "DEYE_RESERVE_SCHEDULE_MAX_AGE_H", 3.0),
        debounce_s=_num(env, "DEYE_RESERVE_DEBOUNCE_S", 150.0),
        max_wait_s=_num(env, "DEYE_RESERVE_MAX_WAIT_S", 300.0),
        poll_s=_num(env, "DEYE_RESERVE_POLL_S", 600.0),
        replan_s=_num(env, "DEYE_RESERVE_REPLAN_S", 3600.0),
        forecast_at=_times(_get(env, "DEYE_RESERVE_FORECAST_AT", "05:30,22:00")),
        write=_bool(env, "DEYE_RESERVE_WRITE", False),
        max_writes_per_day=_num(env, "DEYE_RESERVE_MAX_WRITES", 6, int),
    )
