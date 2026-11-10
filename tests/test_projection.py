import gzip
import json
from datetime import date, datetime, timedelta

import pytest

from fantasy_bot import backtest
from fantasy_bot.config import Config
from fantasy_bot.model import GameLog, PlayerView, SeasonTotals, Snapshot
from fantasy_bot.projection import DAILY, Params, Prior, league_rate, project
from fantasy_bot.scoring import Scorer, opponent_factors

# No fading and a light prior, so the arithmetic is easy to follow.
FLAT = Params(rate_half_life=1e9, rate_prior_minutes=100.0, minutes_half_life=1e9, minutes_prior_games=1.0,
              espn_share=0.0, prior_shrink_minutes=0.0, rookie_rate=0.8)


def logs(*games, start=1):
    """logs((20, 25), (30, 30)) -> games on days 1 and 2."""
    return {start + i: GameLog(f, m) for i, (f, m) in enumerate(games)}


# ---- the model -----------------------------------------------------------------------
def test_nothing_to_go_on_is_no_projection():
    assert project({}, 10, Prior(), 1.0, DAILY) is None


def test_espn_projection_alone_is_the_projection():
    pr = project({}, 10, Prior(espn_fpg=30.0, espn_mpg=30.0), 1.0, DAILY)
    assert pr.fpg == pytest.approx(30.0) and pr.mpg == pytest.approx(30.0) and pr.rate == pytest.approx(1.0)


def test_rate_blends_prior_minutes_with_this_seasons_minutes():
    # Last season 1.0 per minute over 1000 minutes; this season 300 minutes at 1.5.
    prior = Prior(fpts=1000.0, minutes=1000.0, games=40)
    pr = project(logs(*[(45, 30)] * 10), 20, prior, 1.0, FLAT)
    assert pr.rate == pytest.approx((100 * 1.0 + 450) / (100 + 300))
    assert pr.mpg == pytest.approx((1 * 25 + 300) / 11)  # last season's 25 mpg counts as one game


def test_a_bigger_role_moves_the_projection_more_than_a_hot_shooting_week():
    prior = Prior(fpts=20000.0, minutes=20000.0, games=1000)  # a big sample at 1.0 per minute, 20 mpg
    past = [(20, 20)] * 20
    more_minutes = project(logs(*past, *[(32, 32)] * 4), 25, prior, 1.0, DAILY)
    hot_shooting = project(logs(*past, *[(32, 20)] * 4), 25, prior, 1.0, DAILY)
    # Same 32 fpts a night either way, but the minutes count for more than twice as much.
    assert more_minutes.fpg - 20 > 2 * (hot_shooting.fpg - 20) > 0
    assert hot_shooting.mpg == pytest.approx(20)


def test_games_from_today_on_are_left_out():
    log = logs((10, 20), (90, 40), start=9)  # day 10 is tonight
    assert project(log, 10, Prior(), 1.0, FLAT) == project(logs((10, 20), start=9), 10, Prior(), 1.0, FLAT)


def test_small_samples_last_season_are_pulled_toward_the_league():
    params = Params(1e9, 100.0, 1e9, 1.0, espn_share=0.0, prior_shrink_minutes=300.0)
    lucky = Prior(fpts=200.0, minutes=100.0, games=5)  # 2.0 per minute in 100 minutes
    pr = project({}, 1, lucky, 1.0, params)
    assert pr.rate == pytest.approx((200 + 300) / (100 + 300))


def test_rookies_start_below_the_league_rate():
    pr = project(logs((20, 20)), 5, Prior(), 1.0, FLAT)
    assert pr.rate == pytest.approx((100 * 0.8 + 20) / 120)


def test_league_rate_weights_by_minutes():
    assert league_rate({1: Prior(fpts=100, minutes=100), 2: Prior(fpts=300, minutes=100)}) == 2.0
    assert league_rate({}) == 1.0


# ---- in the bot ----------------------------------------------------------------------
TODAY = 10
DATES = {sp: date(2026, 10, 28) + timedelta(days=sp - TODAY) for sp in range(1, 15)}


def pv(pid, team_id, game_log=None, fpg=20.0, mpg=20.0, sched=(10, 11), **kw):
    return PlayerView(
        id=pid, name=f"P{pid}", pro_team="X", pro_team_id=team_id, eligible_slots=["C", "UT", "BE"],
        fpts={"season": fpg, "last_7": fpg, "projected": fpg}, mpg={"season": mpg, "projected": mpg},
        games={sp: datetime.combine(DATES[sp], datetime.min.time()) for sp in sched}, game_log=game_log, **kw,
    )


def cfg(**kw):
    return Config(league_id=1, year=2027, team_id=1, **kw)


def snap_of(players, **kw):
    return Snapshot(TODAY, [10, 11, 12, 13], "Mine", players, [], [], DATES, **kw)


def test_scorer_projects_from_the_game_log_and_falls_back_to_the_blend_without_one():
    breakout = pv(1, 1, game_log={sp: GameLog(36, 34) for sp in range(1, 10)}, fpg=20, mpg=20)
    no_log = pv(2, 1, fpg=20, mpg=20)
    s = Scorer(snap_of([breakout, no_log]), cfg())
    assert s.base(breakout) > 30  # his minutes went up, ESPN's projection hasn't caught up
    assert s.base(no_log) == pytest.approx(20)
    assert s.form(breakout) < s.base(breakout)  # the old blend still leans on the projection
    blend = Scorer(snap_of([breakout, no_log]), cfg(projection="blend"))
    assert blend.base(breakout) == pytest.approx(s.form(breakout))


def test_usual_role_leaves_out_the_games_the_injured_starter_missed():
    starter = pv(1, 1, game_log={sp: GameLog(40, 34) for sp in (2, 4)}, sched=(2, 4, 6, 8, 9, 10),
                 injury_status="OUT")
    backup = pv(2, 1, sched=(2, 4, 6, 8, 9, 10), fpg=12, mpg=15,
                game_log={2: GameLog(12, 15), 4: GameLog(12, 15), 6: GameLog(32, 32), 8: GameLog(32, 32),
                          9: GameLog(32, 32)})
    s = Scorer(snap_of([starter, backup]), cfg())
    assert s.base(backup) > 20  # three big games in a row
    assert s.base_with(backup, [starter]) == pytest.approx(12, abs=0.5)  # with him back: his usual 12


def test_opponent_factors_find_the_soft_defense_and_scale_the_week():
    # Everyone scores their usual 20 against team 8, and 30 against team 9.
    sched = {1: 8, 2: 9, 3: 8, 4: 9, 5: 8, 6: 9, 10: 9, 11: 8}
    players = [pv(i, 1, game_log={sp: GameLog(20 if sched[sp] == 8 else 30, 25) for sp in range(1, 7)},
                  fpg=25, mpg=25) for i in range(1, 4)]
    snap = snap_of(players, opponents={1: sched})
    snap.opp_factors = opponent_factors(snap)
    assert snap.opp_factors[9] > 1 > snap.opp_factors[8]
    assert 1 < snap.opp_factors[9] < 30 / 25  # pulled toward 1 on a small sample
    s = Scorer(snap, cfg())
    p = players[0]
    v = s.value(p, [10, 11])
    assert v.expected == pytest.approx(s.base(p) * (snap.opp_factors[9] + snap.opp_factors[8]))


def test_no_opponent_data_means_no_adjustment():
    p = pv(1, 1)
    assert snap_of([p]).opponent_factor(p, 10) == 1.0


def test_projection_setting_is_checked(tmp_path):
    from fantasy_bot.config import load_config

    path = tmp_path / "bot.toml"
    path.write_text('[league]\nleague_id = 1\nyear = 2027\nteam_id = 1\n[bot]\nprojection = "magic"\n')
    with pytest.raises(SystemExit, match="projection"):
        load_config(path, tmp_path / "none.toml")


# ---- ESPN adapter ----------------------------------------------------------------------
def test_adapter_reads_last_season_and_opponents():
    from types import SimpleNamespace

    from fantasy_bot.espn_adapter import EspnSource

    src = object.__new__(EspnSource)
    src.cfg, src.points_league = cfg(), True
    card = {"player": {"stats": [
        {"seasonId": 2026, "statSplitTypeId": 0, "statSourceId": 0, "appliedTotal": 2100.0,
         "stats": {"40": 2000.0, "42": 70}},  # MIN, GP
        {"seasonId": 2026, "statSplitTypeId": 0, "statSourceId": 1, "appliedTotal": 9.0, "stats": {"40": 1.0}},
    ]}}
    assert src._last_season(card) == SeasonTotals(2100.0, 2000.0, 70)
    assert src._last_season({"player": {"stats": []}}) is None

    src.league = SimpleNamespace(pro_schedule={
        "13": {"10": [{"homeProTeamId": 13, "awayProTeamId": 2, "date": 0}], "11": []},
        "2": {"10": [{"homeProTeamId": 13, "awayProTeamId": 2, "date": 0}]},
    })
    assert src._build_opponents() == {13: {10: 2}, 2: {10: 13}}


# ---- backtest ------------------------------------------------------------------------
def _season_file(path, year, players):
    with gzip.open(path / f"{year}.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"year": year, "players": players, "schedule": {"1": {str(d): [2, True] for d in range(1, 40)}}}, f)


def test_backtest_loads_a_season_with_last_seasons_totals_and_scores_models(tmp_path):
    box = lambda pts, mins: {"PTS": pts, "MIN": mins}
    steady = [[d, 1, box(20, 30)] for d in range(1, 31)]
    _season_file(tmp_path, 2025, {"7": {"name": "Steady", "games": steady, "prior": None, "proj": None}})
    _season_file(tmp_path, 2026, {"7": {"name": "Steady", "games": steady, "prior": None,
                                        "proj": {"GP": 70, "MIN": 2100, "PTS": 1400}}})
    s = backtest.load(2026, {"PTS": 1}, tmp_path)
    p = s.players[0]
    assert p.prior.games == 30 and p.prior.fpts == 600 and p.prior.espn_fpg == 20
    cs = backtest.cases(s, 7)
    assert cs and all(c.actual == 20 for c in cs)
    assert backtest.score(s, cs, backtest.new_model(DAILY)).mae == pytest.approx(0, abs=1e-9)
    old = backtest.old_model(backtest.Weights(), 5, ("last_14", "season"), {"PTS": 1})
    assert backtest.score(s, cs, old).mae == pytest.approx(0, abs=1e-9)
