"""Per game projection from game logs: expected minutes times fantasy points per minute.

Splitting the two is the point. Points per minute is a skill and barely moves within a
season, so it gets a long memory and leans on last season until this one has a real sample.
Minutes are the role, and roles change overnight (a trade, an injury ahead of him, a new
starter), so minutes get a short memory. A backup's hot shooting night barely moves his
value; a backup who starts playing 30 minutes moves it right away.

    rate    = (prior_minutes x prior rate + sum of w x fpts) / (prior_minutes + sum of w x minutes)
    minutes = (prior_games x prior mpg  + sum of v x minutes) / (prior_games + sum of v)

w and v halve every rate_half_life and minutes_half_life days. The prior rate is last season's
points per minute, itself pulled toward the league's rate when last season was a small sample,
blended with ESPN's projection. Rookies get ESPN's projection, else a below average rate.

The parameters were fit by `python -m fantasy_bot.backtest` on last season's game logs and
checked on the season before. Rerun it to refit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from .model import GameLog


@dataclass(frozen=True)
class Params:
    rate_half_life: float  # days for a game's weight in points per minute to halve
    rate_prior_minutes: float  # minutes of play the prior rate counts as
    minutes_half_life: float  # days for a game's weight in minutes to halve
    minutes_prior_games: float  # games the prior minutes count as
    espn_share: float = 0.5  # share of the prior that comes from ESPN's projection, when there's both
    prior_shrink_minutes: float = 300.0  # last season's rate is pulled toward the league's by this many minutes
    rookie_rate: float = 0.8  # a player with no history's rate, as a share of the league's


# Next week's value, for pickups and lineups. Fit on 2024-25, checked on 2025-26.
DAILY = Params(rate_half_life=60.0, rate_prior_minutes=200.0, minutes_half_life=10.0, minutes_prior_games=0.5,
               espn_share=0.25, prior_shrink_minutes=100.0, rookie_rate=0.8)
# Value over the rest of the season, for trades. Fit the same way. Rookies get the league's rate
# here, not a discount: they get better as the season goes on.
REST_OF_SEASON = Params(rate_half_life=60.0, rate_prior_minutes=400.0, minutes_half_life=30.0, minutes_prior_games=4.0,
                        espn_share=0.5, prior_shrink_minutes=600.0, rookie_rate=1.0)


@dataclass(frozen=True)
class Prior:
    """What's known before this season's games: last season's totals and ESPN's projection."""

    fpts: float = 0.0  # last season's fantasy points, in total
    minutes: float = 0.0  # last season's minutes, in total
    games: int = 0  # last season's games played
    espn_fpg: Optional[float] = None  # ESPN's projected fantasy points per game this season
    espn_mpg: Optional[float] = None  # ESPN's projected minutes per game this season

    @property
    def mpg(self) -> Optional[float]:
        return self.minutes / self.games if self.games else None


@dataclass(frozen=True)
class Projection:
    fpg: float  # fantasy points per game
    rate: float  # fantasy points per minute
    mpg: float  # minutes per game


def _blend(a: Optional[float], b: Optional[float], b_share: float) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return (1 - b_share) * a + b_share * b


def prior_rate(prior: Prior, league_rate: float, params: Params) -> float:
    last = None
    if prior.minutes > 0:
        k = params.prior_shrink_minutes
        last = (prior.fpts + k * league_rate) / (prior.minutes + k)
    espn = prior.espn_fpg / prior.espn_mpg if prior.espn_fpg and prior.espn_mpg else None
    rate = _blend(last, espn, params.espn_share)
    return rate if rate is not None else params.rookie_rate * league_rate


def prior_mpg(prior: Prior, params: Params) -> Optional[float]:
    return _blend(prior.mpg, prior.espn_mpg or None, params.espn_share)


def _decay(days_ago: float, half_life: float) -> float:
    return 0.5 ** (days_ago / half_life)


def project(log: Mapping[int, GameLog], today: int, prior: Prior, league_rate: float,
            params: Params) -> Optional[Projection]:
    """Per game value from the games before today (keyed by scoring period, one a day).
    None when there's nothing to go on: no games, no last season and no ESPN projection."""
    f = m = m_w = m_sum = 0.0
    for sp, g in log.items():
        if sp >= today or g.minutes <= 0:
            continue
        w = _decay(today - sp, params.rate_half_life)
        f += w * g.fpts
        m += w * g.minutes
        v = _decay(today - sp, params.minutes_half_life)
        m_w += v
        m_sum += v * g.minutes
    mpg0 = prior_mpg(prior, params)
    if m_w == 0 and mpg0 is None:
        return None
    k = params.rate_prior_minutes
    rate = (k * prior_rate(prior, league_rate, params) + f) / (k + m)
    if mpg0 is None:
        mpg = m_sum / m_w
    else:
        g0 = params.minutes_prior_games
        mpg = (g0 * mpg0 + m_sum) / (g0 + m_w)
    return Projection(rate * mpg, rate, mpg)


def league_rate(priors: Mapping[int, Prior], default: float = 1.0) -> float:
    """Fantasy points per minute across everyone with a last season, the rate small samples
    are pulled toward."""
    f = sum(p.fpts for p in priors.values())
    m = sum(p.minutes for p in priors.values())
    return f / m if m > 0 else default
