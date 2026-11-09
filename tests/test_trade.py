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
        assert t.gain >= c.trade_min_gain and t.their_face >= 0
        assert t.total == pytest.approx(t.gain * len(trade_days(league())) / 7)


def test_fairness_lets_in_trades_that_look_worse_to_them():
    def their_faces(fairness):
        c = cfg(untouchable=["Untouchable Star"], trades_shown=50, trade_fairness=fairness)
        return [t.their_face for t in build_trades(league(), c).trades]
    assert min(their_faces(0)) >= 0
    # Hot Guard for Cold Wing and Rival Center: 42 now plus a 16/g free agent for their 29 and 28
    assert min(their_faces(500)) < 0


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
    # Two for one: you'd drop your weakest, never an untouchable.
    after, drop, add = _swap(me, me[1:2], theirs[:2], {4}, value, pool)
    assert names_of(drop) == ["Bench Guy"] and not add and len(after) == len(me)
    # The player you just got can be the one cut, when he's the weakest.
    after, drop, add = _swap(me, me[1:2], [theirs[4], theirs[0]], {4}, value, pool)
    assert names_of(drop) == ["Rival Bench"] and not add
    # One for two: the open spot goes to the best healthy free agent.
    after, drop, add = _swap(me, me[1:3], theirs[:1], set(), value, pool)
    assert names_of(add) == ["Free Agent"] and not drop and len(after) == len(me)
    # One for one: a player you just got is only cut for a clearly better free agent.
    after, drop, add = _swap(me, me[1:2], theirs[4:5], set(), value, pool)  # Rival Bench (15), a 16/g free agent
    assert not drop and not add
    better = steady(40, "Better FA", F, 20, owner="fa")
    after, drop, add = _swap(me, me[1:2], theirs[4:5], set(), value, [better])  # 20 is 25% more than 15
    assert names_of(drop) == ["Rival Bench"] and names_of(add) == ["Better FA"] and len(after) == len(me)
    # A free agent the other side already took isn't there.
    after, drop, add = _swap(me, me[1:3], theirs[:1], set(), value, [better, pool[0]], exclude={40})
    assert names_of(add) == ["Free Agent"]


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
    assert "1. Send **" in text and "fpts/week for you" in text and "To them it looks" in text
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


# ---- evaluating a trade you name ------------------------------------------------------
from fantasy_bot.report import render_evaluation  # noqa: E402
from fantasy_bot.trade import ROS_HASHTAG, TradeInputError, evaluate_trade, find_player, find_players  # noqa: E402


def test_finds_players_by_name_forgiving_case_accents_and_typos():
    pool = league().all_players()
    assert find_player("cold wing", pool)[0].id == 10
    assert find_player("  COLD-WING ", pool)[0].id == 10
    assert find_player("Cold Wing Jr.", pool)[0].id == 10
    assert find_player("slumping", pool) == (find_player("Slumping Big", pool)[0], None)  # one word, unique
    p, note = find_player("Rival Centr", pool)
    assert p.id == 12 and note == 'Read "Rival Centr" as Rival Center.'


def test_says_which_players_a_name_could_be():
    pool = league().all_players()
    with pytest.raises(TradeInputError, match="could be"):
        find_player("wing", pool)  # Cold Wing and Other Wing
    with pytest.raises(TradeInputError, match="No player named"):
        find_player("Zzyzx Qwerty", pool)


def test_checks_each_side_belongs_where_it_should():
    snap = league()
    give, get, notes = find_players(snap, ["hot guard"], ["cold wing", "rival center"])
    assert (give, get, notes) == ([1], [10, 12], [])
    with pytest.raises(TradeInputError) as e:
        find_players(snap, ["Cold Wing"], ["Free Agent", "Hot Guard"])
    msg = str(e.value)
    assert "Cold Wing is on Rival, not on your team" in msg
    assert "Free Agent is a free agent" in msg and "Hot Guard is already on your team" in msg
    with pytest.raises(TradeInputError, match="same team"):
        find_players(snap, ["Hot Guard"], ["Cold Wing", "Other Guard"])


def test_evaluates_a_trade_the_way_the_trade_scout_scores_it():
    snap, c = league(), cfg(untouchable=["Untouchable Star"], trades_shown=50, trade_fairness=500)
    days = trade_days(snap)
    ros = lambda p: per_game_value(p, c, snap.scoring, c.trade_weights, c.trade_projection_games, ROS_HASHTAG)
    lineups = Lineups(snap, c, days, ros)
    after = [p for p in snap.my_roster if p.id != 1] + [snap.other_rostered[0]]
    expected = (lineups.total(after) - lineups.total(snap.my_roster)) / (len(days) / 7)
    e = evaluate_trade(snap, c, [1], [10])  # Hot Guard for Cold Wing, one for one
    assert e.trade.gain == pytest.approx(expected) and e.trade.total == pytest.approx(expected * len(days) / 7)
    assert not e.trade.drop and not e.trade.add
    assert e.verdict == "good" and e.trade.team_name == "Rival"
    assert any(r.startswith("Buy low: Cold Wing") for r in e.trade.reasons)


def test_three_for_one_makes_room_on_both_sides():
    snap, c = league(), cfg()
    e = evaluate_trade(snap, c, [2, 3, 5], [11])  # three of yours for Their Star
    t = e.trade
    assert names(t.add) == ["Free Agent"]  # the only healthy free agent fills one of your two open spots
    # They take three for one and cut their two weakest, including Bench Guy (18) whom they just got.
    assert names(t.their_drop) == ["Rival Bench", "Bench Guy"]
    assert any("Their Star is the best player in the deal" in n for n in e.notes)


def test_warns_when_you_send_an_untouchable_and_never_cuts_one():
    e = evaluate_trade(league(), cfg(untouchable=["Untouchable Star"]), [4], [10, 12])
    assert "You've marked Untouchable Star untouchable." in e.notes
    e = evaluate_trade(league(), cfg(untouchable=["Untouchable Star"]), [5], [10, 12])
    assert "Untouchable Star" not in names(e.trade.drop)


def test_rejects_an_uneven_or_cross_team_trade_built_by_hand():
    with pytest.raises(TradeInputError):
        evaluate_trade(league(), cfg(), [], [10])
    with pytest.raises(TradeInputError):
        evaluate_trade(league(), cfg(), [1], [10, 20])  # two different teams


def test_render_evaluation_is_readable():
    text = render_evaluation(evaluate_trade(league(), cfg(), [1], [10]))
    assert text.startswith("**Trade check**: Hot Guard to Rival for Cold Wing")
    assert "fpts/week for you" in text and "for them at face value" in text and "**Details**" in text
    print(text)


# ---- alternatives with the same team ------------------------------------------------------
from fantasy_bot.trade import _pick_alternatives, shape_label, shape_of  # noqa: E402


def test_shapes():
    assert [shape_of(*n) for n in ((1, 1), (2, 2), (3, 1), (2, 1), (1, 3))] == [
        "1-for-1", "even", "package", "package", "spread"]


def test_a_bad_trade_gets_better_ones_with_the_same_team_around_the_same_player():
    snap, c = league(), cfg(untouchable=["Untouchable Star"])
    e = evaluate_trade(snap, c, [1], [14])  # Hot Guard for Rival Bench: bad for you
    assert e.verdict == "bad" and e.core == ["Hot Guard"]
    alts = e.alternatives
    assert alts and len(alts) <= 5
    deals = {(tuple(names(a.give)), tuple(names(a.get))) for a in alts}
    assert (("Hot Guard",), ("Cold Wing",)) in deals  # the 1-for-1 that works
    assert (("Hot Guard",), ("Rival Bench",)) not in deals  # not the trade you asked about
    for a in alts:
        assert a.team_name == "Rival" and "Hot Guard" in names(a.give)
        assert "Untouchable Star" not in names(a.give)  # never packaged in unless you chose him
        assert a.gain >= c.trade_min_gain and a.their_face >= -c.trade_fairness
        assert len(a.give) <= 3 and len(a.get) <= 3 and a.reasons
    assert len({frozenset(names(a.get)) for a in alts}) == len(alts)  # no two ask for the same players
    assert [a.gain for a in alts] == sorted((a.gain for a in alts), reverse=True)


def test_alternatives_can_be_uneven():
    # Bolder bars let in uneven deals, so each shape gets a turn. Face value counts each player over
    # a free agent pickup, so a package of yours for one better player can work: whoever you add to
    # Hot Guard is worth little more than a waiver pickup.
    e = evaluate_trade(league(), cfg(trade_fairness=500, trade_min_gain=-500), [1], [14])
    shapes = {shape_of(len(a.give), len(a.get)) for a in e.alternatives}
    assert shapes >= {"1-for-1", "spread", "package"}


def test_says_none_when_no_alternative_works():
    e = evaluate_trade(league(), cfg(trade_min_gain=1000), [1], [14])
    assert e.alternatives == []
    assert "None. No other 1-for-1, package or spread with Rival adds 1000+ fpts/week" in render_evaluation(e)
    assert evaluate_trade(league(), cfg(), [1], [10], alternatives=False).alternatives == []


def test_picks_the_best_of_each_shape_then_the_rest_without_repeats():
    snap = league()
    p = {x.id: x for x in snap.all_players()}

    def alt(give, get, gain):
        return Trade(2, "Rival", [Valued(p[i], 30, 30) for i in give], [Valued(p[i], 30, p[i].fpts["season"]) for i in get],
                     gain, gain * 4, 0)

    trades = [
        alt([1], [10], 9),
        alt([1, 5], [10], 8.5),  # asks for the same players as the 9
        alt([1], [10, 12], 8),  # the best spread; Cold Wing leads
        alt([1], [13, 10], 7.5),  # Cold Wing would lead a third
        alt([1], [12], 7),
        alt([1, 2], [11], 5),  # the best package with new players
        alt([1, 5], [13], 4),
        alt([1, 2], [12], 3.9),  # same players as the 7
        alt([1], [14], 1),  # the list is full
    ]
    picked = _pick_alternatives(trades)
    assert [t.gain for t in picked] == [9, 8, 7, 5, 4]
    assert shape_label(picked[0]) == "1-for-1" and shape_label(trades[1]) == "2-for-1 package"
    assert shape_label(trades[2]) == "1-for-2, more back"


def test_render_lists_the_alternatives():
    text = render_evaluation(evaluate_trade(league(), cfg(untouchable=["Untouchable Star"]), [1], [14]))
    assert "**Other trades with Rival** (built around Hot Guard)" in text
    assert "1-for-1: send **Hot Guard** for **Cold Wing**" in text
    print(text)


# ---- the long view ---------------------------------------------------------------------
def test_adding_a_player_never_makes_a_deal_look_worse_to_them():
    snap, c = league(), cfg()
    snap.my_roster[4].fpts = {k: 10 for k in snap.my_roster[4].fpts}  # Bench Guy, now worse than the 16/g free agent
    one_for_two = evaluate_trade(snap, c, [1], [10, 13], alternatives=False).trade  # they get an open spot
    two_for_two = evaluate_trade(snap, c, [1, 5], [10, 13], alternatives=False).trade  # Bench Guy too
    assert names(one_for_two.their_add) == ["Free Agent"]
    assert "Bench Guy" in names(two_for_two.their_drop) and names(two_for_two.their_add) == ["Free Agent"]
    assert two_for_two.their_gain >= one_for_two.their_gain - 1e-9


def test_free_agents_count_at_rest_of_season_value_on_both_sides():
    snap, c = league(), cfg()
    hot = tp(33, "Hot Streamer", G, 40, 35, 12, 12, owner="fa")  # a big week off waivers, 12/g on the season
    snap.free_agents += [steady(32, "Steady FA", G, 18, owner="fa"), hot]
    e = evaluate_trade(snap, c, [1], [10, 13], alternatives=False)  # they get an open spot
    assert names(e.trade.their_add) == ["Steady FA"]  # not the hot week: about 15/g over the rest of the season
    e = evaluate_trade(snap, c, [1, 2], [10], alternatives=False)  # you get an open spot
    assert names(e.trade.add) == ["Steady FA"]


def test_their_view_blends_recent_form_and_rest_of_season():
    def their_gain(recent):
        return evaluate_trade(league(), cfg(trade_their_recent=recent), [1], [10], alternatives=False).trade.their_gain
    # Hot Guard (hot now, lower rest of season) for Cold Wing (cold now, higher rest of season):
    # the more they go by recent form, the better it looks to them.
    assert their_gain(1.0) > their_gain(0.5) > their_gain(0.0)


def test_reads_their_recent_setting(tmp_path):
    toml = tmp_path / "bot.toml"
    toml.write_text("[league]\nleague_id = 1\nyear = 2027\nteam_id = 3\n[trade]\ntheir_recent = 0.8\n", encoding="utf-8")
    assert load_config(toml, tmp_path / "none.toml").trade_their_recent == 0.8
    toml.write_text("[league]\nleague_id = 1\nyear = 2027\nteam_id = 3\n", encoding="utf-8")
    assert load_config(toml, tmp_path / "none.toml").trade_their_recent == 0.5


# ---- building trades around players you pick ---------------------------------------------
from fantasy_bot.report import render_search  # noqa: E402
from fantasy_bot.trade import (  # noqa: E402
    ALT_MAX_TOTAL, SEARCH_PER_TEAM, SEARCH_SHOWN, _deals_around, _Market, find_included, search_trades,
)


def test_deals_around_a_player_include_him_and_stay_small():
    snap, c = league(), cfg(untouchable=["Untouchable Star"])
    m = _Market(snap, c)
    hot, cold = snap.my_roster[0], snap.other_rostered[0]
    deals = list(_deals_around(m, 2, [hot], [cold]))
    assert deals
    for give, get in deals:
        assert hot in give and cold in get and len(give) + len(get) <= ALT_MAX_TOTAL
        assert "Untouchable Star" not in names_of(give)
    shapes = {(len(g), len(t)) for g, t in deals}
    assert {(1, 1), (1, 3), (3, 1), (2, 2)} <= shapes and (2, 3) not in shapes
    assert len(deals) == len({(frozenset(p.id for p in g), frozenset(p.id for p in t)) for g, t in deals})  # no repeats


def test_searches_every_team_around_your_player():
    snap, c = league(), cfg(untouchable=["Untouchable Star"])
    s = search_trades(snap, c, [1])  # Hot Guard
    assert s.core == ["Hot Guard"] and s.targets == [] and s.teams_searched == 2
    assert s.trades and len(s.trades) <= SEARCH_SHOWN
    for t in s.trades:
        assert "Hot Guard" in names(t.give) and t.reasons
        assert t.gain >= c.trade_min_gain and t.their_face >= -c.trade_fairness
    for team in {t.team_id for t in s.trades}:
        assert sum(t.team_id == team for t in s.trades) <= SEARCH_PER_TEAM
    assert any(names(t.get) == ["Cold Wing"] for t in s.trades)


def test_a_player_of_theirs_limits_the_search_to_his_team():
    s = search_trades(league(), cfg(untouchable=["Untouchable Star"]), [10])  # Cold Wing
    assert s.targets == ["Cold Wing"] and s.core == [] and s.teams_searched == 1
    assert s.trades and all("Cold Wing" in names(t.get) and t.team_name == "Rival" for t in s.trades)


def test_yours_and_theirs_together():
    s = search_trades(league(), cfg(trade_fairness=500), [1, 20])  # Hot Guard, Slumping Big (Other Team)
    assert s.teams_searched == 1 and s.trades
    for t in s.trades:
        assert "Hot Guard" in names(t.give) and "Slumping Big" in names(t.get) and t.team_name == "Other Team"


def test_search_says_what_is_wrong():
    with pytest.raises(TradeInputError, match="at least one"):
        search_trades(league(), cfg(), [])
    with pytest.raises(TradeInputError, match="free agent"):
        search_trades(league(), cfg(), [30])
    snap = league()
    assert find_included(snap, ["hot guard", "Cold Wing"]) == ([1, 10], [])
    with pytest.raises(TradeInputError, match="free agent"):
        find_included(snap, ["Free Agent"])


def test_render_search():
    c = cfg(untouchable=["Untouchable Star"])
    text = render_search(search_trades(league(), c, [1]))
    assert text.startswith("**Mine: trades sending Hot Guard** (rest of season, 27 days through Dec 5)")
    assert "Best across 2 teams" in text and "1-for-1 with **Rival**: send **Hot Guard** for **Cold Wing**" in text
    text = render_search(search_trades(league(), cfg(trade_min_gain=1000), [1, 10]))
    assert "**Mine: trades sending Hot Guard for Cold Wing**" in text
    assert "None. Across 1 team, no trade sending Hot Guard for Cold Wing adds 1000+ fpts/week" in text
    print(text)


# ---- free agents who can't help, and free agents only one side can have ----------------------
def test_a_free_agent_without_games_left_is_never_picked():
    snap, c = league(), cfg()
    unsigned = steady(34, "Unsigned Vet", G, 40, owner="fa")  # projected well, but no NBA team
    unsigned.games = {}
    snap.free_agents.append(unsigned)
    e = evaluate_trade(snap, c, [1, 2], [10], alternatives=False)  # you get an open spot
    assert names(e.trade.add) == ["Free Agent"]


def test_a_free_agent_goes_to_one_side_only():
    snap, c = league(), cfg()
    for p in (snap.my_roster[2], snap.my_roster[4]):  # Steady Center and Bench Guy, now 10/g scrubs
        p.fpts = {k: 10 for k in p.fpts}
    snap.free_agents.append(steady(32, "Steady FA", G, 18, owner="fa"))
    # Two scrubs for Cold Wing: you fill the open spot, and they cut both scrubs, the second for a free agent.
    m = _Market(snap, c)
    d = m.quick_one(2, [snap.my_roster[2], snap.my_roster[4]], [snap.other_rostered[0]])
    assert names_of(d.add) == ["Steady FA"] and names_of(d.their_add) == ["Free Agent"]
    assert set(names_of(d.their_drop)) == {"Steady Center", "Bench Guy"}
    # You'd swap both scrubs for those free agents without a trade: the pickup is listed, since you'd
    # make it, but marked as one you'd make anyway and not credited to the trade.
    e = evaluate_trade(snap, c, [3, 5], [10], alternatives=False)
    t = e.trade
    assert names(t.add) == ["Steady FA"] and t.anyway == {32} and t.new_add == [] and t.fa_gain == 0
    assert any(n.startswith("No trade needed: drop Steady Center") for n in e.notes)
    assert any("Steady FA (~18.0/g), a pickup you could make without the trade too" in r for r in t.reasons)
    assert "of your" not in render_evaluation(e)  # no share of your gain is put down to pickups


# ---- trades with one team ----------------------------------------------------------------------
from fantasy_bot.trade import find_team  # noqa: E402


def test_finds_the_best_trades_with_one_team():
    c = cfg(untouchable=["Untouchable Star"])
    s = search_trades(league(), c, team_id=2)
    assert s.partner == "Rival" and s.teams_searched == 1 and s.core == s.targets == []
    assert s.trades and len(s.trades) <= SEARCH_SHOWN
    for t in s.trades:
        assert t.team_name == "Rival" and t.gain >= c.trade_min_gain and t.their_face >= -c.trade_fairness
        assert "Untouchable Star" not in names(t.give)


def test_one_team_with_a_player():
    s = search_trades(league(), cfg(trade_fairness=500), [1], team_id=3)  # Hot Guard, Other Team
    assert s.partner == "Other Team" and s.trades
    assert all(t.team_name == "Other Team" and "Hot Guard" in names(t.give) for t in s.trades)
    with pytest.raises(TradeInputError, match="Cold Wing is on Rival, not Other Team"):
        search_trades(league(), cfg(), [10], team_id=3)
    with pytest.raises(TradeInputError, match="Pick a team"):
        search_trades(league(), cfg())


def test_finds_a_team_by_name():
    snap = league()
    assert find_team(snap, "rival") == 2 and find_team(snap, "  OTHER   team ") == 3 and find_team(snap, "other") == 3
    with pytest.raises(TradeInputError, match="could be"):
        find_team(snap, "r")  # Rival and Other Team
    with pytest.raises(TradeInputError, match="No other team"):
        find_team(snap, "Mine")  # your own


def test_render_trades_with_one_team():
    text = render_search(search_trades(league(), cfg(untouchable=["Untouchable Star"]), team_id=2))
    assert text.startswith("**Mine: trades with Rival**") and "Best with Rival" in text
    text = render_search(search_trades(league(), cfg(trade_min_gain=1000), [1], team_id=2))
    assert "**Mine: trades with Rival sending Hot Guard**" in text
    assert "None. No trade with Rival sending Hot Guard adds 1000+ fpts/week" in text


# ---- you never lose the trade ----------------------------------------------------------------
from fantasy_bot.report import bar_text, their_bar  # noqa: E402


def test_bar_wording():
    assert their_bar(0) == "is at least even for them at face value"
    assert their_bar(4) == "is no more than 4 fpts/week worse for them at face value"
    assert bar_text(3, 0) == ("adds 3+ fpts/week for you without losing you the trade at face value, "
                              "and is at least even for them at face value")
    assert bar_text(3, float("inf")) == "adds 3+ fpts/week for you without losing you the trade at face value"


def test_nothing_listed_loses_you_the_trade():
    for c in (cfg(untouchable=["Untouchable Star"]), cfg(trade_fairness=float("inf"), trade_min_gain=-1000)):
        listed = build_trades(league(), c).trades + search_trades(league(), c, [1]).trades
        listed += search_trades(league(), c, team_id=2).trades + evaluate_trade(league(), c, [1], [14]).alternatives
        assert listed and all(t.face >= 0 for t in listed)


def deep_bench():
    """Your roster with two more 21/g guards, so guards pile up and one sits most nights."""
    snap = league()
    snap.my_roster += [steady(6, "Spare Guard", G, 21, owner="me"), steady(7, "Other Spare", G, 21, owner="me")]
    return snap


def test_a_trade_that_helps_your_lineup_but_loses_on_talent_is_flagged():
    # Bench Guy (18/g) for Rival Bench (15/g): your lineup gets better, since guards pile up and he
    # fills an empty forward spot, but you give up the better player.
    e = evaluate_trade(deep_bench(), cfg(), [5], [14], alternatives=False)
    assert e.trade.gain >= 3 and e.trade.face < 0
    assert e.verdict == "feeding" and any(n.startswith("You lose this trade") for n in e.notes)
    assert "Careful, you lose this one" in render_evaluation(e)
    found = search_trades(deep_bench(), cfg(trade_fairness=float("inf")), [5], team_id=2).trades
    assert all(t.face >= 0 for t in found)
    assert not any(names(t.give) == ["Bench Guy"] and names(t.get) == ["Rival Bench"] for t in found)


def test_not_asking_them_still_says_whether_they_would_accept():
    e = evaluate_trade(league(), cfg(trade_fairness=float("inf")), [1], [10], alternatives=False)
    assert e.accepts == (e.trade.their_face >= 0)


def test_team_records():
    snap = league()
    snap.team_records = {2: (2, 6, 0), 3: (5, 2, 1)}
    assert snap.record(2) == "2-6" and snap.record(3) == "5-2-1" and snap.record(1) == ""
    assert snap.win_pct(2) == 0.25 and snap.win_pct(3) == 5.5 / 8 and snap.win_pct(9) == 0.5


# ---- face value next to the lineup number ----------------------------------------------------
def test_face_value_is_the_players_alone():
    snap, c = league(), cfg()
    m = _Market(snap, c)
    hot, cold = snap.my_roster[0], snap.other_rostered[0]
    t = evaluate_trade(snap, c, [1], [10], alternatives=False).trade
    assert m.replacement == 16  # the only healthy free agent
    above = lambda p: m.above(m.ros_lineups, p)
    assert above(hot) == pytest.approx(m.season(hot) * (m.ros(hot) - 16) / m.ros(hot))
    assert t.face == pytest.approx((above(cold) - above(hot)) / m.weeks)
    assert t.fa_gain == 0 and t.their_fa_gain == 0
    # Everyone starts on a 5-man roster with 6 spots, and both play the same games, so the two agree.
    assert t.gain == pytest.approx(t.face)


def test_free_agent_pickups_are_split_out():
    snap, c = league(), cfg()
    m = _Market(snap, c)
    # Hot Guard and Steady Forward for Cold Wing: your open spot goes to a free agent.
    t = evaluate_trade(snap, c, [1, 2], [10], alternatives=False).trade
    assert names(t.add) == ["Free Agent"] and t.fa_gain > 0
    hot, fwd, cold = snap.my_roster[0], snap.my_roster[1], snap.other_rostered[0]
    above = lambda p: m.above(m.ros_lineups, p)
    assert t.face == pytest.approx((above(cold) - above(hot) - above(fwd)) / m.weeks)
    raw = (m.season(cold) - m.season(hot) - m.season(fwd)) / m.weeks
    assert t.gain == pytest.approx(raw + t.fa_gain)  # everyone starts, so the gap is the pickup


def test_reports_show_face_value():
    snap, c = league(), cfg()
    text = render_evaluation(evaluate_trade(snap, c, [1, 2], [10], alternatives=False))
    assert "**Face value vs with roster moves**" in text and "Free agent pickups make up +" in text
    text = render_search(search_trades(league(), cfg(untouchable=["Untouchable Star"]), team_id=2))
    assert "Face value (the players over a free agent pickup):" in text
    assert "Face value (the players over a free agent pickup):" in render_trades(
        build_trades(league(), cfg(untouchable=["Untouchable Star"])))


def test_the_bar_for_them_is_face_value_not_their_lineup():
    snap, c = league(), cfg()
    snap.free_agents.append(steady(35, "Great FA", F, 30, owner="fa"))
    # Hot Guard for Cold Wing and Rival Guard: on their lineup it's a gain, only because their open
    # spot goes to a 30/g free agent. At face value they give up more than they get.
    e = evaluate_trade(snap, c, [1], [10, 13], alternatives=False)
    t = e.trade
    assert names(t.their_add) == ["Great FA"] and t.their_gain > 0 > t.their_face
    assert not e.accepts and "They'd likely say no" in render_evaluation(e)
    listed = search_trades(snap, cfg(trade_min_gain=-1000), [1], team_id=2).trades
    assert not any(set(names(x.get)) == {"Cold Wing", "Rival Guard"} and names(x.give) == ["Hot Guard"] for x in listed)


# ---- a trade is measured after the free agent moves you'd make anyway ----------------------------
def test_a_free_agent_you_could_add_anyway_is_no_part_of_any_trade():
    # A 30/g free agent beats Bench Guy (18) by far: pick him up, no trade needed. Before, every trade
    # that let you cut someone for him got credit for him, and the free agent topped every list.
    snap, c = league(), cfg(untouchable=["Untouchable Star"])
    snap.free_agents.append(steady(35, "Great FA", F, 30, owner="fa"))
    m = _Market(snap, c)
    assert names_of(m.base_drop) == ["Bench Guy"] and names_of(m.base_add) == ["Great FA"]
    r = build_trades(snap, c)
    assert r.before.startswith("No trade needed: drop Bench Guy (~18.0/g) for Great FA (~30.0/g)")
    assert r.before in render_trades(r)
    assert r.trades and all(t.fa_gain == 0 and "Great FA" not in names(t.new_add) for t in r.trades)
    # Hot Guard for Cold Wing, against your roster with Great FA already in for Bench Guy.
    t = evaluate_trade(snap, c, [1], [10], alternatives=False).trade
    hot, cold = snap.my_roster[0], snap.other_rostered[0]
    after = [p for p in m.base_me if p.id != hot.id] + [cold]
    assert t.gain == pytest.approx((m.ros_lineups.total(after) - m.my_before) / m.weeks)
    # Both halves of the move are listed, the pickup marked as one you'd make anyway.
    assert names(t.drop) == ["Bench Guy"] and names(t.add) == ["Great FA"]
    assert t.new_add == [] and t.fa_gain == 0


def test_without_a_better_free_agent_there_is_no_move_before_trading():
    r = build_trades(league(), cfg(untouchable=["Untouchable Star"]))
    assert r.before is None and "No trade needed" not in render_trades(r)


# ---- nobody gives a starter for players off waivers -------------------------------------------
def waiver_pickups():
    """Your roster with two players just picked up off waivers on a hot week, about 20/g on the
    season, next to free agents just as good."""
    snap = league()
    snap.my_roster += [tp(6, "Hot Pickup", G, 30, 26, 20, 18, owner="me", acquisition_type="ADD"),
                       tp(7, "Other Pickup", F, 30, 26, 20, 18, owner="me", acquisition_type="ADD")]
    snap.free_agents += [steady(36, "Waiver Guard", G, 21, owner="fa"), steady(37, "Waiver Wing", F, 20, owner="fa")]
    return snap


def test_two_waiver_pickups_dont_buy_a_starter():
    snap, c = waiver_pickups(), cfg(untouchable=["Untouchable Star"], trades_shown=50)
    m = _Market(snap, c)
    hot, other, guard = snap.my_roster[5], snap.my_roster[6], snap.other_rostered[3]  # Rival Guard, 26/g
    assert m.replacement == 16  # third best free agent: 21, 20, 16
    # On a hot week, either one alone looks about as good as Rival Guard, and the two far better...
    assert m.now(hot) + m.now(other) > m.ros(guard) * 1.9
    # ...but a manager who passed on them on waivers values them on rest of season, and only
    # for what they score over a free agent.
    assert m.their(hot) == m.ros(hot) and m.their(guard) == pytest.approx(26)
    e = evaluate_trade(snap, c, [6, 7], [13], alternatives=False)
    assert e.trade.their_face < 0 and not e.accepts
    for t in build_trades(snap, c).trades + search_trades(snap, c, [6, 7]).trades:
        assert t.their_face >= 0
        assert not (set(names(t.give)) == {"Hot Pickup", "Other Pickup"} and "Rival Guard" in names(t.get))


def test_their_view_keeps_recent_form_for_players_not_off_waivers():
    snap, c = league(), cfg()
    m = _Market(snap, c)
    hot = snap.my_roster[0]  # drafted, hot start
    assert m.their(hot) == pytest.approx(0.5 * m.now(hot) + 0.5 * m.ros(hot))
    hot.acquisition_type = "ADD"
    assert _Market(snap, c).their(hot) == pytest.approx(m.ros(hot))


# ---- out with no return date -------------------------------------------------------------------
from fantasy_bot.scoring import is_out_on  # noqa: E402


def test_out_with_no_return_date_is_assumed_back_over_the_season():
    snap, c = league(), cfg()
    star = snap.other_rostered[1]  # Their Star, 45/g
    star.injury_status = "OUT"
    back = DATES[TODAY] + timedelta(days=c.trade_unknown_return_days)
    games = sum(1 for sp in trade_days(snap) if sp in star.games and DATES[sp] >= back)
    assert games > 0
    assert _Market(snap, c).ros_lineups.solo(star) == pytest.approx(45 * games)
    # Over the daily scout's week he's out until there's a date.
    assert is_out_on(star, DATES[END])
    # A season-ending injury is out for good.
    star.injury_note = "Underwent knee surgery and will miss the rest of the season."
    assert _Market(snap, c).ros_lineups.solo(star) == 0
    # A return date wins over the assumption either way.
    star.injury_note, star.expected_return = "", DATES[END - 2]
    assert _Market(snap, c).ros_lineups.solo(star) == 45 * sum(1 for sp in trade_days(snap)
                                                                if sp in star.games and sp >= END - 2)


def test_reads_unknown_return_days(tmp_path):
    toml = tmp_path / "bot.toml"
    toml.write_text("[league]\nleague_id = 1\nyear = 2027\nteam_id = 3\n[trade]\nunknown_return_days = 21\n",
                    encoding="utf-8")
    assert load_config(toml, tmp_path / "none.toml").trade_unknown_return_days == 21
    toml.write_text("[league]\nleague_id = 1\nyear = 2027\nteam_id = 3\n", encoding="utf-8")
    assert load_config(toml, tmp_path / "none.toml").trade_unknown_return_days == 14


# ---- day-to-day is a few days, not the rest of the season ------------------------------------------
from fantasy_bot.scoring import availability_on  # noqa: E402
from fantasy_bot.trade import DTD_DAYS  # noqa: E402


def test_day_to_day_is_discounted_only_until_hes_back():
    snap, c = league(), cfg()
    fwd = snap.my_roster[1]  # Steady Forward, 30/g
    fwd.injury_status = "QUESTIONABLE"
    assert fwd.is_risky
    # The daily scout discounts him every day, as before.
    assert availability_on(fwd, DATES[END], c) == c.dtd_availability
    # Over the rest of the season: discounted for DTD_DAYS with no date...
    healthy = DATES[TODAY] + timedelta(days=DTD_DAYS)
    full = sum(30 for sp in trade_days(snap) if sp in fwd.games)
    hurt = sum(30 * (1 - c.dtd_availability) for sp in trade_days(snap) if sp in fwd.games and DATES[sp] < healthy)
    assert _Market(snap, c).season(fwd) == pytest.approx(full - hurt)
    # ...and only until his return date when he has one.
    fwd.expected_return = DATES[TODAY + 1]
    assert _Market(snap, c).season(fwd) == pytest.approx(full)


def test_a_day_to_day_starter_is_never_cut_for_a_free_agent():
    # Steady Forward (30/g) questionable, back tomorrow, and a 25/g free agent. Discounted all season,
    # he looked like an 18/g player: you'd "drop him without a trade", and whoever got him would cut him.
    snap, c = league(), cfg(untouchable=["Untouchable Star"])
    fwd = snap.my_roster[1]
    fwd.injury_status, fwd.expected_return = "QUESTIONABLE", DATES[TODAY + 1]
    snap.free_agents.append(steady(38, "Good FA", F, 25, owner="fa"))
    m = _Market(snap, c)
    assert "Steady Forward" not in names_of(m.base_drop)
    t = evaluate_trade(snap, c, [2], [10], alternatives=False).trade  # Steady Forward for Cold Wing
    assert "Steady Forward" not in names(t.their_drop)
