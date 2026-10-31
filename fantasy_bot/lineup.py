"""Works out who would actually start each day.

A pickup only helps on days he'd start over the players you already have, and
a player you drop only costs you the games he'd have started. So moves are
valued by how much they raise the total of your best lineup over the coming
days, not by counting every game a player has.

Picking the starters is a matching problem: each player can fill only some
slots. Choosing the highest scoring group that fits is a transversal matroid,
so taking players best first, and keeping each one only if everyone kept so
far can still be matched to slots, gives the optimal lineup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .config import Config
from .model import PlayerView, Snapshot
from .scoring import availability_on, value_on

NOT_STARTING = {"BE", "IR"}


def starting_slots(lineup_slots: Dict[str, int]) -> List[str]:
    """{"PG": 1, "UT": 3, "BE": 3} -> ["PG", "UT", "UT", "UT"]"""
    return [s for s, n in lineup_slots.items() if s not in NOT_STARTING for _ in range(n)]


def _place(i: int, fits: List[List[int]], owner: List[Optional[int]], seen: Set[int]) -> bool:
    """Find player i a slot, moving already placed players along if needed."""
    for s in fits[i]:
        if s in seen:
            continue
        seen.add(s)
        if owner[s] is None or _place(owner[s], fits, owner, seen):
            owner[s] = i
            return True
    return False


def assign(
    players: Sequence[Tuple[PlayerView, float]], slots: List[str], prefer: Optional[Dict[int, str]] = None
) -> Dict[int, str]:
    """The highest scoring group of these (player, value) pairs that fits the slots, as
    player id -> slot. Each player tries his `prefer` slot first, so a lineup that's
    already right stays put."""
    prefer = prefer or {}
    ranked = sorted((pv for pv in players if pv[1] > 0), key=lambda pv: pv[1], reverse=True)
    fits = [
        sorted((s for s, name in enumerate(slots) if name in p.eligible_slots), key=lambda s: slots[s] != prefer.get(p.id))
        for p, _ in ranked
    ]
    owner: List[Optional[int]] = [None] * len(slots)
    for i in range(len(ranked)):
        _place(i, fits, owner, set())
    return {ranked[i][0].id: slots[s] for s, i in enumerate(owner) if i is not None}


def best_lineup(players: Sequence[Tuple[PlayerView, float]], slots: List[str]) -> List[PlayerView]:
    """The highest scoring group of these (player, value) pairs that fits the slots."""
    chosen = assign(players, slots)
    return [p for p, _ in players if p.id in chosen]


@dataclass
class Week:
    total: float = 0.0  # fpts from the best lineup each day
    starts: Dict[int, int] = field(default_factory=dict)  # player id -> games he'd start
    games: Dict[int, int] = field(default_factory=dict)  # player id -> games he's available for
    points: Dict[int, float] = field(default_factory=dict)  # player id -> fpts from his starts
    sq: float = 0.0  # sum of each start's value squared, for the game to game spread
    open: Dict[int, int] = field(default_factory=dict)  # scoring period -> starting slots left empty

    def __add__(self, other: "Week") -> "Week":
        def merge(a, b):
            return {k: a.get(k, 0) + b.get(k, 0) for k in a.keys() | b.keys()}
        return Week(self.total + other.total, merge(self.starts, other.starts), merge(self.games, other.games),
                    merge(self.points, other.points), self.sq + other.sq, {**self.open, **other.open})


def project(
    roster: Sequence[Tuple[PlayerView, float]],
    days: List[int],
    snap: Snapshot,
    cfg: Config,
    now: Optional[datetime] = None,
) -> Week:
    """Best lineup totals over these days for a roster of (player, per game value).
    Without known slots, everyone who plays counts, as if the bench were unlimited.
    Games that started before `now` are left out: they're already in the live score."""
    slots = starting_slots(snap.lineup_slots)
    week = Week()
    for sp in days:
        day = snap.period_dates.get(sp)
        playing = []
        for p, per_game in roster:
            if sp not in p.games or (now is not None and p.games[sp] <= now):
                continue
            value = availability_on(p, day, cfg) * value_on(per_game, day)
            if value > 0:
                playing.append((p, value))
                week.games[p.id] = week.games.get(p.id, 0) + 1
        starters = best_lineup(playing, slots) if slots else [p for p, _ in playing]
        value_of = {p.id: v for p, v in playing}
        for p in starters:
            v = value_of[p.id]
            week.total += v
            week.sq += v * v
            week.starts[p.id] = week.starts.get(p.id, 0) + 1
            week.points[p.id] = week.points.get(p.id, 0.0) + v
        if slots:
            week.open[sp] = len(slots) - len(starters)
    return week


@dataclass
class LineupChange:
    player: PlayerView
    old: str  # slot he's in now
    new: str  # slot to move him to, "BE" for the bench
    reason: str = ""  # why a starter comes out


@dataclass
class TodayPlan:
    changes: List[LineupChange] = field(default_factory=list)
    gain: float = 0.0  # fpts the changes add today
    warnings: List[str] = field(default_factory=list)
    # Starting spots nobody can fill today, and the starters left in them who won't score
    empty: int = 0
    idle: List[Tuple[PlayerView, str]] = field(default_factory=list)


def todays_lineup(snap: Snapshot, cfg: Config, value_of: Callable[[PlayerView], float]) -> TodayPlan:
    """The best lineup for today's games that haven't started, as changes from the one
    set in ESPN. Players whose game has started are locked, and so is their slot."""
    plan = TodayPlan()
    today, now = snap.today, snap.now
    day = snap.period_dates.get(today)
    for p in snap.my_roster:
        if p.lineup_slot == "IR" and not p.is_out:
            plan.warnings.append(
                f"{p.name} is {p.status_label()} but still on IR. ESPN blocks adds and drops until he's moved off it."
            )
    slots = starting_slots(snap.lineup_slots)
    if not slots or today not in snap.period_dates:
        return plan

    def locked(p: PlayerView) -> bool:
        return now is not None and today in p.games and p.games[today] <= now

    def worth(p: PlayerView) -> float:
        if today not in p.games:
            return 0.0
        return availability_on(p, day, cfg) * value_of(p)

    free = list(slots)
    movable = []
    for p in snap.my_roster:
        if p.lineup_slot == "IR":
            continue
        if locked(p):
            if p.lineup_slot in free:
                free.remove(p.lineup_slot)
        else:
            movable.append(p)
    current = {p.id: p.lineup_slot for p in movable}
    best = assign([(p, worth(p)) for p in movable], free, prefer=current)
    plan.gain = sum(worth(p) for p in movable if p.id in best) - sum(
        worth(p) for p in movable if p.lineup_slot in free and p.lineup_slot not in NOT_STARTING
    )
    for p in movable:
        new = best.get(p.id, "BE")
        if new == p.lineup_slot:
            continue
        reason = ""
        if new == "BE":
            reason = "no game today" if today not in p.games else p.status_label() if worth(p) == 0 else "others score more"
        plan.changes.append(LineupChange(p, p.lineup_slot or "BE", new, reason))
    plan.changes.sort(key=lambda c: c.new != "BE")  # benchings first, so the slots are free
    plan.empty = len(free) - len(best)
    if plan.empty:
        plan.idle = [
            (p, "no game today" if today not in p.games else p.status_label())
            for p in movable
            if p.id not in best and p.lineup_slot not in NOT_STARTING and p.lineup_slot
        ]
    return plan
