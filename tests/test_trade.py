from datetime import date, datetime, timedelta

import pytest
from espn_api.basketball.player import Player

from fantasy_bot.config import Config, load_config
from fantasy_bot.espn_adapter import EspnSource
from fantasy_bot.lineup import project
from fantasy_bot.model import PlayerView, Snapshot
from fantasy_bot.report import render_trades
from fantasy_bot.scoring import per_game_value
from fantasy_bot.trade import Lineups, Trade, Valued, _distinct, _swap, build_trades, trade_days

TODAY = 10
END = 37  # four weeks of games from tomorrow
DATES = {sp: date(2026, 11, 8) + timedelta(days=sp - TODAY) for sp in range(1, END + 1)}
SLOTS = {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "UT": 1, "BE": 3}

G = ["PG", "SG", "G", "UT", "BE", "IR"]
F = ["SF", "PF", "F", "UT", "BE", "IR"]
C = ["C", "UT", "BE", "IR"]


def every_other_day(team_id):
    return {sp: datetime.combine(DATES[sp], datetime.min.time()) for sp in range(team_id % 2 + 1, END + 1, 2)}


def tp(pid, name, slots, last_7, last_15, season, projected, team_id=0, owner="other", pro=2, **kw):
    return PlayerView(
        id=pid, name=name, pro_team="BOS", pro_team_id=pro, eligible_slots=slots,
        fpts={"last_7": last_7, "last_15": last_15, "season": season, "projected": projected},
        mpg={"season": 30}, games_played=10, games=every_other_day(pro), owner=owner, team_id=team_id, **kw,
    )


def steady(pid, name, slots, fpts, **kw):
    return tp(pid, name, slots, fpts, fpts, fpts, fpts, **kw)


def cfg(**kw):
    return Config(league_id=1, year=2027, team_id=1, **kw)


def league():
    mine = [
        tp(1, "Hot Guard", G, 45, 44, 42, 28, owner="me"),  # hot start, projected far lower
        steady(2, "Steady Forward", F, 30, owner="me"),
        steady(3, "Steady Center", C, 30, owner="me"),
        steady(4, "Untouchable Star", G, 50, owner="me"),
        steady(5, "Bench Guy", G, 18, owner="me"),
    ]
    rival = [
        tp(10, "Cold Wing", G + ["SF"], 25, 27, 29, 55, team_id=2),  # slow start, projected far higher
        steady(11, "Their Star", F, 45, team_id=2),
        steady(12, "Rival Center", C, 28, team_id=2),
        steady(13, "Rival Guard", G, 26, team_id=2),
        steady(14, "Rival Bench", F, 15, team_id=2),
    ]
    other = [
        tp(20, "Slumping Big", C, 20, 22, 24, 40, team_id=3),
        steady(21, "Other Guard", G, 33, team_id=3),
        steady(22, "Other Forward", F, 31, team_id=3),
        steady(23, "Other Wing", F, 27, team_id=3),
        steady(24, "Other Bench", G, 14, team_id=3),
    ]
    fas = [steady(30, "Free Agent", F, 16, owner="fa"), steady(31, "Hurt Free Agent", F, 40, owner="fa", injury_status="OUT")]
    return Snapshot(
        TODAY, [10, 11, 12, 13], "Mine", mine, rival + other, fas, DATES,
        move_horizon=[11, 12, 13, 14], lineup_slots=SLOTS, season_end=END,
        team_names={1: "Mine", 2: "Rival", 3: "Other Team"},
    )


def names(vs):
    return [v.player.name for v in vs]


# ---- values ----------------------------------------------------------------------
def test_rest_of_season_value_regresses_a_hot_start_toward_the_projection():
    c = cfg()
    hot, cold = league().my_roster[0], league().other_rostered[0]
    now, ros = per_game_value(hot, c), per_game_value(hot, c, None, c.trade_weights, c.trade_projection_games)
    assert 28 < ros < now < 45
    now, ros = per_game_value(cold, c), per_game_value(cold, c, None, c.trade_weights, c.trade_projection_games)
    assert 25 < now < ros < 55


def test_lineup_totals_match_the_daily_scouts_projection():
    snap, c = league(), cfg()
    days = trade_days(snap)
    assert days[0] == 11 and days[-1] == END
    value = lambda p: p.fpts["season"]
    roster = snap.my_roster + snap.other_rostered[:3]  # more than fit, so some sit
    lineups = Lineups(snap, c, days, value)
    expected = project([(p, value(p)) for p in roster], days, snap, c).total
    assert lineups.total(roster) == pytest.approx(expected)
    assert lineups.total(roster) == pytest.approx(expected)  # from the cache
    assert lineups.solo(snap.my_roster[1]) == 30 * 14


# ---- trades ----------------------------------------------------------------------
def test_buys_low_and_sells_high():
    r = build_trades(league(), cfg(untouchable=["Untouchable Star"]))
    assert r.trades
    assert "Cold Wing" in names(r.trades[0].get)
    assert names(r.sell_high) == ["Hot Guard"]
    assert [v.player.name for v, _ in r.buy_low][:1] == ["Cold Wing"] and r.buy_low[0][1] == "Rival"
    hot = next(t for t in r.trades if "Hot Guard" in names(t.give))
    assert any(reason.startswith("Sell high: Hot Guard") for reason in hot.reasons)
    assert any(reason.startswith("Buy low: ") for reason in hot.reasons)


def test_every_trade_helps_you_and_looks_even_to_them():
    c = cfg(untouchable=["Untouchable Star"])
    r = build_trades(league(), c)
    for t in r.trades:
        assert t.gain >= c.trade_min_gain and t.their_gain >= 0
        assert t.total == pytest.approx(t.gain * len(trade_days(league())) / 7)


def test_fairness_lets_in_trades_that_look_worse_to_them():
    def their_gains(fairness):
        c = cfg(untouchable=["Untouchable Star"], trades_shown=50, trade_fairness=fairness)
        return [t.their_gain for t in build_trades(league(), c).trades]
    assert min(their_gains(0)) >= 0
    # Hot Guard for Cold Wing and Rival Center: 42 now plus a 16/g free agent for their 29 and 28
    assert min(their_gains(500)) < 0


def test_the_best_player_in_the_deal_goes_to_them():
    # Steady Forward and Steady Center for Their Star adds up for both sides, but they keep their star.
    for c in (cfg(untouchable=["Untouchable Star"], trades_shown=50), cfg(trades_shown=50, trade_fairness=500)):
        for t in build_trades(league(), c).trades:
            assert max(v.now for v in t.give) >= max(v.now for v in t.get)


def test_never_offers_an_untouchable():
    for t in build_trades(league(), cfg(untouchable=["Untouchable Star", "hot guard"], trade_fairness=500)).trades:
        assert not {"Untouchable Star", "Hot Guard"} & set(names(t.give))


def test_one_for_one_only():
    r = build_trades(league(), cfg(untouchable=["Untouchable Star"], trade_max_players=1, trades_shown=10))
    assert r.trades and all(len(t.give) == len(t.get) == 1 for t in r.trades)


def test_uneven_trades_drop_your_weakest_or_fill_the_spot_from_free_agents():
    snap = league()
    me, theirs = snap.my_roster, snap.other_rostered[:5]
    value = lambda p: p.fpts["season"]
    pool = [snap.free_agents[0]]
    # Two for one: you'd drop your weakest, never an untouchable or the player you just got.
    after, drop, add = _swap(me, me[1:2], theirs[:2], {4}, value, pool)
    assert names_of(drop) == ["Bench Guy"] and not add and len(after) == len(me)
    after, drop, add = _swap(me, me[1:2], [theirs[4], theirs[0]], {4}, value, pool)
    assert names_of(drop) == ["Bench Guy"]  # Rival Bench is worse, but you traded for him
    # One for two: the open spot goes to the best healthy free agent.
    after, drop, add = _swap(me, me[1:3], theirs[:1], set(), value, pool)
    assert names_of(add) == ["Free Agent"] and not drop and len(after) == len(me)


def names_of(ps):
    return [p.name for p in ps]


def test_each_trade_shown_is_its_own_idea():
    snap = league()
    v = {p.id: Valued(p, 0, 0) for p in snap.all_players()}

    def trade(team, give, get):
        return Trade(team, "", [v[i] for i in give], [v[i] for i in get], 5, 50, 0)

    chosen = [trade(2, [1], [10])]
    assert not _distinct(trade(3, [2], [10]), chosen)  # same player to get
    assert not _distinct(trade(2, [1, 5], [12]), chosen)  # same team, same player to give
    assert _distinct(trade(3, [1], [20]), chosen)  # selling him to another team instead


def test_says_none_when_no_trade_works():
    r = build_trades(league(), cfg(untouchable=["Untouchable Star"], trade_min_gain=1000))
    assert r.trades == []
    assert "None. No trade adds 1000+ fpts/week" in render_trades(r)


def test_render_trades_is_readable():
    text = render_trades(build_trades(league(), cfg(untouchable=["Untouchable Star"])))
    assert text.startswith("**Mine: trade scout** (rest of season, 27 days through Dec 5)")
    assert "1. Send **" in text and "fpts/week for you" in text and "On current form they see" in text
    assert "**Sell high**" in text and "- Hot Guard (BOS PG/SG): last 7 45.0, last 15 44.0" in text
    assert "**Buy low**" in text and "Cold Wing (BOS PG/SF/SG, Rival)" in text
    print(text)


# ---- config and adapter ------------------------------------------------------------
def test_reads_trade_settings(tmp_path):
    toml = tmp_path / "bot.toml"
    toml.write_text(
        "[league]\nleague_id = 1\nyear = 2027\nteam_id = 3\n"
        "[trade]\nmax_players = 1\nmin_gain = 5\nfairness = 2\nshown = 3\n[trade.weights]\nseason = 0.5\n",
        encoding="utf-8",
    )
    c = load_config(toml, tmp_path / "none.toml")
    assert (c.trade_max_players, c.trade_min_gain, c.trade_fairness, c.trades_shown) == (1, 5.0, 2.0, 3)
    assert c.trade_weights.season == 0.5 and c.trade_weights.projected == 0.45  # the rest keep their defaults


def test_adapter_attaches_recent_form_to_other_teams_players():
    from types import SimpleNamespace

    def split(code, avg):
        return {"seasonId": 2027, "id": f"{code}2027", "scoringPeriodId": 0, "appliedTotal": avg * 5,
                "appliedAverage": avg, "stats": {"0": avg, "40": 150}, "averageStats": {"0": avg, "40": 30}}

    def game(sp, fpts):
        return {"seasonId": 2027, "id": "002027", "scoringPeriodId": sp, "statSplitTypeId": 5, "statSourceId": 0,
                "appliedTotal": fpts, "stats": {"40": 30}}

    player = {"id": 7, "fullName": "Their Guard", "defaultPositionId": 1, "proTeamId": 2,
              "eligibleSlots": [0, 1, 3, 11, 12], "injuryStatus": "ACTIVE"}
    roster_entry = {"playerId": 7, "lineupSlotId": 0, "playerPoolEntry": {"player": {**player, "stats": [split("00", 30)]}}}
    card = {"id": 7, "player": {**player, "stats": [split("00", 30), split("01", 41), split("02", 36), game(4, 41)]}}
    calls = []

    class FakeRequests:
        def get_player_card(self, ids, final, additional_filters=None):
            calls.append(list(ids))
            return {"players": [card] if 7 in ids else []}

    src = object.__new__(EspnSource)
    src.cfg, src.points_league = Config(league_id=1, year=2027, team_id=1), True
    src.league = SimpleNamespace(finalScoringPeriod=160, espn_request=FakeRequests())
    src._games_by_team = {}
    v = src._view(Player(roster_entry, 2027), "other")
    assert "last_7" not in v.fpts  # the roster listing only has season totals
    src.attach_cards([v])
    assert v.fpts["last_7"] == 41 and v.fpts["last_15"] == 36 and v.fpts["season"] == 30
    assert set(v.game_log) == {4} and v.owner == "other"
    src.attach_cards([v])  # already has them: no second request
    assert calls == [[7]]
