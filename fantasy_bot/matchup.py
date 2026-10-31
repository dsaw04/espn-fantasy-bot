"""Where you stand in the matchup, and how a move changes your chance to win it.

Both teams are projected the same way: each player's per game value over the
games he has left, with the best lineup each day (lineup.project). Points
already scored come from ESPN's live score, so games that have started are
left out of the projection.

The spread around a projection has two parts:
  game to game noise  each start varies by about cv x its value, independently
  estimate error      a player's per game value may itself be off by projection_error,
                      and that error repeats in every game he plays
Your chance to win is the normal probability that your final score beats theirs.

When there's no matchup to look at (category leagues, a bye, a failed request),
moves are judged on fpts over the next horizon_days instead.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, median, pstdev
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .lineup import Week, project
from .model import PlayerView, Snapshot

DEFAULT_CV = 0.35  # game to game spread as a share of a player's average, until his log says otherwise
DECIDED = 0.02  # a matchup this lopsided, either way, can't be swung by a pickup


def calendar(first: int, last: int, period_dates: Dict[int, date], matchup_weeks: Dict[int, List[int]]) -> Dict[int, List[int]]:
    """Scoring periods in each matchup. ESPN doesn't publish future matchup dates, only
    which of its weeks each matchup covers ({1: [1], ..., 20: [20, 21]}). Its weeks run
    Monday to Sunday, except that the All-Star break week and the week after it are one.
    The break is the longest run of days without games from January on (December has
    gaps too, but only because NBA Cup games are scheduled late)."""
    if not period_dates:
        return {}
    anchor = min(period_dates)

    def day(sp: int) -> date:
        return period_dates[anchor] + timedelta(days=sp - anchor)

    weeks: List[List[int]] = []
    for sp in range(first, last + 1):
        if weeks and day(sp).weekday() != 0:
            weeks[-1].append(sp)
        else:
            weeks.append([sp])

    n = max((w for ws in matchup_weeks.values() for w in ws), default=len(weeks))
    if len(weeks) > n:
        gap: List[int] = []
        run: List[int] = []
        for sp in range(first, last + 1):
            if sp not in period_dates and day(sp).month < 7:
                run.append(sp)
                if len(run) > len(gap):
                    gap = list(run)
            else:
                run = []
        if gap:
            i = next(k for k, w in enumerate(weeks) if gap[0] in w)
            j = next(k for k, w in enumerate(weeks) if gap[-1] in w)
            j = j if j > i else min(i + 1, len(weeks) - 1)
            weeks[i : j + 1] = [[sp for w in weeks[i : j + 1] for sp in w]]

    def game_days(w: List[int]) -> int:
        return sum(sp in period_dates for sp in w) or len(w)

    # Anything still off gets the sparsest weeks merged, so the counts at least line up.
    while len(weeks) > max(n, 1):
        i = min(range(len(weeks)), key=lambda k: (game_days(weeks[k]), -k))
        neighbours = [k for k in (i - 1, i + 1) if 0 <= k < len(weeks)]
        j = min(neighbours, key=lambda k: game_days(weeks[k]))
        lo, hi = sorted((i, j))
        weeks[lo : hi + 1] = [weeks[lo] + weeks[hi]]
    return {m: [sp for w in ws if 0 < w <= len(weeks) for sp in weeks[w - 1]] for m, ws in matchup_weeks.items()}


def check_calendar(cal: Dict[int, List[int]], played: Dict[int, Sequence]) -> None:
    """Warn if a period ESPN has already scored for a matchup falls outside it."""
    for m, sps in played.items():
        stray = {int(sp) for sp in sps} - set(cal.get(int(m), []))
        if stray:
            print(f"warning: matchup {m} has scoring periods {sorted(stray)} outside its computed dates", file=sys.stderr)


def adds_limit(acq: dict, n_days: int) -> int:
    """Acquisitions allowed in a matchup this many days long. ESPN stores "7 per matchup
    (for 7-day matchups)" as 1 per scoring period."""
    limit = acq.get("matchupAcquisitionLimit")
    if limit is None or limit < 0:
        return -1
    if acq.get("matchupLimitPerScoringPeriod"):
        return int(round(limit * n_days))
    return int(limit)


def game_cv(players: Sequence[PlayerView]) -> float:
    """Typical game to game spread as a share of a player's average, from game logs."""
    cvs = []
    for p in players:
        g = [x.fpts for x in (p.game_log or {}).values()]
        if len(g) >= 4 and mean(g) > 5:
            cvs.append(pstdev(g) / mean(g))
    return median(cvs) if cvs else DEFAULT_CV


def win_prob(lead: float, var: float) -> float:
    """Chance a projected lead holds up, given the variance of the difference."""
    if var <= 0:
        return 1.0 if lead > 0 else 0.0 if lead < 0 else 0.5
    return 0.5 * (1 + math.erf(lead / math.sqrt(2 * var)))


@dataclass
class Outlook:
    mine: float  # projected final score
    theirs: float
    var: float  # variance of mine minus theirs

    @property
    def win(self) -> float:
        return win_prob(self.mine - self.theirs, self.var)

    @property
    def decided(self) -> bool:
        return not DECIDED <= self.win <= 1 - DECIDED


@dataclass
class Verdict:
    """How good a roster you'd field from tomorrow on is."""

    week: Week  # your best lineups over every day judged
    now: Optional[Outlook] = None  # this matchup
    next: Optional[Outlook] = None  # the next one


Lineup = List[Tuple[PlayerView, float]]


class Judge:
    """Scores the roster a move would leave you with. With a matchup, that's your chance
    to win this week and next; without one, fpts over the next horizon_days."""

    def __init__(self, snap: Snapshot, cfg: Config, value_of: Callable[[PlayerView], float]):
        self.snap, self.cfg = snap, cfg
        m = snap.matchup
        self.matchup = m if m is not None and (m.next_opp is not None or any(sp > snap.today for sp in m.days)) else None
        if self.matchup is None:
            self.days = snap.move_days()
            return
        m = self.matchup
        self.cv = game_cv(snap.my_roster)
        self.rest = [sp for sp in m.days if sp > snap.today]  # days a move made now plays this week
        self.next_days = m.next_days if m.next_opp is not None else []
        self.days = self.rest + self.next_days

        def lineup(players):
            return [(p, value_of(p)) for p in players]

        today = [snap.today] if snap.today in m.days else []
        mine_today = project(lineup(snap.my_roster), today, snap, cfg, now=snap.now)
        theirs = project(lineup(m.opp.roster), today + self.rest, snap, cfg, now=snap.now)
        self.base_mine = m.me.score + mine_today.total
        self.base_theirs = m.opp.score + theirs.total
        self.base_var = self.spread(mine_today) + self.spread(theirs)
        if m.next_opp is not None:
            self.next_theirs = project(lineup(m.next_opp.roster), self.next_days, snap, cfg)

    def spread(self, w: Week) -> float:
        err = self.cfg.projection_error
        return self.cv**2 * w.sq + err**2 * sum(v * v for v in w.points.values())

    def __call__(self, lineup: Lineup) -> Verdict:
        if self.matchup is None:
            return Verdict(project(lineup, self.days, self.snap, self.cfg))
        rest = project(lineup, self.rest, self.snap, self.cfg)
        now = Outlook(self.base_mine + rest.total, self.base_theirs, self.base_var + self.spread(rest))
        if self.matchup.next_opp is None:
            return Verdict(rest, now)
        nxt = project(lineup, self.next_days, self.snap, self.cfg)
        t = self.next_theirs
        return Verdict(rest + nxt, now, Outlook(nxt.total, t.total, self.spread(nxt) + self.spread(t)))

    def gain(self, after: Verdict, before: Verdict) -> float:
        """Win chance added across both matchups, or fpts added without a matchup."""
        if self.matchup is None:
            return after.week.total - before.week.total
        g = after.now.win - before.now.win
        if after.next is not None:
            g += after.next.win - before.next.win
        return g

    def bar(self) -> float:
        """How much a streaming move has to gain. Leftover adds expire when the matchup
        ends, so the bar drops when you have more adds than days left, and rises when
        you're down to your last few early in the week."""
        if self.matchup is None:
            return self.cfg.stream_min_gain
        left = self.matchup.adds_left
        if left is None:
            return self.cfg.min_win_gain
        if left == 0:
            return math.inf
        return self.cfg.min_win_gain * min(2.0, max(0.5, len(self.rest) / left))

    def moves_allowed(self) -> int:
        left = self.matchup.adds_left if self.matchup else None
        return self.cfg.max_moves if left is None else min(self.cfg.max_moves, left)
