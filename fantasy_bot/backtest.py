"""Backtest per game projections on past seasons, and fit the projection model's parameters.

    python -m fantasy_bot.backtest fetch 2025 2026        # download game logs (public, no login)
    python -m fantasy_bot.backtest compare 2025 2026      # how each model would have done
    python -m fantasy_bot.backtest fit 2025 --check 2026  # refit projection.DAILY and REST_OF_SEASON
    python -m fantasy_bot.backtest factors 2026           # do opponents and back-to-backs matter?

Every few days of a past season, each model projects every player from what was known that
morning: the games before it, last season and ESPN's projection. The projection is checked
against his average over the next week (the daily scout) or the rest of the season (trades), in
the games he played, since availability is handled separately. The error is the average miss in
fpts per game, weighted by games, so a player who played four games that week counts four times.

Data comes from ESPN's public league defaults, so no cookies are needed, scored with ESPN's
standard points unless --scoring gives your league's (JSON, e.g. '{"PTS": 1, "TO": -2}').

One caveat: ESPN's season projection is whatever ESPN shows for that season now. It looks like
the preseason projection (games played are projected, not actual), but if ESPN revised it during
the season, models that use it look a little better here than they were.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from bisect import bisect_left
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import mean
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from .config import Config, Weights, trade_weights
from .model import GameLog, HashtagLine, PlayerView
from .projection import DAILY, REST_OF_SEASON, Params, Prior, league_rate, project
from .scoring import PROJECTION_GAMES, per_game_value

URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{year}/segments/0/leaguedefaults/3"
SCHEDULE_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{year}"
CACHE = Path("data/backtest")
POOL = 700  # players fetched a season, by ownership: everyone who played real minutes
BATCH = 50
KEEP = ("PTS", "3PM", "3PA", "FGA", "FGM", "FTA", "FTM", "REB", "OREB", "DREB", "AST", "STL", "BLK", "TO",
        "DD", "TD", "PF", "GS", "MIN", "GP")
SINGLE_GAME, TOTAL, ACTUAL, PROJECTED = 5, 0, 0, 1
# ESPN's standard points league scoring. Same as espn_adapter.DEFAULT_POINTS, kept here so the
# backtest doesn't need espn-api's league code.
STANDARD = {"PTS": 1, "3PM": 1, "FGA": -1, "FGM": 2, "FTA": -1, "FTM": 1, "REB": 1, "AST": 2, "STL": 4, "BLK": 4,
            "TO": -2}


# ---- data ---------------------------------------------------------------------------
def _get(url: str, view: str, filt: Optional[dict] = None) -> dict:
    headers = {"x-fantasy-filter": json.dumps(filt)} if filt else {}
    r = requests.get(url, params={"view": view}, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()


def _stats(raw: dict) -> Dict[str, float]:
    from espn_api.basketball.constant import STATS_MAP

    named = {STATS_MAP.get(k, k): float(v) for k, v in (raw or {}).items()}
    return {k: named[k] for k in KEEP if k in named}


def fetch(year: int, pool: int = POOL, cache: Path = CACHE) -> Path:
    """Every game this season for the pool, last season's totals and ESPN's projection."""
    url = URL.format(year=year)
    listing = _get(url, "kona_player_info", {"players": {"limit": pool, "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}})
    ids = [p["id"] for p in listing.get("players", [])]
    players: Dict[str, dict] = {}
    for i in range(0, len(ids), BATCH):
        filt = {"players": {"filterIds": {"value": ids[i:i + BATCH]},
                            "filterStatsForTopScoringPeriodIds": {"value": 200, "additionalValue": [
                                f"00{year}", f"10{year}", f"00{year - 1}"]}}}
        for entry in _get(url, "kona_playercard", filt).get("players", []):
            p = entry.get("player", entry)
            row = {"name": p.get("fullName", ""), "games": [], "prior": None, "proj": None}
            for s in p.get("stats", []):
                season, source, split = s.get("seasonId"), s.get("statSourceId"), s.get("statSplitTypeId")
                st = _stats(s.get("stats"))
                if season == year and source == ACTUAL and split == SINGLE_GAME and st.get("MIN", 0) > 0:
                    row["games"].append([int(s["scoringPeriodId"]), int(s.get("proTeamId") or 0), st])
                elif season == year - 1 and source == ACTUAL and split == TOTAL and st.get("GP"):
                    row["prior"] = st
                elif season == year and source == PROJECTED and split == TOTAL and st.get("GP"):
                    row["proj"] = st
            row["games"].sort(key=lambda g: g[0])
            players[str(entry.get("id", p.get("id")))] = row
        print(f"  {year}: {len(players)}/{len(ids)} players", file=sys.stderr)
    schedule: Dict[str, Dict[str, list]] = {}
    for team in _get(SCHEDULE_URL.format(year=year), "proTeamSchedules_wl").get("settings", {}).get("proTeams", []):
        for sp, games in (team.get("proGamesByScoringPeriod") or {}).items():
            if games:
                g = games[0]
                home = g.get("homeProTeamId") == team["id"]
                schedule.setdefault(str(team["id"]), {})[sp] = [g["awayProTeamId"] if home else g["homeProTeamId"], home]
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{year}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"year": year, "players": players, "schedule": schedule}, f)
    return path


@dataclass
class Player:
    id: int
    name: str
    games: Dict[int, GameLog]  # scoring period -> game
    teams: Dict[int, int]  # scoring period -> his NBA team that day
    stats: Dict[int, Dict[str, float]]  # scoring period -> box score
    prior: Prior
    sps: List[int] = field(default_factory=list)  # sorted game days


@dataclass
class Season:
    year: int
    players: List[Player]
    schedule: Dict[int, Dict[int, Tuple[int, bool]]]  # team -> period -> (opponent, home)
    league_rate: float
    first: int
    last: int


def _fpts(stats: Dict[str, float], scoring: Dict[str, float]) -> float:
    return sum(stats.get(k, 0.0) * v for k, v in scoring.items())


def _read(year: int, cache: Path) -> Optional[dict]:
    path = cache / f"{year}.json.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _totals(games: List[list]) -> Dict[str, float]:
    """A season's box score totals from its games, with GP."""
    out: Dict[str, float] = {"GP": float(len(games))}
    for _, _, st in games:
        for k, v in st.items():
            if k != "GP":
                out[k] = out.get(k, 0.0) + v
    return out


def load(year: int, scoring: Dict[str, float], cache: Path = CACHE) -> Season:
    raw = _read(year, cache)
    if raw is None:
        raise SystemExit(f"No data for {year}: run  python -m fantasy_bot.backtest fetch {year}")
    # ESPN only returns last season's totals for the current season, so for past seasons they
    # come from last season's file.
    before = _read(year - 1, cache) or {"players": {}}
    players = []
    for pid, row in raw["players"].items():
        if not row["games"]:
            continue
        games = {sp: GameLog(_fpts(st, scoring), st["MIN"]) for sp, _, st in row["games"]}
        last = before["players"].get(pid)
        prior_st = row.get("prior") or (_totals(last["games"]) if last and last["games"] else {})
        proj = row.get("proj") or {}
        gp = int(proj.get("GP") or 0)
        prior = Prior(
            fpts=_fpts(prior_st, scoring) if prior_st else 0.0,
            minutes=prior_st.get("MIN", 0.0),
            games=int(prior_st.get("GP") or 0),
            espn_fpg=_fpts(proj, scoring) / gp if gp else None,
            espn_mpg=proj.get("MIN", 0.0) / gp if gp else None,
        )
        players.append(Player(int(pid), row["name"], games, {sp: t for sp, t, _ in row["games"]},
                              {sp: st for sp, _, st in row["games"]}, prior, sorted(games)))
    schedule = {int(t): {int(sp): (o, h) for sp, (o, h) in d.items()} for t, d in raw["schedule"].items()}
    sps = [sp for p in players for sp in p.sps]
    return Season(year, players, schedule, league_rate({p.id: p.prior for p in players}), min(sps), max(sps))


# ---- models -------------------------------------------------------------------------
# A model takes (season, player, morning of day t) and returns a per game value, or None.
Model = Callable[[Season, Player, int], Optional[float]]


def new_model(params: Params, use_espn: bool = True) -> Model:
    def run(s: Season, p: Player, t: int) -> Optional[float]:
        prior = p.prior if use_espn else replace(p.prior, espn_fpg=None, espn_mpg=None)
        pr = project(p.games, t, prior, s.league_rate, params)
        return pr.fpg if pr else None
    return run


def _window(p: Player, t: int, days: Optional[int]) -> List[int]:
    hi = bisect_left(p.sps, t)
    lo = 0 if days is None else bisect_left(p.sps, t - days)
    return p.sps[lo:hi]


def season_average(s: Season, p: Player, t: int) -> Optional[float]:
    sps = _window(p, t, None)
    if sps:
        return mean(p.games[sp].fpts for sp in sps)
    return p.prior.espn_fpg if p.prior.espn_fpg is not None else (p.prior.fpts / p.prior.games if p.prior.games else None)


def old_model(weights: Weights, projection_games: int, windows: Sequence[str], scoring: Dict[str, float]) -> Model:
    """The blend the bot used before this module: last 7 and 15 days, the season, ESPN's projection
    and Hashtag's per game stats (rebuilt from the box scores), run through scoring.per_game_value."""
    cfg = Config(league_id=1, year=1, team_id=1, weights=weights)
    spans = {"last_7": 7, "last_14": 14, "last_15": 15, "last_30": 30, "season": None}

    def run(s: Season, p: Player, t: int) -> Optional[float]:
        fpts, ht = {}, {}
        played = len(_window(p, t, None))
        for key in ("last_7", "last_15", "season"):
            sps = _window(p, t, spans[key])
            if sps:
                fpts[key] = mean(p.games[sp].fpts for sp in sps)
        if p.prior.espn_fpg is not None:
            fpts["projected"] = p.prior.espn_fpg
        for key in ("last_7", "last_14", "last_30", "season"):
            sps = _window(p, t, spans[key])
            if sps:
                stats = {k: mean(p.stats[sp].get(k, 0.0) for sp in sps) for k in scoring}
                ht[key] = HashtagLine(p.name, "", [], 0, len(sps), 0.0, stats)
        if not fpts and not ht:
            return None
        view = PlayerView(p.id, p.name, "", 0, [], fpts=fpts, games_played=played, hashtag=ht)
        view.game_log = {sp: p.games[sp] for sp in _window(p, t, None)}
        return per_game_value(view, cfg, scoring, weights, projection_games, windows)
    return run


# ---- scoring the models -------------------------------------------------------------
@dataclass
class Case:
    player: Player
    t: int
    actual: float  # average fpts per game over the window
    games: int
    early: bool  # in the season's first three weeks
    rotation: bool  # projected for 20+ minutes on what was known that morning


def cases(s: Season, horizon: Optional[int], step: int = 3) -> List[Case]:
    """(player, morning) pairs to score: every step days, players with a game in the window ahead.
    horizon None means the rest of the season."""
    out = []
    end = s.last - (horizon or 14)
    for t in range(s.first, end + 1, step):
        for p in s.players:
            hi = s.last + 1 if horizon is None else t + horizon
            ahead = p.sps[bisect_left(p.sps, t):bisect_left(p.sps, hi)]
            if not ahead:
                continue
            before = _window(p, t, None)
            mpg = mean(p.games[sp].minutes for sp in before) if before else (p.prior.mpg or p.prior.espn_mpg or 0.0)
            if not before and not p.prior.games and p.prior.espn_fpg is None:
                continue  # nothing to go on
            out.append(Case(p, t, mean(p.games[sp].fpts for sp in ahead), len(ahead), t - s.first < 21, mpg >= 20))
    return out


@dataclass
class Score:
    mae: float
    bias: float  # actual minus projected, on average
    n: int

    def __str__(self) -> str:
        return f"{self.mae:6.2f} ({self.bias:+5.2f})"


def predict(s: Season, cs: Iterable[Case], model: Model) -> Dict[Tuple[int, int], float]:
    return {(c.player.id, c.t): model(s, c.player, c.t) or 0.0 for c in cs}


def score(s: Season, cs: Iterable[Case], model: Model, preds: Optional[Dict[Tuple[int, int], float]] = None) -> Score:
    err = bias = w = 0.0
    n = 0
    for c in cs:
        v = preds[(c.player.id, c.t)] if preds is not None else model(s, c.player, c.t) or 0.0
        err += c.games * abs(c.actual - v)
        bias += c.games * (c.actual - v)
        w += c.games
        n += 1
    return Score(err / w, bias / w, n) if w else Score(0.0, 0.0, 0)


def models(scoring: Dict[str, float]) -> Dict[str, Tuple[Model, Model]]:
    """Name -> (next week model, rest of season model)."""
    from .scoring import HASHTAG_BLEND, ROS_HASHTAG

    old = old_model(Weights(), PROJECTION_GAMES, HASHTAG_BLEND, scoring)
    old_ros = old_model(trade_weights(), 20, ROS_HASHTAG, scoring)
    return {
        "season average": (season_average, season_average),
        "old blend": (old, old_ros),
        "new: no ESPN": (new_model(DAILY, False), new_model(REST_OF_SEASON, False)),
        "new": (new_model(DAILY), new_model(REST_OF_SEASON)),
    }


def compare(years: Sequence[int], scoring: Dict[str, float]) -> None:
    ms = models(scoring)
    for year in years:
        s = load(year, scoring)
        for label, horizon, idx in (("next 7 days", 7, 0), ("rest of season", None, 1)):
            cs = cases(s, horizon)
            groups = {
                "all": cs,
                "first 3 weeks": [c for c in cs if c.early],
                "after": [c for c in cs if not c.early],
                "rotation (20+ mpg)": [c for c in cs if c.rotation],
            }
            print(f"\n{year - 1}-{year % 100:02d}, {label}: average miss in fpts per game (bias), {len(cs)} cases")
            print(f"  {'model':<16}" + "".join(f"{g:>22}" for g in groups))
            for name, pair in ms.items():
                preds = predict(s, cs, pair[idx])
                print(f"  {name:<16}" + "".join(f"{str(score(s, g, pair[idx], preds)):>22}" for g in groups.values()))


# ---- fitting ------------------------------------------------------------------------
GRID = {
    "rate_half_life": [15.0, 30.0, 60.0, 120.0, 240.0, 1000.0],
    "rate_prior_minutes": [100.0, 200.0, 400.0, 600.0, 900.0, 1400.0, 2000.0],
    "minutes_half_life": [4.0, 7.0, 10.0, 15.0, 20.0, 30.0, 60.0, 1000.0],
    "minutes_prior_games": [0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 10.0],
    "espn_share": [0.0, 0.25, 0.5, 0.75, 1.0],
    "prior_shrink_minutes": [100.0, 300.0, 600.0],
    "rookie_rate": [0.6, 0.7, 0.8, 0.9, 1.0],
}


def fit(s: Season, cs: List[Case], start: Params, rounds: int = 3) -> Tuple[Params, float]:
    """Coordinate descent over GRID: one parameter at a time, keeping whatever lowers the miss."""
    best = start
    best_mae = score(s, cs, new_model(best)).mae
    for r in range(rounds):
        moved = False
        for name, values in GRID.items():
            for v in values:
                trial = replace(best, **{name: v})
                if trial == best:
                    continue
                mae = score(s, cs, new_model(trial)).mae
                if mae < best_mae - 1e-4:
                    best, best_mae, moved = trial, mae, True
        print(f"  round {r + 1}: {best_mae:.3f}  {best}", file=sys.stderr)
        if not moved:
            break
    return best, best_mae


def fit_cmd(train: Sequence[int], check: Sequence[int], scoring: Dict[str, float]) -> None:
    for label, horizon, start in (("DAILY", 7, DAILY), ("REST_OF_SEASON", None, REST_OF_SEASON)):
        print(f"\nfitting {label} on {list(train)}", file=sys.stderr)
        # Fit on the first training season; with more than one, check the rest too.
        s = load(train[0], scoring)
        cs = cases(s, horizon, step=4)
        params, mae = fit(s, cs, start)
        print(f"{label} = {params}")
        for year in list(train[1:]) + list(check):
            s2 = load(year, scoring)
            cs2 = cases(s2, horizon)
            print(f"  {year}: fitted {score(s2, cs2, new_model(params))}, current {score(s2, cs2, new_model(start))}")


# ---- opponents and back-to-backs ----------------------------------------------------
def factors(years: Sequence[int], scoring: Dict[str, float]) -> None:
    """Whether a player's games against a stingy defense, at home or on the second night of a
    back-to-back come in below his projection, on the games after each morning (a week ahead)."""
    for year in years:
        s = load(year, scoring)
        model = new_model(DAILY)
        # Fpts allowed by each team relative to the scorers' projections, as of each day.
        allowed: Dict[int, List[Tuple[int, float, float]]] = {}  # team -> (day, actual, projected)
        rows = []
        for p in s.players:
            for sp in p.sps:
                team = p.teams.get(sp)
                opp_home = s.schedule.get(team, {}).get(sp)
                if opp_home is None:
                    continue
                v = model(s, p, sp)
                if not v or v < 10:
                    continue
                opp, home = opp_home
                b2b = (sp - 1) in s.schedule.get(team, {})
                allowed.setdefault(opp, []).append((sp, p.games[sp].fpts, v))
                rows.append((sp, opp, home, b2b, p.games[sp].fpts, v, p.games[sp].minutes))
        def opp_factor(opp: int, sp: int) -> Optional[float]:
            hist = [(a, v) for d, a, v in allowed.get(opp, []) if d < sp]
            if len(hist) < 150:
                return None
            return sum(a for a, _ in hist) / sum(v for _, v in hist)
        buckets: Dict[str, List[Tuple[float, float]]] = {}
        for sp, opp, home, b2b, actual, v, mins in rows:
            buckets.setdefault("home" if home else "away", []).append((actual, v))
            buckets.setdefault("2nd night of back-to-back" if b2b else "rested", []).append((actual, v))
            fac = opp_factor(opp, sp)
            if fac is not None:
                band = "tough defense (<0.97)" if fac < 0.97 else "soft defense (>1.03)" if fac > 1.03 else "average defense"
                buckets.setdefault(band, []).append((actual, v))
                buckets.setdefault("_fac", []).append((actual / v, fac))
        print(f"\n{year - 1}-{year % 100:02d}: actual / projected fpts, by game")
        for k, xs in sorted(buckets.items()):
            if k == "_fac":
                continue
            print(f"  {k:<28} {sum(a for a, _ in xs) / sum(v for _, v in xs):.3f}  ({len(xs)} games)")
        # Does scaling by the opponent factor lower the miss?
        pairs = [(a, v, opp_factor(o, sp)) for sp, o, _, _, a, v, _ in rows]
        pairs = [(a, v, f) for a, v, f in pairs if f is not None]
        if pairs:
            for k in (0.0, 0.5, 1.0):
                mae = mean(abs(a - v * (1 + k * (f - 1))) for a, v, f in pairs)
                print(f"  opponent adjustment x{k:.1f}: miss per game {mae:.3f}")


def opponent_table(s: Season, model: Model) -> Dict[int, Tuple[List[int], List[float], List[float]]]:
    """Per opponent, running totals by day of what the players facing it scored and were projected
    for: opponent -> (days, actual so far, projected so far)."""
    rows: Dict[int, List[Tuple[int, float, float]]] = {}
    for p in s.players:
        for sp in p.sps:
            opp = s.schedule.get(p.teams.get(sp, 0), {}).get(sp)
            v = model(s, p, sp)
            if opp is None or not v:
                continue
            rows.setdefault(opp[0], []).append((sp, p.games[sp].fpts, v))
    out = {}
    for opp, rs in rows.items():
        rs.sort()
        days, a, v = [], [], []
        ta = tv = 0.0
        for d, x, y in rs:
            ta += x
            tv += y
            days.append(d)
            a.append(ta)
            v.append(tv)
        out[opp] = (days, a, v)
    return out


def opponent_factor(table, opp: int, t: int, shrink: float) -> float:
    """What the opponent's gives up over what its opponents were projected for, from games before
    day t, pulled toward 1 by shrink fpts of projection."""
    days, a, v = table.get(opp, ([], [], []))
    i = bisect_left(days, t)
    if i == 0:
        return 1.0
    return (a[i - 1] + shrink) / (v[i - 1] + shrink)


SHRINKS = (0.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)


def opponents(years: Sequence[int], scoring: Dict[str, float]) -> None:
    """Next week's miss with each game scaled by its opponent's factor, by how hard the factor is
    pulled toward 1 (fpts of projection; a team game is roughly 200)."""
    model = new_model(DAILY)
    for year in years:
        s = load(year, scoring)
        table = opponent_table(s, model)
        cs = cases(s, 7)
        base = predict(s, cs, model)
        print(f"\n{year - 1}-{year % 100:02d}, next 7 days with opponents: miss (bias)")
        print(f"  {'shrink':<10}{'all':>16}{'first 3 weeks':>16}{'after':>16}")
        for k in (None,) + SHRINKS:
            err = {"all": [0.0, 0.0, 0.0], "early": [0.0, 0.0, 0.0], "late": [0.0, 0.0, 0.0]}
            for c in cs:
                v = base[(c.player.id, c.t)]
                ahead = c.player.sps[bisect_left(c.player.sps, c.t):bisect_left(c.player.sps, c.t + 7)]
                if k is not None and v:
                    facs = []
                    for sp in ahead:
                        opp = s.schedule.get(c.player.teams.get(sp, 0), {}).get(sp)
                        facs.append(opponent_factor(table, opp[0], c.t, k) if opp else 1.0)
                    v = v * mean(facs)
                for g in ("all", "early" if c.early else "late"):
                    e = err[g]
                    e[0] += c.games * abs(c.actual - v)
                    e[1] += c.games * (c.actual - v)
                    e[2] += c.games
            label = "none" if k is None else f"{k:.0f}"
            print(f"  {label:<10}" + "".join(f"{e[0] / e[2]:>9.3f} ({e[1] / e[2]:+.2f})" for e in err.values()))


def main(argv: Optional[Sequence[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m fantasy_bot.backtest", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["fetch", "compare", "fit", "factors", "opponents"])
    ap.add_argument("years", nargs="+", type=int, help="seasons by the year they end in (2025-26 = 2026)")
    ap.add_argument("--check", nargs="*", type=int, default=[], help="with fit: seasons to check the fit on")
    ap.add_argument("--scoring", help="your league's points per stat as JSON; ESPN standard points if left out")
    ap.add_argument("--pool", type=int, default=POOL, help="with fetch: players a season, by ownership")
    args = ap.parse_args(argv)
    scoring = json.loads(args.scoring) if args.scoring else STANDARD
    if args.command == "fetch":
        for y in args.years:
            print(f"wrote {fetch(y, args.pool)}")
    elif args.command == "compare":
        compare(args.years, scoring)
    elif args.command == "fit":
        fit_cmd(args.years, args.check, scoring)
    elif args.command == "opponents":
        opponents(args.years, scoring)
    else:
        factors(args.years, scoring)


if __name__ == "__main__":
    main()
