"""How the bot values a player over the next few days.

expected value = sum over upcoming games of
    availability on that day x (per game value x opponent + depth boost on that day)

per game value  projection.py: expected minutes times fantasy points per minute, from his
                game log, last season and ESPN's projection. Players without a game log
                fall back to the old blend of recent form, season average, ESPN's
                projection and Hashtag's stats (per_game_value), which is also what other
                managers go by when the trade scout guesses how they value a player.
opponent        how many fpts that night's opponent gives up compared to what the players
                facing it were projected for, pulled toward 1 until the sample is big.
depth boost     is the extra production a player should get while an injured
                teammate who shares his position is sitting out.
availability    is 0 while a player is out, a discount when day-to-day, else 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence

from . import projection
from .config import Config, Weights
from .model import PlayerView, Snapshot


# ESPN's projection counts as much as this many real games. Its weight fades as a
# player's games pile up, so a preseason guess can't hold down a breakout for long.
PROJECTION_GAMES = 5

# Hashtag windows to blend in, in order of preference. Two weeks is recent enough to
# catch a new role and long enough that one game doesn't swing it.
HASHTAG_BLEND = ("last_14", "last_30", "season", "last_7")


def hashtag_behind(p: PlayerView) -> int:
    """Games ESPN has counted that Hashtag hasn't yet. Hashtag updates once a day, so a
    late West Coast game from last night can be missing."""
    season = p.hashtag.get("season")
    return max(0, p.games_played - season.games) if season else 0


def hashtag_fpts(p: PlayerView, scoring: Dict[str, float], windows: Sequence[str] = HASHTAG_BLEND) -> Optional[float]:
    """Hashtag's per game stats scored with the league's points, from the first of these
    windows he has. None in category leagues, where its 9-cat value doesn't translate into
    points, when he has no line, or when it's missing games ESPN already has (a big night
    would count against him)."""
    if not scoring or hashtag_behind(p):
        return None
    for window in windows:
        line = p.hashtag.get(window)
        if line and line.games:
            return line.fpts(scoring)
    return None


def per_game_value(
    p: PlayerView,
    cfg: Config,
    scoring: Optional[Dict[str, float]] = None,
    weights: Optional[Weights] = None,
    projection_games: int = PROJECTION_GAMES,
    hashtag_windows: Sequence[str] = HASHTAG_BLEND,
) -> float:
    """The daily scout's blend by default. The trade scout passes its own, leaning on the
    season and the projection to value a player over the rest of the season."""
    w = weights or cfg.weights
    played = len(p.game_log) if p.game_log is not None else p.games_played
    parts = [
        (p.fpts.get("last_7"), w.last_7),
        (p.fpts.get("last_15"), w.last_15),
        (p.fpts.get("season"), w.season),
        (p.fpts.get("projected"), w.projected * projection_games / (projection_games + played)),
        (hashtag_fpts(p, scoring or {}, hashtag_windows), w.hashtag),
    ]
    used = [(v, wt) for v, wt in parts if v is not None and wt > 0]
    if not used:
        return 0.0
    total_w = sum(wt for _, wt in used)
    return sum(v * wt for v, wt in used) / total_w


# Hashtag windows for the rest of season value: the whole season first.
ROS_HASHTAG = ("season", "last_30", "last_14", "last_7")


def rest_of_season(p: PlayerView, cfg: Config, scoring: Optional[Dict[str, float]] = None) -> float:
    """The old blend's rest of season value: the trade weights, which lean on the season and
    ESPN's projection. Scorer.ros uses it for players without a game log."""
    return per_game_value(p, cfg, scoring, cfg.trade_weights, cfg.trade_projection_games, ROS_HASHTAG)


def prior_of(p: PlayerView) -> projection.Prior:
    last = p.last_season
    return projection.Prior(
        fpts=last.fpts if last else 0.0,
        minutes=last.minutes if last else 0.0,
        games=last.games if last else 0,
        espn_fpg=p.fpts.get("projected") or None,
        espn_mpg=p.mpg.get("projected") or None,
    )


# Opponent factors are pulled toward 1 by this many fpts of projection, about ten games' worth
# of the players facing a team. Fit by `python -m fantasy_bot.backtest opponents`.
OPPONENT_SHRINK = 2000.0


def opponent_factors(snap: Snapshot) -> Dict[int, float]:
    """NBA team id -> what the players facing it scored over what they were projected for that
    morning, from every game log in the snapshot. Over 1 is a soft defense."""
    priors = {p.id: prior_of(p) for p in snap.all_players() if p.last_season}
    rate = projection.league_rate(priors)
    scored: Dict[int, float] = {}
    expected: Dict[int, float] = {}
    for p in snap.all_players():
        if not p.game_log:
            continue
        prior = prior_of(p)
        schedule = snap.opponents.get(p.pro_team_id, {})
        for sp, g in p.game_log.items():
            opp = schedule.get(sp)
            if opp is None or sp >= snap.today:
                continue
            pr = projection.project(p.game_log, sp, prior, rate, projection.DAILY)
            if pr is None or pr.fpg <= 0:
                continue
            scored[opp] = scored.get(opp, 0.0) + g.fpts
            expected[opp] = expected.get(opp, 0.0) + pr.fpg
    k = OPPONENT_SHRINK
    return {t: (scored[t] + k) / (expected[t] + k) for t in expected}


def minutes(p: PlayerView) -> float:
    for key in ("last_7", "last_15", "season", "projected"):
        if p.mpg.get(key):
            return p.mpg[key]
    return 0.0


SEASON_ENDING = ("season-ending", "season ending", "out for the season", "remainder of the season",
                 "rest of the season")


def out_for_season(p: PlayerView) -> bool:
    note = p.injury_note.lower()
    return any(s in note for s in SEASON_ENDING)


def is_out_on(p: PlayerView, day: Optional[date], unknown_back: Optional[date] = None) -> bool:
    """Out with no return date: out for good by default, which is safe over the daily scout's
    week. Over months that writes off a star sidelined for a week or two, so the trade scout
    passes unknown_back, the day such a player is assumed back, unless his injury note says
    his season is over."""
    if not p.is_out:
        return False
    if day is None:
        return True
    if p.expected_return is not None:
        return day < p.expected_return
    if unknown_back is None or out_for_season(p):
        return True
    return day < unknown_back


def availability_on(p: PlayerView, day: Optional[date], cfg: Config, unknown_back: Optional[date] = None,
                    risky_back: Optional[date] = None) -> float:
    """unknown_back: see is_out_on. risky_back: the day a day-to-day player with no return
    date is assumed healthy. Without it he's discounted every day, which is fine over the daily
    scout's week, but over the rest of the season it makes a nagging hamstring cost him 40% of
    every game until April. With it, the discount ends at his return date, or else risky_back."""
    if is_out_on(p, day, unknown_back):
        return 0.0
    if p.available_from is not None and day is not None and day < p.available_from:
        return 0.0  # still on waivers
    if p.is_risky:
        if risky_back is None or day is None:
            return cfg.dtd_availability
        return cfg.dtd_availability if day < (p.expected_return or risky_back) else 1.0
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
        priors: Dict[int, projection.Prior] = {}
        for p in snap.all_players():
            self.by_team.setdefault(p.pro_team_id, []).append(p)
            if p.last_season:
                priors[p.id] = prior_of(p)
        self.league_rate = projection.league_rate(priors)
        self._base: Dict[int, float] = {}
        self._ros: Dict[int, float] = {}
        self._form: Dict[int, float] = {}

    def project(self, p: PlayerView, params: projection.Params,
                skip: Iterable[int] = ()) -> Optional[projection.Projection]:
        """None without a game log, or with projection = "blend": then the old blend is all there is.
        skip: scoring periods to leave out of his log."""
        if p.game_log is None or self.cfg.projection != "minutes":
            return None
        log = p.game_log
        if skip:
            skip = set(skip)
            log = {sp: g for sp, g in log.items() if sp not in skip}
        return projection.project(log, self.snap.today, prior_of(p), self.league_rate, params)

    def base(self, p: PlayerView) -> float:
        """Per game value over the next week or so."""
        if p.id not in self._base:
            pr = self.project(p, projection.DAILY)
            self._base[p.id] = pr.fpg if pr else self.form(p)
        return self._base[p.id]

    def base_with(self, p: PlayerView, injured: Iterable[PlayerView]) -> float:
        """Per game value in his usual role, with these injured teammates playing: his projection
        from only the games they played too. The minutes model reacts fast, so once a starter has
        sat a few games his backup's plain projection already carries the bigger role, which is
        wrong for the days after the starter is back, and double counts a depth boost."""
        missed = {sp for inj in injured if inj.game_log is not None
                  for sp in (p.game_log or {}) if sp in inj.games and sp not in inj.game_log}
        if not missed:
            return self.base(p)
        pr = self.project(p, projection.DAILY, missed)
        return pr.fpg if pr else self.base(p)

    def ros(self, p: PlayerView) -> float:
        """Per game value over the rest of the season. Ignores injuries: it's what he's worth
        when he plays."""
        if p.id not in self._ros:
            pr = self.project(p, projection.REST_OF_SEASON)
            self._ros[p.id] = pr.fpg if pr else rest_of_season(p, self.cfg, self.snap.scoring)
        return self._ros[p.id]

    def form(self, p: PlayerView) -> float:
        """The old blend, heavy on the last week or two: a worse projection, but what other
        managers go by, so the trade scout uses it to guess what they'll pay."""
        if p.id not in self._form:
            self._form[p.id] = per_game_value(p, self.cfg, self.snap.scoring)
        return self._form[p.id]

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
        openings = self.openings_for(p)
        # The boost is on top of his usual role, not one the injury already gave him.
        base = self.base_with(p, [o.injured for o in openings]) if openings else self.base(p)
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
            expected += avail * (base * self.snap.opponent_factor(p, sp) + boost)
        return Valuation(
            player=p,
            base=base,
            expected=expected,
            games=games,
            plays_today=self.snap.today in p.games,
            openings=openings,
        )
