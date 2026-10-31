"""Turns valuations into concrete suggestions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Dict, List, Optional, Set, Tuple

from .config import Config
from .lineup import TodayPlan, Week, todays_lineup
from .matchup import Judge, Verdict
from .model import MatchupState, PlayerView, Snapshot, n_games
from .opportunity import Opportunity, fresh_injuries, opportunities
from .scoring import Role, Scorer, Valuation, availability_on, value_on


@dataclass
class Move:
    add: Valuation
    drop: Valuation
    kind: str  # "stream": replaces a streaming spot on expected fpts; "core": a per game upgrade
    per_game: float  # what the add should score per game in his current role
    drop_per_game: float
    gain: float  # how much the move raises your best lineups' total over the days judged
    edge: float  # per game, add minus drop
    opportunity: Optional[Opportunity] = None  # the injury that opened his role, if any
    win_gain: Optional[float] = None  # win chance added this week plus next, with a matchup
    before: Optional[Verdict] = None
    after: Optional[Verdict] = None
    add_starts: int = 0  # games the add would start over the horizon
    add_games: int = 0
    drop_starts: int = 0  # games the drop would have started
    drop_games: int = 0
    reasons: List[str] = field(default_factory=list)


@dataclass
class Watch:
    """A free agent whose role is growing but who doesn't clear the bar yet."""

    player: PlayerView
    injured: PlayerView
    per_game: float
    games_without: int  # games played without the injured player; 0 means per_game is an estimate
    edge: float  # per_game minus your weakest droppable player's per game value
    median: float = 0.0  # his typical game without the injured player


@dataclass
class Report:
    team_name: str
    today: int
    horizon_days: int
    injured_on_roster: List[PlayerView]
    lineup: TodayPlan
    moves: List[Move]
    roster: List[Valuation]  # everyone you could drop, weakest per game first
    watchlist: List[Watch]
    streamers: Set[int] = field(default_factory=set)  # ids of the streaming spots
    week: Optional[Week] = None  # your best lineups over the horizon, before any move
    outlook: Optional[Verdict] = None  # win chances before any move, with a matchup
    matchup: Optional[MatchupState] = None
    win_bar: float = 0.0  # win chance a streaming move had to add today
    # (day, starting slots your best lineup leaves empty) over the coming days, before any move
    open_spots: List[Tuple[date, int]] = field(default_factory=list)
    review: Optional[object] = None  # history.Review of last matchup, on the first report of a new one
    season: List[object] = field(default_factory=list)  # every history.Review so far
    projection_error: float = 0.0
    per_game: Dict[int, float] = field(default_factory=dict)  # your players' per game value in their current role
    min_edge: float = 0.0
    lineup_min_gain: float = 0.0
    stream_min_gain: float = 0.0
    moves_start_tomorrow: bool = False


def _norm(name: str) -> str:
    return " ".join(name.lower().replace(".", "").split())


def droppable(snap: Snapshot, cfg: Config) -> List[PlayerView]:
    """Everyone on the roster a move would change, except the untouchables."""
    safe = {_norm(n) for n in cfg.untouchable}
    return [p for p in snap.roster_for_moves() if _norm(p.name) not in safe]


OPEN_DAYS_SHOWN = 7

HASHTAG_WINDOWS = (("last_14", "last 14 days"), ("season", "season"), ("last_7", "last 7 days"))


def _hashtag_reason(add: PlayerView, drop: PlayerView, scoring: Dict[str, float]) -> Optional[str]:
    """Hashtag Basketball as a second opinion, from the first window both players have.
    In a points league its per game stats are scored with the league's own points, since
    its 9-cat rank weighs every category equally (a steal is worth 4 points here, not 1/9)."""
    for window, label in HASHTAG_WINDOWS:
        a, d = add.hashtag.get(window), drop.hashtag.get(window)
        if a and d:
            if scoring:
                return (
                    f"Hashtag stats in your scoring ({label}): {add.name} {a.fpts(scoring):.1f} fpts/g, "
                    f"{drop.name} {d.fpts(scoring):.1f}"
                )
            return f"Hashtag 9-cat rank ({label}): {add.name} #{a.rank}, {drop.name} #{d.rank}"
    return None


def _pos(p: PlayerView) -> str:
    return "/".join(sorted(p.positions)) or "?"


def _median_game(p: PlayerView) -> Optional[float]:
    return median(g.fpts for g in p.game_log.values()) if p.game_log else None


def _reasons(m: Move, horizon: int, scoring: Dict[str, float]) -> List[str]:
    a, d, opp = m.add.player, m.drop.player, m.opportunity
    r: List[str] = []
    if opp:
        inj, wo, w = opp.injured, opp.without_him, opp.with_him
        r.append(
            f"{inj.name} ({inj.pro_team} {_pos(inj)}) is {inj.status_label()}. Without him: "
            f"{', '.join(f'{x:.0f}' for x in wo.fpts)} fpts (avg {wo.avg:.1f}, {wo.mpg:.0f} min/g)"
        )
        if w.games:
            r.append(f"with him: {w.avg:.1f} fpts in {w.mpg:.0f} min/g over {n_games(w.games)}")
        else:
            r.append(f"{inj.name} hasn't played with him this season, so that's his whole season")
    r.append(
        f"next {horizon} days: {a.name} would start {m.add_starts} of his {n_games(m.add_games)} (~{m.per_game:.1f}/g), "
        f"{d.name} starts {m.drop_starts} of {m.drop_games} (~{m.drop_per_game:.1f}/g)"
    )
    if m.kind == "core" and opp and (dm := _median_game(d)) is not None:
        r.append(f"median game {opp.without_him.median:.0f} vs {dm:.0f} for {d.name}")
    if opp and opp.with_him.games:
        r.append(f"expect ~{opp.with_him.avg:.0f} fpts/g again once {opp.injured.name} returns")
    if a.is_risky:
        r.append(f"note: {a.name} is listed {a.status_label()}")
    if a.available_from:
        r.append(f"he's on waivers: put in a claim, and he's yours from {a.available_from:%b %d}")
    if (ht := _hashtag_reason(a, d, scoring)) is not None:
        r.append(ht)
    return r


def _role_value(fa: PlayerView, opps: List[Opportunity], cfg: Config, scorer: Scorer):
    """Per game value in the role he has now. With enough games played while an injured
    teammate sat, that's his actual average in those games. Otherwise his blended average."""
    solid = [o for o in opps if o.without_him.games >= cfg.min_games_without]
    if solid:
        best = max(solid, key=lambda o: o.without_him.avg)
        return best.without_him.avg, best
    return scorer.base(fa), None


def _expected(p: PlayerView, per_game, days: List[int], snap: Snapshot, cfg: Config) -> float:
    """Expected fpts over these days. Uses only observed value: the depth chart estimate
    is a guess, so it feeds the watch list, not moves."""
    total = 0.0
    for sp in days:
        if sp in p.games:
            day = snap.period_dates.get(sp)
            total += availability_on(p, day, cfg) * value_on(per_game, day)
    return total


def _role(p: PlayerView, per_game: float, opp: Optional[Opportunity], scorer: Scorer):
    """His role value lasts only until the injured teammate is due back."""
    if opp is None or opp.injured.expected_return is None:
        return per_game
    return Role(per_game, scorer.base(p), opp.injured)


def _consistent(opp: Optional[Opportunity], drop: PlayerView, drop_per_game: float) -> bool:
    """For a core upgrade built on an injury, his typical game without the starter has to
    beat the drop's typical game, so one huge night can't carry the average."""
    if opp is None:
        return True
    theirs = _median_game(drop)
    return opp.without_him.median > (theirs if theirs is not None else drop_per_game)


def waiver_moves(snap: Snapshot, cfg: Config, scorer: Scorer):
    """Your weakest few players are streaming spots: anyone who adds enough to your chance
    of winning this week and next replaces them (or, without a matchup, enough fpts over the
    horizon). Everyone else is core and only moves for a consistent per game upgrade of
    min_edge."""
    all_opps = opportunities(snap, cfg)
    opps_by_player: Dict[int, List[Opportunity]] = {}
    for o in all_opps:
        opps_by_player.setdefault(o.player.id, []).append(o)

    # Your players get the same role based value a free agent would.
    value: Dict[int, float] = {}
    roles: Dict[int, object] = {}
    for p in snap.roster_for_moves():
        v, opp = _role_value(p, opps_by_player.get(p.id, []), cfg, scorer)
        value[p.id], roles[p.id] = v, _role(p, v, opp, scorer)
    judge = Judge(snap, cfg, lambda p: value[p.id] if p.id in value else scorer.base(p))
    days = judge.days
    roster = [scorer.value(p, days) for p in droppable(snap, cfg)]
    roster.sort(key=lambda v: value[v.player.id])
    streamers = {v.player.id for v in roster[: cfg.streaming_spots]}
    # Everyone you'd field, untouchables included.
    lineup = [(p, roles[p.id]) for p in snap.roster_for_moves()]
    verdict = before = judge(lineup)
    bar = judge.bar()

    def swap(add: PlayerView, per_game, drop: Valuation):
        trial = [x for x in lineup if x[0].id != drop.player.id] + [(add, per_game)]
        return trial, judge(trial)

    candidates = []
    for p in snap.free_agents:
        if p.is_out or not p.games:
            continue
        v = scorer.value(p, days)
        if not v.games:
            continue  # no game before the horizon ends
        per_game, opp = _role_value(p, opps_by_player.get(p.id, []), cfg, scorer)
        role = _role(p, per_game, opp, scorer)
        candidates.append((_expected(p, role, days, snap, cfg), per_game, opp, v, role))
    candidates.sort(key=lambda c: c[0], reverse=True)

    moves: List[Move] = []
    remaining = list(roster)
    taken: Set[int] = set()
    bet_on: Set[int] = set()  # injured players a recommended add is already leaning on
    for expected, per_game, opp, add, role in candidates:
        if len(moves) >= judge.moves_allowed() or not remaining:
            break
        if opp and opp.injured.id in bet_on:
            continue  # don't stack two pickups on the same injury
        best: Optional[Move] = None
        best_score = 0.0
        best_trial = None
        # Streaming spots first, into whichever one the move helps most. On fpts, adding a
        # player can gain at most his own expected value, so skip hopeless ones.
        if expected > 0 and (judge.matchup is not None or expected >= bar):
            for drop in remaining:
                if drop.player.id not in streamers:
                    continue
                trial, after = swap(add.player, role, drop)
                score = judge.gain(after, verdict)
                if score >= bar and (best is None or score > best_score):
                    dv = value[drop.player.id]
                    best = Move(add, drop, "stream", per_game, dv, after.week.total - verdict.week.total, per_game - dv, opp)
                    best_score, best_trial = score, (trial, after)
        # Core players only for a consistent per game upgrade, judged per game and not on the
        # next few days, so an injured regular is never dropped for being worth 0 this week.
        if best is None and judge.moves_allowed() > 0:
            for drop in remaining:  # weakest per game first
                dv = value[drop.player.id]
                edge = per_game - dv
                if drop.player.id not in streamers and edge >= cfg.min_edge and _consistent(opp, drop.player, dv):
                    trial, after = swap(add.player, role, drop)
                    best = Move(add, drop, "core", per_game, dv, after.week.total - verdict.week.total, edge, opp)
                    best_score, best_trial = judge.gain(after, verdict), (trial, after)
                    break
        if best is None:
            continue
        trial, after = best_trial
        if judge.matchup is not None:
            best.win_gain, best.before, best.after = best_score, verdict, after
        best.add_starts, best.add_games = after.week.starts.get(add.player.id, 0), after.week.games.get(add.player.id, 0)
        best.drop_starts = verdict.week.starts.get(best.drop.player.id, 0)
        best.drop_games = verdict.week.games.get(best.drop.player.id, 0)
        best.reasons = _reasons(best, len(days), snap.scoring)
        lineup, verdict = trial, after  # the next move builds on this one
        moves.append(best)
        if opp:
            bet_on.add(opp.injured.id)
        taken.add(add.player.id)
        remaining.remove(best.drop)

    weakest = value[roster[0].player.id] if roster else 0.0
    fa_ids = {p.id for p in snap.free_agents}
    # Free agent roles that have grown in the game logs, whether or not they clear the bar yet.
    watch: List[Watch] = [
        Watch(o.player, o.injured, o.without_him.avg, o.without_him.games, o.without_him.avg - weakest,
              o.without_him.median)
        for o in all_opps
        if o.player.id in fa_ids and o.player.id not in taken and o.without_him.avg > scorer.base(o.player)
    ]
    # Injuries too fresh for game logs: fall back to the depth chart estimate.
    for inj, mates in fresh_injuries(snap, cfg):
        for p in mates:
            if p.id in taken or p.id not in fa_ids:
                continue
            boost = sum(o.boost for o in scorer.openings_for(p) if o.injured.id == inj.id)
            if boost >= 1.0:
                est = scorer.base(p) + boost
                watch.append(Watch(p, inj, est, 0, est - weakest))
    watch.sort(key=lambda w: w.per_game, reverse=True)
    return moves, roster, streamers, watch[:5], before, value, judge


def build_report(snap: Snapshot, cfg: Config) -> Report:
    scorer = Scorer(snap, cfg)
    moves, roster, streamers, watch, before, value, judge = waiver_moves(snap, cfg, scorer)
    return Report(
        team_name=snap.my_team_name,
        today=snap.today,
        horizon_days=len(judge.days),
        injured_on_roster=[p for p in snap.roster_for_moves() if p.is_out or p.is_risky],
        lineup=todays_lineup(snap, cfg, lambda p: value[p.id] if p.id in value else scorer.base(p)),
        lineup_min_gain=cfg.lineup_min_gain,
        projection_error=cfg.projection_error,
        moves=moves,
        roster=roster,
        watchlist=watch,
        streamers=streamers,
        week=before.week,
        outlook=before if judge.matchup is not None else None,
        matchup=judge.matchup,
        win_bar=judge.bar() if judge.matchup is not None else 0.0,
        open_spots=[(snap.period_dates[sp], n) for sp, n in sorted(before.week.open.items())[:OPEN_DAYS_SHOWN]
                    if n > 0 and sp in snap.period_dates],
        per_game=value,
        min_edge=cfg.min_edge,
        stream_min_gain=cfg.stream_min_gain,
        moves_start_tomorrow=snap.move_days()[:1] != snap.horizon[:1],
    )
