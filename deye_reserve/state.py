"""What deye-reserve keeps across restarts, as small JSON files in its data directory."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from deye_reserve.planner import Slot, Table

logger = logging.getLogger(__name__)


def table_to_list(table: Optional[Table]) -> Optional[list]:
    return [[s.start, s.soc, s.grid_charge] for s in table] if table is not None else None


def table_from_list(data: Optional[list]) -> Optional[Table]:
    return tuple(Slot(int(a), int(b), bool(c)) for a, b, c in data) if data else None


@dataclass
class State:
    previous_table: Optional[list] = None       # the last recommended table (it stays unless clearly beaten)
    sent_table: Optional[list] = None           # what the last message showed
    sent_status: str = ""
    sent_schedule: dict = field(default_factory=dict)   # our group's slots by ISO date, as last sent
    seen_stamp: Optional[str] = None            # DTEK's schedule "last updated" seen by the fallback poll
    last_full: Optional[str] = None             # ISO date the battery was last seen at 99%+
    device_sn: Optional[str] = None
    written_table: Optional[list] = None        # taken by the inverter (order status 666)
    writes: dict = field(default_factory=dict)  # ISO date -> write attempts
    last_write: Optional[dict] = None
    verify: Optional[dict] = None               # a written table the cloud has yet to show: table, order, sent

    def writes_on(self, day: date) -> int:
        return int(self.writes.get(day.isoformat(), 0))

    def count_write(self, day: date) -> None:
        self.writes = {d: n for d, n in self.writes.items() if d >= day.isoformat()}
        self.writes[day.isoformat()] = self.writes_on(day) + 1


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning(f"{path.name} unreadable, ignoring: {e}")
        return None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_state(path: Path) -> State:
    data = read_json(path) or {}
    known = {k: v for k, v in data.items() if k in State.__dataclass_fields__}
    return State(**known)


def save_state(path: Path, state: State) -> None:
    write_json(path, asdict(state))
