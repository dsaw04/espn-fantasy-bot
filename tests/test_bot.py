from datetime import date, datetime, timedelta

import pytest
from espn_api.basketball.player import Player

from fantasy_bot.config import Config
from fantasy_bot.espn_adapter import EspnSource
from fantasy_bot.model import GameLog, HashtagLine, PlayerView, Snapshot
from fantasy_bot.opportunity import needs_game_logs, opportunities
from fantasy_bot.recommend import build_report, droppable
from fantasy_bot.report import render
from fantasy_bot.scoring import Scorer

TODAY = 10
HORIZON = [10, 11, 12, 13]
DATES = {sp: date(2026, 10, 28) + timedelta(days=sp - TODAY) for sp in range(1, 15)}
PAST = [2, 4, 6, 8, 9]  # games already played this season


def games(*sps):
    return {sp: datetime.combine(DATES[sp], datetime.min.time()) for sp in sps}


def log(**games_by_sp):
    """log(g6=(30, 28)) -> {6: GameLog(fpts=30, minutes=28)}"""
    return {int(k[1:]): GameLog(*v) for k, v in games_by_sp.items()}


def pv(pid, name, team, team_id, slots, fpts, mpg, sched, **kw):
    return PlayerView(
        id=pid, name=name, pro_team=team, pro_team_id=team_id,
        eligible_slots=slots, fpts={"season": fpts, "last_7": fpts, "projected": fpts},
        mpg={"season": mpg, "last_7": kw.pop("mpg7", mpg)}, games=games(*sched), **kw,
    )


def cfg(**kw):
    base = dict(league_id=1, year=2027, team_id=1, horizon_days=4, min_edge=4.0, min_games_without=3, max_moves=2)
    base.update(kw)
    return Config(**base)


C = ["C", "F/C", "PF/C", "UT", "BE", "IR"]
G = ["PG", "SG", "G", "UT", "BE", "IR"]
F = ["SF", "PF", "F", "UT", "BE", "IR"]


def scenario():
    my = [
        pv(1, "Star Guard", "BOS", 2, G, 45, 34, [11, 12], lineup_slot="PG", acquisition_type="DRAFT"),
        pv(2, "Bench Guard", "MIA", 14, G, 25, 28, [10, 11, 12], lineup_slot="BE", acquisition_type="DRAFT"),
        pv(3, "Late Pick A", "DET", 8, G, 14, 20, [10, 12], lineup_slot="SG", acquisition_type="DRAFT"),
        pv(4, "Late Pick B", "UTA", 26, C, 18, 22, [10, 11, 12, 13], lineup_slot="BE",
           injury_status="OUT", expected_return=date(2026, 11, 10), acquisition_type="DRAFT"),
        pv(5, "Old Stream", "POR", 22, G, 16, 22, [11], lineup_slot="BE", acquisition_type="ADD"),
    ]
    others = [
        # Played the first two games, has missed the last three.
        pv(20, "Lakers Starting C", "LAL", 13, C, 44, 33, PAST + HORIZON, owner="other",
           injury_status="OUT", expected_return=date(2026, 11, 6), game_log=log(g2=(40, 33), g4=(46, 34))),
        pv(21, "Lakers Starting PF", "LAL", 13, ["PF", "F", "PF/C", "UT", "BE"], 30, 32, PAST + HORIZON, owner="other"),
        # Ruled out today after playing every game, so nobody has played without him yet.
        pv(22, "Heat Starting F", "MIA", 14, F, 40, 32, PAST + [10, 11, 12], owner="other", injury_status="OUT",
           game_log=log(g2=(40, 32), g4=(40, 32), g6=(40, 32), g8=(40, 32), g9=(40, 32))),
    ]
    fas = [
        pv(30, "Lakers Backup C", "LAL", 13, C, 15, 15, PAST + HORIZON, mpg7=24, percent_owned=6,
           game_log=log(g2=(12, 15), g4=(14, 16), g6=(30, 28), g8=(34, 30), g9=(32, 29))),
        pv(31, "Steady Vet", "SAS", 24, G, 21, 26, [12], percent_owned=30),
        pv(32, "Hurt FA", "NYK", 18, G, 40, 30, [10, 11, 12, 13], injury_status="OUT"),
        pv(33, "Lakers Third C", "LAL", 13, C, 6, 6, PAST + HORIZON,
           game_log=log(g6=(8, 10), g8=(6, 8), g9=(10, 12))),
        pv(34, "Heat Backup F", "MIA", 14, F, 14, 18, PAST + [10, 11, 12],
           game_log=log(g2=(14, 18), g4=(14, 18), g6=(14, 18), g8=(14, 18), g9=(14, 18))),
    ]
    return Snapshot(TODAY, HORIZON, "Dhruv's Team", my, others, fas, DATES)


def fa(snap, pid):
    return next(p for p in snap.free_agents if p.id == pid)


# ---- depth chart estimate --------------------------------------------------
def test_depth_boost_goes_to_backup_not_unrelated_players():
    snap = scenario()
    s = Scorer(snap, cfg())
    backup = s.value(fa(snap, 30))
    vet = s.value(fa(snap, 31))
    assert backup.openings and backup.openings[0].injured.name == "Lakers Starting C"
    assert not vet.openings
    assert backup.expected > backup.base * backup.games  # boosted


def test_next_man_up_gets_bigger_bump_than_third_stringer():
    snap = scenario()
    s = Scorer(snap, cfg())
    backup = s.value(fa(snap, 30)).openings[0].boost
    third = s.value(fa(snap, 33)).openings[0].boost
    assert backup > 2 * third


def test_injured_player_contributes_nothing_until_return():
    snap = scenario()
    v = Scorer(snap, cfg()).value(snap.my_roster[3])
    assert v.games == 0 and v.expected == 0


# ---- game log splits ---------------------------------------------------------
def test_splits_backup_games_with_and_without_injured_starter():
    snap = scenario()
    opp = next(o for o in opportunities(snap, cfg()) if o.player.name == "Lakers Backup C")
    assert opp.injured.name == "Lakers Starting C"
    assert opp.with_him.fpts == [12, 14]
    assert opp.without_him.fpts == [30, 34, 32]
    assert opp.without_him.mpg == 29


def test_starter_out_all_season_makes_every_game_a_without_game():
    snap = scenario()
    starter = next(p for p in snap.other_rostered if p.id == 20)
    starter.game_log, starter.fpts, starter.mpg = {}, {"projected": 44}, {"projected": 33}
    opp = next(o for o in opportunities(snap, cfg()) if o.player.name == "Lakers Backup C")
    assert opp.with_him.games == 0 and opp.without_him.fpts == [12, 14, 30, 34, 32]
    # His stats already reflect the bigger role, so the estimate must not add a bump on top.
    assert not Scorer(snap, cfg()).openings_for(fa(snap, 30))


def test_only_fetches_logs_for_injured_rotation_players_and_their_free_agent_teammates():
    names = {p.name for p in needs_game_logs(scenario(), cfg())}
    assert names == {"Lakers Starting C", "Lakers Backup C", "Lakers Third C", "Heat Starting F", "Heat Backup F"}


# ---- moves -------------------------------------------------------------------
# Roster per game: Late Pick A 14, Old Stream 16, Late Pick B 18 (out), Bench Guard 25, Star Guard 45.
# With two streaming spots, Late Pick A and Old Stream rotate; the rest are core.
def test_whole_roster_is_droppable_except_untouchables():
    names = {p.name for p in droppable(scenario(), cfg())}
    assert names == {"Star Guard", "Bench Guard", "Late Pick A", "Late Pick B", "Old Stream"}
    names = {p.name for p in droppable(scenario(), cfg(untouchable=["Star Guard", "old stream"]))}
    assert names == {"Bench Guard", "Late Pick A", "Late Pick B"}


def test_streaming_spots_are_the_weakest_players_per_game():
    assert build_report(scenario(), cfg()).streamers == {3, 5}
    assert build_report(scenario(), cfg(streaming_spots=1)).streamers == {3}
    # Untouchables are never streaming spots.
    assert build_report(scenario(), cfg(untouchable=["Late Pick A"])).streamers == {5, 4}


def test_streams_backup_into_the_spot_he_gains_most_over():
    r = build_report(scenario(), cfg())
    first = r.moves[0]
    assert first.add.player.name == "Lakers Backup C" and first.kind == "stream"
    # 4 games x 32 = 128. Old Stream plays once (16), Late Pick A twice (28), so Old Stream goes.
    assert first.drop.player.name == "Old Stream" and first.gain == 112
    assert first.per_game == 32  # his average without the injured starter, not his blended 15
    assert any("Without him: 30, 34, 32 fpts" in reason for reason in first.reasons)
    assert all(m.add.player.name != "Hurt FA" for m in r.moves)


def test_a_busier_schedule_alone_can_win_a_streaming_spot():
    r = build_report(scenario(), cfg())
    m = next(m for m in r.moves if m.add.player.name == "Heat Backup F")
    # Same 14 fpts/g as Late Pick A, but 3 games to his 2.
    assert m.kind == "stream" and m.drop.player.name == "Late Pick A" and m.gain == pytest.approx(14)


def test_stream_moves_clear_stream_min_gain():
    names = [m.add.player.name for m in build_report(scenario(), cfg(stream_min_gain=15)).moves]
    assert names == ["Lakers Backup C"]  # Heat Backup F's +14 is not enough
    for gain in (10.0, 50.0):
        assert all(m.gain >= gain for m in build_report(scenario(), cfg(stream_min_gain=gain, max_moves=5)).moves)


def test_injured_core_player_is_not_dropped_for_being_worth_zero_this_week():
    r = build_report(scenario(), cfg(max_moves=5))
    assert "Late Pick B" not in [m.drop.player.name for m in r.moves]


def test_core_upgrade_needs_a_per_game_edge():
    r = build_report(scenario(), cfg(streaming_spots=0))
    first = r.moves[0]
    assert first.kind == "core" and first.drop.player.name == "Late Pick A" and first.edge == 18
    r = build_report(scenario(), cfg(streaming_spots=0, min_edge=20))
    assert "Lakers Backup C" not in [m.add.player.name for m in r.moves]


def test_one_huge_night_is_not_a_consistent_core_upgrade():
    snap = scenario()
    fa(snap, 30).game_log.update(log(g6=(60, 30), g8=(10, 28), g9=(12, 29)))  # avg 27, median 12
    next(p for p in snap.my_roster if p.id == 3).game_log = log(g2=(14, 20), g4=(14, 20))  # median 14
    r = build_report(snap, cfg(streaming_spots=0))
    assert "Lakers Backup C" not in [m.add.player.name for m in r.moves]
    w = next(w for w in r.watchlist if w.player.name == "Lakers Backup C")
    assert w.median == 12 and w.games_without == 3


def test_needs_enough_games_without_the_starter_to_use_his_new_role():
    m = build_report(scenario(), cfg(min_games_without=4)).moves[0]
    assert m.add.player.name == "Lakers Backup C"
    assert m.opportunity is None and m.per_game == pytest.approx(15)  # falls back to his blended average


def test_holds_when_nobody_clears_either_bar():
    r = build_report(scenario(), cfg(min_edge=1000, stream_min_gain=1000))
    assert r.moves == []
    assert "Hold. No pickup over your streaming spots (Late Pick A and Old Stream) beats them by 1000+ fpts" in render(r)


def test_fresh_injury_shows_up_as_an_estimate_on_the_watch_list():
    r = build_report(scenario(), cfg(max_moves=1))
    w = next(w for w in r.watchlist if w.player.name == "Heat Backup F")
    assert w.injured.name == "Heat Starting F" and w.games_without == 0
    assert w.per_game == 34  # 14 base + half of the starter's 40
    assert "no games without him yet" in render(r)


def test_does_not_stack_two_pickups_on_one_injury():
    r = build_report(scenario(), cfg(min_edge=-100, stream_min_gain=-1000, max_moves=5))
    lal = [m for m in r.moves if m.add.player.pro_team == "LAL"]
    assert len(lal) == 1


def test_moves_use_next_period_roster_and_days():
    snap = scenario()
    pending = pv(6, "Pending Add", "SAS", 24, G, 50, 34, [11, 12, 13], acquisition_type="ADD")
    snap.next_roster = [p for p in snap.my_roster if p.name != "Old Stream"] + [pending]
    snap.move_horizon = [11, 12, 13]
    r = build_report(snap, cfg(max_moves=5))
    assert "Old Stream" not in {v.player.name for v in r.roster}  # already dropped today
    assert "Pending Add" in {v.player.name for v in r.roster}
    assert r.moves_start_tomorrow and r.horizon_days == 3
    backup = next(m for m in r.moves if m.add.player.name == "Lakers Backup C")
    assert backup.add.games == 3  # tonight's game doesn't count, he can't play it
    assert "(adds play from tomorrow)" in render(r)


def test_projection_counts_for_less_as_real_games_pile_up():
    c = cfg()
    p = pv(40, "Breakout", "UTA", 26, C, 32, 27, [10])
    p.fpts["projected"] = 16
    early = Scorer(scenario(), c).base(p)
    p.game_log = log(**{f"g{sp}": (32, 27) for sp in range(1, 11)})
    later = Scorer(scenario(), c).base(p)
    assert early < later < 32


def _hashtag(snap):
    def line(name, rank, **stats):
        return {"last_14": HashtagLine(name=name, team="", positions=[], rank=rank, games=5, mpg=20, stats=stats)}
    fa(snap, 30).hashtag = line("Lakers Backup C", 120, PTS=20, STL=2, TO=1)
    next(p for p in snap.my_roster if p.id == 5).hashtag = line("Old Stream", 210, PTS=10, AST=3)


def test_hashtag_lines_are_scored_with_the_league_points():
    snap = scenario()
    _hashtag(snap)
    snap.scoring = {"PTS": 1, "AST": 2, "STL": 4, "TO": -2}
    reasons = build_report(snap, cfg()).moves[0].reasons
    # 20 + 2x4 - 1x2 = 26, and 10 + 3x2 = 16
    assert "Hashtag stats in your scoring (last 14 days): Lakers Backup C 26.0 fpts/g, Old Stream 16.0" in reasons


def test_category_leagues_get_the_hashtag_rank():
    snap = scenario()
    _hashtag(snap)
    reasons = build_report(snap, cfg()).moves[0].reasons
    assert "Hashtag 9-cat rank (last 14 days): Lakers Backup C #120, Old Stream #210" in reasons


# ---- lineup and report -------------------------------------------------------
def test_lineup_swap_benches_idle_starter_for_bench_player_with_game():
    r = build_report(scenario(), cfg())
    swap = next(s for s in r.swaps if s.bench.name == "Star Guard")
    assert swap.reason == "no game today"
    assert swap.start and swap.start.name == "Bench Guard"


def test_render_is_readable():
    text = render(build_report(scenario(), cfg()))
    assert "Add **Lakers Backup C**" in text and "drop **Old Stream** (streaming spot). ~+112 fpts over the next 4 days" in text
    assert "Late Pick A (streaming spot): ~14.0 fpts/g" in text
    assert "sit Star Guard" in text
    assert "**Depth watch**" in text
    print(text)


# ---- ESPN adapter ------------------------------------------------------------
def _raw_player(pid, name, fpts_avg, minutes, status="ACTIVE"):
    def split(code, avg):
        return {"seasonId": 2027, "id": f"{code}2027", "scoringPeriodId": 0,
                "appliedTotal": avg * 5, "appliedAverage": avg,
                "stats": {"0": avg, "40": minutes * 5}, "averageStats": {"0": avg / 2, "40": minutes}}
    return {
        "id": pid,
        "player": {
            "id": pid, "fullName": name, "defaultPositionId": 5, "proTeamId": 13,
            "eligibleSlots": [4, 9, 10, 11, 12], "injuryStatus": status, "injured": status != "ACTIVE",
            "ownership": {"percentOwned": 7.5},
            "stats": [split("00", fpts_avg), split("01", fpts_avg + 6), split("10", fpts_avg - 2)],
        },
    }


def test_adapter_reads_espn_player_json():
    src = object.__new__(EspnSource)
    src.cfg = cfg()
    src.points_league = True
    src._games_by_team = {13: games(10, 11)}
    raw = _raw_player(99, "Raw Center", 20.0, 18.0)
    v = src._view(Player(raw, 2027), "fa", raw)
    assert v.pro_team == "LAL" and v.pro_team_id == 13
    assert v.positions == {"C"}
    assert v.fpts == {"season": 20.0, "last_7": 26.0, "projected": 18.0}
    assert v.mpg["season"] == 18.0
    assert v.percent_owned == 7.5 and set(v.games) == {10, 11}

    src.points_league = False  # category league falls back to standard points from averages
    v2 = src._view(Player(raw, 2027), "fa", raw)
    assert v2.fpts["season"] == 10.0  # PTS avg 10 x 1


def test_snapshot_end_to_end_with_fake_league():
    """Drives EspnSource.snapshot() with a stand-in for the espn_api League object."""
    from types import SimpleNamespace

    def roster_entry(raw, slot_id, acq):
        return {"playerId": raw["id"], "lineupSlotId": slot_id, "acquisitionType": acq,
                "playerPoolEntry": {"player": raw["player"]}}

    starter = _raw_player(1, "Injured Starter", 40.0, 32.0, status="OUT")
    mine_raw = _raw_player(2, "My Late Pick", 4.0, 8.0)
    fa_raw = _raw_player(3, "Waiver Backup", 14.0, 16.0)
    added_raw = _raw_player(4, "Added Today", 30.0, 30.0)  # shows up on my roster from tomorrow

    ms = lambda d: int(datetime(2026, 10, 28 + d, 23).timestamp() * 1000)
    pro_schedule = {13: {str(10 + d): [{"date": ms(d), "homeProTeamId": 13, "awayProTeamId": 2}] for d in range(4)}}

    class FakeRequests:
        def get_player_card(self, ids, final, additional_filters=None):
            return {"players": [r for r in (mine_raw, added_raw, starter, fa_raw) if r["id"] in ids]}

        def league_get(self, params=None, headers=None, extend=""):
            if "mSettings" in params.get("view"):
                items = [{"statId": 0, "points": 1.0}, {"statId": 2, "points": 4.0}, {"statId": 11, "points": -2.0}]
                return {"settings": {"scoringSettings": {"scoringItems": items}}}
            if params.get("view") == "mRoster":
                assert params["scoringPeriodId"] == 11
                entries = [roster_entry(mine_raw, 4, "ADD"), roster_entry(added_raw, 12, "ADD")]
                return {"teams": [{"id": 1, "roster": {"entries": entries}}]}
            # ESPN's free agent list already hides players added today
            return {"players": [fa_raw, mine_raw]}

    my_team = SimpleNamespace(team_id=1, team_name="Mine",
                              roster=[Player(roster_entry(mine_raw, 4, "ADD"), 2027)])
    other = SimpleNamespace(team_id=2, team_name="Rival",
                            roster=[Player(roster_entry(starter, 4, "DRAFT"), 2027)])
    league = SimpleNamespace(teams=[my_team, other], scoringPeriodId=10, finalScoringPeriod=160,
                             pro_schedule=pro_schedule, espn_request=FakeRequests())

    src = object.__new__(EspnSource)
    src.cfg, src.league, src.points_league = cfg(), league, True
    src._games_by_team = src._build_game_index()
    snap = src.snapshot()

    assert snap.horizon == [10, 11, 12, 13]
    assert snap.my_roster[0].lineup_slot == "C" and snap.my_roster[0].acquisition_type == "ADD"
    assert snap.period_dates[10] == date(2026, 10, 28)  # 11pm UTC game stays on the US Eastern day
    assert [p.name for p in snap.next_roster] == ["My Late Pick", "Added Today"]
    assert snap.move_horizon == [11, 12, 13, 14]
    assert [p.name for p in snap.free_agents] == ["Waiver Backup"]  # rostered players filtered out
    assert snap.scoring == {"PTS": 1.0, "STL": 4.0, "TO": -2.0}

    src.attach_game_logs(needs_game_logs(snap, src.cfg))
    assert snap.free_agents[0].game_log == {}  # fetched, no games in the fake card

    report = build_report(snap, src.cfg)
    assert report.moves and report.moves[0].add.player.name == "Waiver Backup"  # 14/g vs 4/g
    assert report.moves[0].drop.player.name == "My Late Pick"


def test_adapter_ignores_zero_splits_for_players_who_havent_played():
    src = object.__new__(EspnSource)
    src.cfg, src.points_league = cfg(), True
    src._games_by_team = {13: games(10, 11)}
    raw = _raw_player(98, "Not Played Yet", 0.0, 0.0, status="OUT")
    v = src._view(Player(raw, 2027), "me", raw)
    assert set(v.fpts) == {"projected"}  # no minutes in the season or recent splits


def test_adapter_reads_this_seasons_game_log():
    src = object.__new__(EspnSource)
    src.cfg, src.points_league = cfg(), True
    raw = _raw_player(97, "Logged", 20.0, 25.0)
    def game(season, sp, fpts, minutes, split=5, source=0):
        return {"seasonId": season, "id": "054018", "scoringPeriodId": sp, "statSourceId": source,
                "statSplitTypeId": split, "appliedTotal": fpts, "stats": {"0": fpts / 2, "40": minutes}}
    raw["player"]["stats"] += [
        game(2027, 2, 19.0, 18.0),
        game(2027, 4, 0.0, 0.0),  # did not play
        game(2026, 150, 40.0, 30.0),  # last season
        game(2027, 6, 33.0, 30.0, source=1),  # projection, not a real game
    ]
    assert src._game_log(raw) == {2: GameLog(19.0, 18.0)}
    src.points_league = False  # category league: standard points from the box score (PTS only here)
    assert src._game_log(raw) == {2: GameLog(9.5, 18.0)}


# ---- lineup slots ------------------------------------------------------------
def test_best_lineup_moves_a_flexible_player_to_fit_another():
    from fantasy_bot.lineup import best_lineup
    flex = pv(50, "Flex Guard", "BOS", 2, ["PG", "UT"], 30, 30, [10])
    ut_only = pv(51, "UT Only", "BOS", 2, ["UT"], 25, 30, [10])
    big = pv(52, "Big", "BOS", 2, ["C", "UT"], 20, 30, [10])
    # Flex Guard takes UT first, then slides to PG so UT Only can start. Big is left over.
    names = {p.name for p in best_lineup([(flex, 30), (ut_only, 25), (big, 20)], ["UT", "PG"])}
    assert names == {"Flex Guard", "UT Only"}
    # Two bigs can't both start at one C and one guard slot.
    other_big = pv(53, "Other Big", "BOS", 2, ["C"], 28, 30, [10])
    names = {p.name for p in best_lineup([(big, 20), (other_big, 28), (flex, 10)], ["C", "PG"])}
    assert names == {"Other Big", "Flex Guard"}


def test_pickup_only_counts_games_he_would_start():
    snap = scenario()
    snap.lineup_slots = {"UT": 2, "BE": 3}  # two starters a day
    r = build_report(snap, cfg(max_moves=5))
    backup = next(m for m in r.moves if m.add.player.name == "Lakers Backup C")
    # Unlimited slots made this +112. With two a day, Backup C mostly pushes Bench Guard to
    # the bench rather than adding his full value. Dropping either streaming spot gains the
    # same 64, and the tie goes to the weaker player per game.
    assert backup.gain == pytest.approx(64)
    assert backup.drop.player.name == "Late Pick A"
    assert backup.add_starts == 4 and backup.drop_starts == 1 and backup.drop_games == 2
    # Heat Backup F's extra game falls on days he wouldn't start, so he no longer clears the bar.
    assert "Heat Backup F" not in [m.add.player.name for m in r.moves]
    assert "starts 0 of his 1 game" in render(r)


def test_your_own_backup_keeps_his_role_value_after_you_add_him():
    snap = scenario()
    backup = fa(snap, 30)
    snap.free_agents.remove(backup)
    snap.next_roster = [p for p in snap.my_roster if p.name != "Old Stream"] + [backup]
    r = build_report(snap, cfg())
    assert r.per_game[backup.id] == 32  # not his blended 15, so he isn't churned straight back out
    assert backup.id not in r.streamers


# ---- matchups ----------------------------------------------------------------
from fantasy_bot.lineup import project
from fantasy_bot.matchup import Judge, adds_limit, calendar, win_prob
from fantasy_bot.model import MatchupState, Side


def test_calendar_merges_the_all_star_break_not_late_scheduled_december_weeks():
    start = date(2026, 12, 7)  # a Monday
    days = {sp: start + timedelta(days=sp - 1) for sp in range(1, 85)}  # 12 calendar weeks
    cup = {date(2026, 12, d) for d in range(8, 13)}  # NBA Cup week, not scheduled yet
    all_star = {date(2027, 2, d) for d in range(19, 25)}
    played = {sp: d for sp, d in days.items() if d not in cup and d not in all_star}
    weeks = {m: [m] for m in range(1, 10)}
    weeks[10] = [10, 11]  # a two week playoff round
    cal = calendar(1, 84, played, weeks)
    assert cal[1] == list(range(1, 8))  # December's gap didn't merge anything
    assert len(cal[10]) == 21  # Feb 8-14, then Feb 15-28 as one All-Star week
    assert days[cal[10][-1]] == date(2027, 2, 28)


def test_adds_limit_is_per_day_of_the_matchup():
    acq = {"matchupAcquisitionLimit": 1.0, "matchupLimitPerScoringPeriod": True}
    assert adds_limit(acq, 7) == 7 and adds_limit(acq, 14) == 14
    assert adds_limit({"matchupAcquisitionLimit": 5, "matchupLimitPerScoringPeriod": False}, 7) == 5
    assert adds_limit({"matchupAcquisitionLimit": -1}, 7) == -1


def test_win_prob():
    assert win_prob(0, 100) == 0.5
    assert win_prob(100, 100**2) == pytest.approx(0.841, abs=0.001)
    assert win_prob(100, 50**2) > win_prob(100, 100**2)  # less spread, safer lead


def rivals():
    opp = [pv(60 + i, f"Rival {i}", "BOS", 2, G, 30, 30, [10, 11, 12], owner="other") for i in range(3)]
    nxt = [pv(70 + i, f"Next {i}", "BOS", 2, G, 30, 30, [13, 14], owner="other") for i in range(3)]
    return opp, nxt


def with_matchup(snap, me=500.0, them=480.0, used=0, limit=7):
    opp, nxt = rivals()
    snap.other_rostered += opp + nxt
    snap.matchup = MatchupState(
        period=2, days=[6, 7, 8, 9, 10, 11, 12], me=Side("Mine", me, snap.my_roster),
        opp=Side("Rival", them, opp), next_days=[13, 14], next_opp=Side("Next Rival", 0.0, nxt),
        adds_limit=limit, adds_used=used,
    )
    return snap


def test_report_shows_both_matchups_and_win_chances():
    r = build_report(with_matchup(scenario()), cfg())
    assert 0 < r.outlook.now.win < 1 and 0 < r.outlook.next.win < 1
    text = render(r)
    assert "**This week vs Rival**: 500 to 480" in text
    assert "7 of 7 adds left" in text and "**Next week vs Next Rival**" in text
    m = next(m for m in r.moves if m.kind == "stream")
    assert m.win_gain > 0 and "this week" in render(r)


def test_a_decided_week_judges_pickups_on_next_week():
    r = build_report(with_matchup(scenario(), me=0.0, them=5000.0), cfg())
    assert r.outlook.now.win < 0.001
    assert "all but decided" in render(r)
    assert r.moves
    for m in r.moves:
        assert m.after.now.win - m.before.now.win == pytest.approx(0, abs=1e-6)
        assert m.win_gain == pytest.approx(m.after.next.win - m.before.next.win, abs=1e-6)


def test_no_moves_once_the_matchups_adds_are_used():
    r = build_report(with_matchup(scenario(), used=7), cfg())
    assert r.moves == []
    assert "Out of adds: all 7 used this matchup" in render(r)


def test_bar_drops_when_adds_would_go_unused_and_rises_when_scarce():
    snap = with_matchup(scenario(), used=3)  # 4 adds left, 2 days left after today
    judge = Judge(snap, cfg(), lambda p: 20.0)
    assert judge.bar() == pytest.approx(0.03 * 0.5)
    snap.matchup.adds_used = 6  # 1 left
    assert judge.bar() == pytest.approx(0.03 * 2)
    assert judge.moves_allowed() == 1


def test_games_already_started_are_left_out_of_the_projection():
    snap = scenario()
    bench_guard = next(p for p in snap.my_roster if p.name == "Bench Guard")  # plays 10, 11, 12
    week = project([(bench_guard, 25.0)], [10, 11], snap, cfg(), now=bench_guard.games[10])
    assert week.games == {bench_guard.id: 1} and week.total == 25.0


def test_adapter_reads_the_matchup_scores_opponents_and_adds():
    from types import SimpleNamespace
    src = object.__new__(EspnSource)
    src.cfg = cfg()
    src._games_by_team = {13: games(*range(1, 15))}  # Oct 19 (a Monday) to Nov 1
    src.league = SimpleNamespace(finalScoringPeriod=14, matchup_ids={1: ["1", "2"]})
    data = {
        "status": {"currentMatchupPeriod": 2, "firstScoringPeriod": 1, "finalScoringPeriod": 14},
        "settings": {
            "scheduleSettings": {"matchupPeriods": {"1": [1], "2": [2]}},
            "acquisitionSettings": {"matchupAcquisitionLimit": 1.0, "matchupLimitPerScoringPeriod": True},
        },
        "teams": [{"id": 1, "transactionCounter": {"matchupAcquisitionTotals": {"1": 2, "2": 4}}}],
        "schedule": [
            {"matchupPeriodId": 2, "home": {"teamId": 7, "totalPoints": 901.0, "totalPointsLive": 1188.0},
             "away": {"teamId": 1, "totalPoints": 681.0, "totalPointsLive": 946.0}},
            {"matchupPeriodId": 3, "home": {"teamId": 1, "totalPoints": 0.0}, "away": {"teamId": 6, "totalPoints": 0.0}},
        ],
    }
    mine = [pv(1, "Me", "LAL", 13, G, 30, 30, [8])]
    rosters = {7: [pv(2, "Them", "LAL", 13, G, 30, 30, [8])], 6: []}
    m = src._matchup(data, "Mine", mine, rosters, {7: "Fungus", 6: "Kyan"})
    assert m.days == list(range(8, 15))  # Monday Oct 26 to Sunday Nov 1
    assert (m.me.score, m.opp.score, m.opp.name) == (946.0, 1188.0, "Fungus")
    assert m.opp.roster[0].name == "Them" and m.next_opp.name == "Kyan"
    assert (m.adds_limit, m.adds_used, m.adds_left) == (7, 4, 3)


def test_waiver_player_counts_only_from_the_day_his_claim_clears():
    snap = scenario()
    backup = fa(snap, 30)  # Lakers Backup C, plays 10-13
    backup.available_from = DATES[12]
    week = project([(backup, 30.0)], [11, 12, 13], snap, cfg())
    assert week.games == {backup.id: 2}
    r = build_report(snap, cfg())
    m = next(m for m in r.moves if m.add.player.id == backup.id)
    assert any("on waivers" in x for x in m.reasons)


def test_report_lists_days_with_empty_lineup_spots():
    snap = scenario()
    snap.lineup_slots = {"PG": 1, "SG": 1, "C": 1, "UT": 2, "BE": 3}
    r = build_report(snap, cfg())
    assert r.open_spots and all(n > 0 for _, n in r.open_spots)
    assert "**Empty lineup spots**" in render(r)
