"""Configuration, read once from the environment (see .env.example).

Each address is a "source" named in SOURCES; its settings use the upper-cased
name as prefix, e.g. KEM_STREET. Telegram bots are declared as
BOT_<NAME>_TOKEN and referenced by name from sources.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Mapping, Optional

DEFAULT_URLS = {
    "kem": "https://www.dtek-kem.com.ua/ua/shutdowns",
    "krem": "https://www.dtek-krem.com.ua/ua/shutdowns",
}
LANGUAGES = ("en", "uk")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SourceConfig:
    name: str
    url: str
    street: str
    building: str
    settlement: Optional[str]
    port: int               # the mini app / API for this address listens here
    schedule_id: int        # row in the legacy dtek_schedule table
    lang: str
    bot: str
    chat_ids: tuple[str, ...]
    notify_outages: bool    # send DTEK's "no power at your address" notices

    @property
    def fingerprint(self) -> str:
        """Identity of the watched address; a change resets the detector baseline."""
        raw = "|".join((self.url, self.settlement or "", self.street, self.building))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class PowerConfig:
    ws_url: str
    token: str
    voltage_entity: str
    battery_entity: str
    source: str             # whose schedule and chats the power messages use
    min_voltage: float


@dataclass(frozen=True)
class Settings:
    database_url: str
    db_schema: str
    sources: tuple[SourceConfig, ...]
    bots: Mapping[str, str]
    power: Optional[PowerConfig]
    wol_mac: Optional[str]
    wol_broadcast: str
    fetch_interval: float
    dry_run: bool

    def source(self, name: str) -> SourceConfig:
        return next(s for s in self.sources if s.name == name)


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


def _int(env: Mapping[str, str], key: str, default: Optional[int] = None) -> int:
    value = _get(env, key)
    if value is None:
        if default is None:
            raise ConfigError(f"{key} is required")
        return default
    try:
        return int(value)
    except ValueError as e:
        raise ConfigError(f"{key}={value!r} is not an integer") from e


def _chat_ids(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    return tuple(c.strip() for c in (_get(env, key) or "").split(",") if c.strip())


def _source(env: Mapping[str, str], name: str, index: int) -> SourceConfig:
    p = name.upper() + "_"
    lang = _get(env, p + "LANG", "uk")
    if lang not in LANGUAGES:
        raise ConfigError(f"{p}LANG must be one of {LANGUAGES}")
    url = _get(env, p + "URL", DEFAULT_URLS.get(name))
    if not url:
        raise ConfigError(f"{p}URL is required for source {name!r}")
    return SourceConfig(
        name=name,
        url=url,
        street=_require(env, p + "STREET"),
        building=_require(env, p + "BUILDING"),
        settlement=_get(env, p + "SETTLEMENT"),
        port=_int(env, p + "PORT"),
        schedule_id=_int(env, p + "SCHEDULE_ID", index + 1),
        lang=lang,
        bot=_require(env, p + "BOT"),
        chat_ids=_chat_ids(env, p + "CHAT_IDS"),
        notify_outages=_bool(env, p + "NOTIFY_OUTAGES", True),
    )


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    env = os.environ if env is None else env
    names = [n.strip().lower() for n in (_get(env, "SOURCES") or "").split(",") if n.strip()]
    if not names:
        raise ConfigError("SOURCES is required, e.g. SOURCES=kem,krem")
    if len(set(names)) != len(names):
        raise ConfigError("SOURCES has duplicates")
    sources = tuple(_source(env, name, i) for i, name in enumerate(names))

    bots = {
        key[len("BOT_"):-len("_TOKEN")].lower(): value.strip()
        for key, value in env.items()
        if key.startswith("BOT_") and key.endswith("_TOKEN") and value.strip()
    }
    for s in sources:
        if s.bot not in bots:
            raise ConfigError(f"{s.name.upper()}_BOT={s.bot!r} but BOT_{s.bot.upper()}_TOKEN is not set")
    if len({s.port for s in sources}) != len(sources):
        raise ConfigError("every source needs its own *_PORT")

    power = None
    if _get(env, "HA_WS_URL"):
        power_source = _get(env, "POWER_SOURCE", sources[0].name)
        if power_source not in names:
            raise ConfigError(f"POWER_SOURCE={power_source!r} is not in SOURCES")
        power = PowerConfig(
            ws_url=_require(env, "HA_WS_URL"),
            token=_require(env, "HA_TOKEN"),
            voltage_entity=_get(env, "HA_VOLTAGE_ENTITY", "sensor.victron_vebus_activein_l1_voltage_228"),
            battery_entity=_get(env, "HA_BATTERY_ENTITY", "sensor.victron_battery_soc"),
            source=power_source,
            min_voltage=float(_get(env, "POWER_MIN_VOLTAGE", "50")),
        )

    return Settings(
        database_url=_require(env, "DATABASE_URL"),
        db_schema=_get(env, "DB_SCHEMA", "public"),
        sources=sources,
        bots=bots,
        power=power,
        wol_mac=_get(env, "WOL_MAC"),
        wol_broadcast=_get(env, "WOL_BROADCAST", "255.255.255.255"),
        fetch_interval=float(_get(env, "FETCH_INTERVAL", "60")),
        dry_run=_bool(env, "NOTIFY_DRY_RUN", False),
    )
