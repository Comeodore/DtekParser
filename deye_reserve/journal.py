"""Every replan, write and message, one JSON line each, for looking back at what was decided and why."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Journal:
    KEEP = timedelta(days=30)

    def __init__(self, path: Path):
        self.path = path

    def add(self, kind: str, at: datetime, **fields) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": at.isoformat(), "kind": kind, **fields}, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning(f"journal not written: {e}")

    def read(self, since: Optional[datetime] = None, until: Optional[datetime] = None) -> list[dict]:
        rows = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return rows
        for line in lines:
            try:
                row = json.loads(line)
                at = datetime.fromisoformat(row["at"])
            except (ValueError, KeyError):
                continue
            if (since is None or at >= since) and (until is None or at < until):
                rows.append(row)
        return rows

    def prune(self, now: datetime) -> None:
        """Drops what is older than a month."""
        keep = self.read(since=now - self.KEEP)
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
            tmp.replace(self.path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"journal not pruned: {e}")
