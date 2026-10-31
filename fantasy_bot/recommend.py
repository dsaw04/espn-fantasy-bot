"""Turns valuations into concrete suggestions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from .config import Config
from .model import PlayerView, Snapshot, n_games
from .scoring import Scorer, Valuation, availability_on, minutes_trend


@dataclass
class LineupSwap:
    slot: str
    bench: PlayerView  # the starter coming out
    start: Optional[PlayerView]  # the bench player going in, if any
    reason: str


@dataclass
class Move:
    add: Valuation
    drop: Valuation
    gain: float
    reasons: List[str] = field(default_factory=list)


@dataclass
class Report:
    team_name: str
    today: int
    horizon_days: int
    injured_on_roster: List[PlayerView]
    swaps: List[LineupSwap]
    moves: List[Move]
    chopping_block: List[Valuation]
    watchlist: List[Valuation]


def _norm(name: str) -> str:
    return " ".join(name.lower().replace(".", "").split())


def chopping_block(snap: Snapshot, cfg: Config) -> List[PlayerView]:
    named = {_norm(n) for n in cfg.chopping_block}
    safe = {_norm(n) for n in cfg.untouchable}
    out = []
    for p in snap.my_roster:
        key = _norm(p.name)
        if key in safe:
            continue
        if key in named or (cfg.auto_chop_pickups and p.acquisition_type == "ADD"):
            out.append(p)
    return out


def lineup_swaps(snap: Snapshot, cfg: Config, scorer: Scorer) -> List[LineupSwap]:
    """Starters who won't score today, and the best bench player to slide in."""
    today = snap.today
    day = snap.period_dates.get(today)
    starters = [p for p in snap.my_roster if p.is_starting_slot]
    bench = [p for p in snap.my_roster if p.lineup_slot == "BE"]
    used: Set[int] = set()
    swaps: List[LineupSwap] = []

    def bench_options(slot: str):
        opts = [
            b
            for b in bench
            if b.id not in used
            and today in b.games
            and availability_on(b, day, cfg) > 0
            and slot in b.eligible_slots
        ]
        return sorted(opts, key=lambda b: scorer.base(b) * availability_on(b, day, cfg), reverse=True)

    for s in starters:
        if today not in s.games:
            reason = "no game today"
        elif availability_on(s, day, cfg) == 0:
            reason = s.status_label()
        else:
            continue
        opts = bench_options(s.lineup_slot)
        pick = opts[0] if opts else None
        if pick:
            used.add(pick.id)
        swaps.append(LineupSwap(s.lineup_slot, s, pick, reason))
    return swaps


def _add_reasons(v: Valuation, horizon: int) -> List[str]:
    p = v.player
    r = [f"{n_games(v.games)} in the next {horizon} days, ~{v.base:.1f} fpts/g"]
    for o in sorted(v.openings, key=lambda o: o.boost, reverse=True):
        if o.boost < 0.5:
            continue
        r.append(
            f"{o.injured.name} ({'/'.join(sorted(o.injured.positions))}) is "
            f"{o.injured.status_label()}, leaving ~{o.value:.0f} fpts/g behind. "
            f"Projected bump ~+{o.boost:.1f}/g"
        )
    trend = minutes_trend(p)
    if trend >= 3:
        r.append(f"minutes up {p.mpg.get('season', 0):.0f} to {p.mpg.get('last_7', 0):.0f} over the last week")
    if p.is_risky:
        r.append(f"note: listed {p.status_label()}")
    return r


def _drop_reason(v: Valuation, horizon: int) -> str:
    p = v.player
    bits = [f"{n_games(v.games)} in {horizon} days, ~{v.base:.1f} fpts/g"]
    if p.is_out or p.is_risky:
        bits.append(p.status_label())
    return ", ".join(bits)


def waiver_moves(snap: Snapshot, cfg: Config, scorer: Scorer):
    droppable = [scorer.value(p) for p in chopping_block(snap, cfg)]
    droppable.sort(key=lambda v: v.expected)

    pool = [p for p in snap.free_agents if not p.is_out and p.games]
    adds = [scorer.value(p) for p in pool]
    adds.sort(key=lambda v: v.expected, reverse=True)

    moves: List[Move] = []
    remaining_drops = list(droppable)
    taken: Set[int] = set()
    bet_on: Set[int] = set()  # injured players a recommended add is already leaning on
    for add in adds:
        if len(moves) >= cfg.max_moves or not remaining_drops:
            break
        if add.player.id in taken:
            continue
        leans_on = {o.injured.id for o in add.openings if o.boost >= 0.25 * max(add.base, 1)}
        if leans_on & bet_on:
            continue  # don't stack two pickups on the same injury
        bet_on |= leans_on
        drop = remaining_drops[0]
        gain = add.expected - drop.expected
        if gain < cfg.min_gain:
            break
        moves.append(
            Move(
                add=add,
                drop=drop,
                gain=gain,
                reasons=_add_reasons(add, len(snap.horizon))
                + [f"drop {drop.player.name}: {_drop_reason(drop, len(snap.horizon))}"],
            )
        )
        taken.add(add.player.id)
        remaining_drops.pop(0)

    # Depth plays worth watching even if they don't beat anyone on the block yet.
    watch = [
        v
        for v in adds
        if v.player.id not in taken and any(o.boost >= 1.0 for o in v.openings)
    ][:5]
    return moves, droppable, watch


def build_report(snap: Snapshot, cfg: Config) -> Report:
    scorer = Scorer(snap, cfg)
    moves, block, watch = waiver_moves(snap, cfg, scorer)
    return Report(
        team_name=snap.my_team_name,
        today=snap.today,
        horizon_days=len(snap.horizon),
        injured_on_roster=[p for p in snap.my_roster if p.is_out or p.is_risky],
        swaps=lineup_swaps(snap, cfg, scorer),
        moves=moves,
        chopping_block=block,
        watchlist=watch,
    )
