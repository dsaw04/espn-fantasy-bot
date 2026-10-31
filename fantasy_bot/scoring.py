"""How the bot values a player over the next few days.

expected value = sum over upcoming games of
    availability on that day x (per game value + depth boost on that day)

per game value  blends recent form, season average and ESPN's projection.
depth boost     is the extra production a player should get while an injured
                teammate who shares his position is sitting out.
availability    is 0 while a player is out, a discount when day-to-day, else 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from .config import Config
from .model import PlayerView, Snapshot


# ESPN's projection counts as much as this many real games. Its weight fades as a
# player's games pile up, so a preseason guess can't hold down a breakout for long.
PROJECTION_GAMES = 5


def per_game_value(p: PlayerView, cfg: Config) -> float:
    w = cfg.weights
    played = len(p.game_log) if p.game_log is not None else p.games_played
    parts = [
        (p.fpts.get("last_7"), w.last_7),
        (p.fpts.get("last_15"), w.last_15),
        (p.fpts.get("season"), w.season),
        (p.fpts.get("projected"), w.projected * PROJECTION_GAMES / (PROJECTION_GAMES + played)),
    ]
    used = [(v, wt) for v, wt in parts if v is not None and wt > 0]
    if not used:
        return 0.0
    total_w = sum(wt for _, wt in used)
    return sum(v * wt for v, wt in used) / total_w


def minutes(p: PlayerView) -> float:
    for key in ("last_7", "last_15", "season", "projected"):
        if p.mpg.get(key):
            return p.mpg[key]
    return 0.0


def is_out_on(p: PlayerView, day: Optional[date]) -> bool:
    if not p.is_out:
        return False
    if p.expected_return is None or day is None:
        return True
    return day < p.expected_return


def availability_on(p: PlayerView, day: Optional[date], cfg: Config) -> float:
    if is_out_on(p, day):
        return 0.0
    if p.available_from is not None and day is not None and day < p.available_from:
        return 0.0  # still on waivers
    if p.is_risky:
        return cfg.dtd_availability
    return 1.0


@dataclass(frozen=True)
class Role:
    """A per game value that only holds while an injured teammate sits, like a backup's
    bigger role. Once the teammate is due back, he's worth his usual value again."""

    value: float  # while the teammate is out
    after: float  # once he's back
    injured: PlayerView

    def on(self, day: Optional[date]) -> float:
        return self.value if is_out_on(self.injured, day) else self.after


def value_on(per_game, day: Optional[date]) -> float:
    """A plain per game number, or a Role's value on that day."""
    return per_game.on(day) if isinstance(per_game, Role) else per_game


@dataclass
class DepthOpening:
    """An injured player whose minutes are up for grabs."""

    injured: PlayerView
    value: float
    overlap: float  # share of the injured player's positions the candidate covers
    competitors: int  # healthy teammates who can also cover those positions
    boost: float  # per game boost to the candidate


@dataclass
class Valuation:
    player: PlayerView
    base: float
    expected: float
    games: int
    plays_today: bool
    openings: List[DepthOpening] = field(default_factory=list)


class Scorer:
    def __init__(self, snap: Snapshot, cfg: Config):
        self.snap = snap
        self.cfg = cfg
        self.by_team: Dict[int, List[PlayerView]] = {}
        for p in snap.all_players():
            self.by_team.setdefault(p.pro_team_id, []).append(p)
        self._base: Dict[int, float] = {}

    def base(self, p: PlayerView) -> float:
        if p.id not in self._base:
            self._base[p.id] = per_game_value(p, self.cfg)
        return self._base[p.id]

    # Share of vacated work by depth order among non-starters: the next man up
    # takes the most, the third stringer a little. Starters can only stretch a bit.
    DEPTH_WEIGHTS = (1.0, 0.45, 0.2, 0.1)
    STARTER_MINUTES = 30.0
    STARTER_WEIGHT = 0.15

    def _depth_share(self, p: PlayerView, competitors: List[PlayerView]) -> float:
        starters = [t for t in competitors if minutes(t) >= self.STARTER_MINUTES]
        reserves = sorted(
            (t for t in competitors if minutes(t) < self.STARTER_MINUTES),
            key=minutes,
            reverse=True,
        )
        weights = {t.id: self.STARTER_WEIGHT for t in starters}
        for rank, t in enumerate(reserves):
            weights[t.id] = self.DEPTH_WEIGHTS[min(rank, len(self.DEPTH_WEIGHTS) - 1)]
        total = sum(weights.values())
        return weights.get(p.id, 0.0) / total if total else 0.0

    def openings_for(self, p: PlayerView) -> List[DepthOpening]:
        """Scout the depth chart: which injured teammates leave work at this player's spots."""
        if p.is_out or not p.positions:
            return []
        out: List[DepthOpening] = []
        team = self.by_team.get(p.pro_team_id, [])
        for inj in team:
            if inj.id == p.id or not inj.is_out or not inj.positions:
                continue
            if minutes(inj) < self.cfg.min_minutes_for_depth:
                continue
            if "season" not in inj.mpg:
                continue  # out all season, so the candidate's stats already reflect the bigger role
            shared = p.positions & inj.positions
            if not shared:
                continue
            overlap = len(shared) / len(inj.positions)
            competitors = [
                t
                for t in team
                if not t.is_out and t.positions & inj.positions
            ]
            share = self._depth_share(p, competitors)
            inj_value = self.base(inj)
            boost = inj_value * self.cfg.absorb_rate * overlap * share
            out.append(DepthOpening(inj, inj_value, overlap, len(competitors), boost))
        return out

    def value(self, p: PlayerView, horizon: Optional[List[int]] = None) -> Valuation:
        base = self.base(p)
        openings = self.openings_for(p)
        expected = 0.0
        games = 0
        for sp in self.snap.horizon if horizon is None else horizon:
            if sp not in p.games:
                continue
            day = self.snap.period_dates.get(sp)
            avail = availability_on(p, day, self.cfg)
            if avail <= 0:
                continue
            games += 1
            boost = sum(o.boost for o in openings if is_out_on(o.injured, day))
            expected += avail * (base + boost)
        return Valuation(
            player=p,
            base=base,
            expected=expected,
            games=games,
            plays_today=self.snap.today in p.games,
            openings=openings,
        )
