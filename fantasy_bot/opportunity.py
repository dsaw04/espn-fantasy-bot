"""Finds free agents whose role grew while an injured teammate sits, from game logs.

For each injured rotation player, every free agent teammate who plays one of
his positions has his games split in two: games played alongside him and games
played while he sat. The "without" average is what the pickup has actually
produced in the role he'd be stepping into, so nothing is guessed. A player
who has been out all season leaves only "without" games, which is right: the
backup's whole season already reflects the bigger role.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Dict, List, Set, Tuple

from .config import Config
from .model import GameLog, PlayerView, Snapshot
from .scoring import minutes


@dataclass
class Split:
    fpts: List[float] = field(default_factory=list)
    minutes: List[float] = field(default_factory=list)

    def add(self, g: GameLog) -> None:
        self.fpts.append(g.fpts)
        self.minutes.append(g.minutes)

    @property
    def games(self) -> int:
        return len(self.fpts)

    @property
    def avg(self) -> float:
        return sum(self.fpts) / len(self.fpts) if self.fpts else 0.0

    @property
    def mpg(self) -> float:
        return sum(self.minutes) / len(self.minutes) if self.minutes else 0.0

    @property
    def median(self) -> float:
        return median(self.fpts) if self.fpts else 0.0


@dataclass
class Opportunity:
    injured: PlayerView
    player: PlayerView  # the free agent teammate
    with_him: Split
    without_him: Split


def injured_rotation(snap: Snapshot, cfg: Config) -> List[PlayerView]:
    """Injured players who were playing real minutes, so their absence opens a role."""
    return [
        p
        for p in snap.all_players()
        if p.is_out and p.positions and minutes(p) >= cfg.min_minutes_for_depth
    ]


def pairings(snap: Snapshot, cfg: Config) -> List[Tuple[PlayerView, List[PlayerView]]]:
    """Each injured rotation player with the healthy teammates who share a position, among
    free agents and your own roster, so a pickup is valued the same way before and after
    you add him."""
    fas: Dict[int, List[PlayerView]] = {}
    seen: Set[int] = set()
    for p in snap.free_agents + snap.roster_for_moves():
        if not p.is_out and p.positions and p.id not in seen:
            seen.add(p.id)
            fas.setdefault(p.pro_team_id, []).append(p)
    out = []
    for inj in injured_rotation(snap, cfg):
        mates = [p for p in fas.get(inj.pro_team_id, []) if p.positions & inj.positions]
        if mates:
            out.append((inj, mates))
    return out


def needs_game_logs(snap: Snapshot, cfg: Config) -> List[PlayerView]:
    out: Dict[int, PlayerView] = {}
    for inj, mates in pairings(snap, cfg):
        for p in [inj] + mates:
            out.setdefault(p.id, p)
    return list(out.values())


def missed_games(inj: PlayerView, mates: List[PlayerView]) -> Set[int]:
    """Games his team has played this season without him. A game counts as played
    once a teammate has a stat line for it, so tonight's unplayed game never does."""
    played = {sp for p in mates for sp in (p.game_log or {}) if sp in inj.games}
    return played - set(inj.game_log or {})


def opportunities(snap: Snapshot, cfg: Config) -> List[Opportunity]:
    """Every free agent teammate with at least one game played while the injured player sat."""
    out: List[Opportunity] = []
    for inj, mates in pairings(snap, cfg):
        if inj.game_log is None:
            continue
        missed = missed_games(inj, mates)
        if not missed:
            continue
        for p in mates:
            if p.game_log is None:
                continue
            with_him, without_him = Split(), Split()
            for sp in sorted(p.game_log):
                if sp not in inj.games:
                    continue
                (without_him if sp in missed else with_him).add(p.game_log[sp])
            if without_him.games:
                out.append(Opportunity(inj, p, with_him, without_him))
    return out


def fresh_injuries(snap: Snapshot, cfg: Config) -> List[Tuple[PlayerView, List[PlayerView]]]:
    """Injured players whose team hasn't played without them yet, so there's no
    game log to go on and any bump for their backups is an estimate."""
    return [
        (inj, mates)
        for inj, mates in pairings(snap, cfg)
        if inj.game_log is not None and not missed_games(inj, mates)
    ]
