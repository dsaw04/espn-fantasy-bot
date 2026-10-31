"""What the bot has said before, kept in a folder between runs.

The GitHub workflows keep this folder on its own `bot-data` branch, so every
run sees what earlier runs wrote. Local runs only use it when given --data-dir.

alerts.json  lineup alerts already sent today, so the hourly check stays quiet
             until something changes
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import List, Optional

from .lineup import TodayPlan


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def alert_key(plan: TodayPlan) -> str:
    """Identifies the advice, not the wording, so a repeat is recognised."""
    moves = sorted(f"{c.player.id}:{c.new}" for c in plan.changes)
    return "|".join(moves + sorted(plan.warnings))


class Alerts:
    def __init__(self, data_dir: Optional[str], today: date):
        self.path = Path(data_dir) / "alerts.json" if data_dir else None
        self.today = today.isoformat()
        state = _read_json(self.path, {}) if self.path else {}
        self.sent: List[str] = state.get("sent", []) if state.get("date") == self.today else []

    def already_sent(self, key: str) -> bool:
        return key in self.sent

    def mark_sent(self, key: str) -> None:
        if key not in self.sent:
            self.sent.append(key)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"date": self.today, "sent": self.sent}, indent=1), encoding="utf-8")
