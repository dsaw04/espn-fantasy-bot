"""What the bot has said before, kept in a folder between runs.

The GitHub workflows keep this folder on its own `bot-data` branch, so every
run sees what earlier runs wrote. Local runs only use it when given --data-dir.

alerts.json   lineup alerts already sent today, so the hourly check stays quiet
              until something changes
runs.jsonl    one line per daily report: the matchup, the win chances, every
              suggested move and your roster's per game estimates
reviews.jsonl one line per finished matchup, checking those predictions against
              what happened
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Callable, Dict, List, Optional

from .lineup import TodayPlan
from .model import GameLog, Snapshot

MOVE_WINDOW = 7  # days after a suggestion over which the add and drop are compared


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


# ---- run history ---------------------------------------------------------------
def _read_lines(path: Path) -> List[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a half written line from an interrupted run
    return out


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def log_run(data_dir: str, snap: Snapshot, report) -> None:
    """Remember what this report predicted and suggested."""
    m, o = snap.matchup, report.outlook
    row = {
        "at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "sp": snap.today,
        "matchup": m.period if m else None,
        "days": m.days if m else [],
        "opp": m.opp.name if m else None,
        "score": [m.me.score, m.opp.score] if m else None,
        "projected": [round(o.now.mine, 1), round(o.now.theirs, 1)] if o else None,
        "win_now": round(o.now.win, 4) if o else None,
        "win_next": round(o.next.win, 4) if o and o.next else None,
        "moves": [
            {
                "add": mv.add.player.id, "add_name": mv.add.player.name,
                "drop": mv.drop.player.id, "drop_name": mv.drop.player.name,
                "kind": mv.kind, "per_game": round(mv.per_game, 2), "drop_per_game": round(mv.drop_per_game, 2),
                "gain": round(mv.gain, 1), "win_gain": round(mv.win_gain, 4) if mv.win_gain is not None else None,
            }
            for mv in report.moves
        ],
        "roster": {str(p.id): round(report.per_game.get(p.id, 0.0), 2) for p in snap.roster_for_moves()},
        "names": {str(p.id): p.name for p in snap.roster_for_moves()},
    }
    _append(Path(data_dir) / "runs.jsonl", row)


@dataclass
class Review:
    period: int
    opp: str
    me: float
    them: float
    predicted: Optional[float]  # your win chance at the first report of that matchup
    suggested: int  # distinct add/drop suggestions during it
    made: int  # of those, the ones you made
    made_gain: Optional[float]  # fpts the adds you made scored over their drops, in the week after each
    error: Optional[float]  # average miss of your players' per game estimates, in fpts/g
    bias: Optional[float]  # actual minus estimate, on average
    rel_error: Optional[float]  # average miss as a share of the estimate, to compare with projection_error
    players: int = 0

    @property
    def won(self) -> bool:
        return self.me > self.them


def reviews(data_dir: str) -> List[Review]:
    return [Review(**r) for r in _read_lines(Path(data_dir) / "reviews.jsonl")]


def review_last_matchup(
    data_dir: Optional[str], snap: Snapshot, game_logs: Callable[[List[int]], Dict[int, Dict[int, GameLog]]]
) -> Optional[Review]:
    """The first report of a matchup checks the one before it, once: how the forecast,
    the suggestions you took and the per game estimates held up."""
    m = snap.matchup
    if not data_dir or m is None or m.last is None:
        return None
    period = m.period - 1
    if any(r.period == period for r in reviews(data_dir)):
        return None
    runs = _read_lines(Path(data_dir) / "runs.jsonl")
    during = [r for r in runs if r.get("matchup") == period]
    if not during:
        return None
    first = during[0]
    days = set(during[-1].get("days") or [])

    # Each distinct suggestion, from the day it was first made. It counts as made if the
    # add was on your roster in any later report, or is now.
    suggested: Dict[tuple, int] = {}
    for r in during:
        for mv in r.get("moves", []):
            suggested.setdefault((mv["add"], mv["drop"]), r["sp"])
    later_rosters = [set(r.get("roster", {})) for r in runs if r["sp"] > first["sp"]]
    later_rosters.append({str(p.id) for p in snap.roster_for_moves()})
    made = [(a, d, sp) for (a, d), sp in suggested.items() if any(str(a) in ids for ids in later_rosters)]

    estimates = {int(k): v for k, v in first.get("roster", {}).items() if v > 0}
    logs = game_logs(sorted({x for a, d, _ in made for x in (a, d)} | set(estimates)))

    def fpts(pid: int, lo: int, hi: int) -> float:
        return sum(g.fpts for sp, g in (logs.get(pid) or {}).items() if lo < sp <= hi and sp < snap.today)

    made_gain = sum(fpts(a, sp, sp + MOVE_WINDOW) - fpts(d, sp, sp + MOVE_WINDOW) for a, d, sp in made) if made else None
    errors, rel = [], []
    for pid, est in estimates.items():
        played = [g.fpts for sp, g in (logs.get(pid) or {}).items() if sp in days]
        if played:
            errors.append(mean(played) - est)
            rel.append(abs(mean(played) - est) / est)
    opp, me, them = m.last
    review = Review(
        period=period, opp=opp, me=me, them=them, predicted=first.get("win_now"),
        suggested=len(suggested), made=len(made), made_gain=made_gain,
        error=mean(abs(e) for e in errors) if errors else None,
        bias=mean(errors) if errors else None,
        rel_error=mean(rel) if rel else None,
        players=len(errors),
    )
    _append(Path(data_dir) / "reviews.jsonl", asdict(review))
    return review


def brier(rs: List[Review]) -> Optional[float]:
    """Mean squared miss of the win forecasts: 0 is perfect, 0.25 is a coin flip."""
    scored = [(r.predicted - (1.0 if r.won else 0.0)) ** 2 for r in rs if r.predicted is not None]
    return mean(scored) if scored else None
