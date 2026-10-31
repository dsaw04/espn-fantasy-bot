from datetime import date, datetime, timedelta

from espn_api.basketball.player import Player

from fantasy_bot.config import Config
from fantasy_bot.espn_adapter import EspnSource
from fantasy_bot.model import PlayerView, Snapshot
from fantasy_bot.recommend import build_report, chopping_block
from fantasy_bot.report import render
from fantasy_bot.scoring import Scorer

TODAY = 10
HORIZON = [10, 11, 12, 13]
DATES = {sp: date(2026, 10, 28) + timedelta(days=sp - TODAY) for sp in HORIZON}


def games(*sps):
    return {sp: datetime.combine(DATES[sp], datetime.min.time()) for sp in sps}


def pv(pid, name, team, team_id, slots, fpts, mpg, sched, **kw):
    return PlayerView(
        id=pid, name=name, pro_team=team, pro_team_id=team_id,
        eligible_slots=slots, fpts={"season": fpts, "last_7": fpts, "projected": fpts},
        mpg={"season": mpg, "last_7": kw.pop("mpg7", mpg)}, games=games(*sched), **kw,
    )


def cfg(**kw):
    base = dict(league_id=1, year=2027, team_id=1, chopping_block=["Late Pick A", "Late Pick B"],
                auto_chop_pickups=True, horizon_days=4, min_gain=5.0, max_moves=2)
    base.update(kw)
    return Config(**base)


def scenario():
    C = ["C", "F/C", "PF/C", "UT", "BE", "IR"]
    G = ["PG", "SG", "G", "UT", "BE", "IR"]
    my = [
        pv(1, "Star Guard", "BOS", 2, G, 45, 34, [11, 12], lineup_slot="PG", acquisition_type="DRAFT"),
        pv(2, "Bench Guard", "MIA", 14, G, 25, 28, [10, 11, 12], lineup_slot="BE", acquisition_type="DRAFT"),
        pv(3, "Late Pick A", "DET", 8, G, 14, 20, [10, 12], lineup_slot="SG", acquisition_type="DRAFT"),
        pv(4, "Late Pick B", "UTA", 26, C, 18, 22, [10, 11, 12, 13], lineup_slot="BE",
           injury_status="OUT", expected_return=date(2026, 11, 10), acquisition_type="DRAFT"),
        pv(5, "Old Stream", "POR", 22, G, 16, 22, [11], lineup_slot="BE", acquisition_type="ADD"),
    ]
    others = [
        pv(20, "Lakers Starting C", "LAL", 13, C, 44, 33, [10, 11, 12, 13], owner="other",
           injury_status="OUT", expected_return=date(2026, 11, 6)),
        pv(21, "Lakers Starting PF", "LAL", 13, ["PF", "F", "PF/C", "UT", "BE"], 30, 32, [10, 11, 12, 13], owner="other"),
    ]
    fas = [
        pv(30, "Lakers Backup C", "LAL", 13, C, 15, 15, [10, 11, 12, 13], mpg7=24, percent_owned=6),
        pv(31, "Steady Vet", "SAS", 24, G, 21, 26, [12], percent_owned=30),
        pv(32, "Hurt FA", "NYK", 18, G, 40, 30, [10, 11, 12, 13], injury_status="OUT"),
        pv(33, "Lakers Third C", "LAL", 13, C, 6, 6, [10, 11, 12, 13]),
    ]
    return Snapshot(TODAY, HORIZON, "Dhruv's Team", my, others, fas, DATES)


def test_depth_boost_goes_to_backup_not_unrelated_players():
    snap = scenario()
    s = Scorer(snap, cfg())
    backup = s.value(next(p for p in snap.free_agents if p.id == 30))
    vet = s.value(next(p for p in snap.free_agents if p.id == 31))
    assert backup.openings and backup.openings[0].injured.name == "Lakers Starting C"
    assert not vet.openings
    assert backup.expected > backup.base * backup.games  # boosted


def test_injured_player_contributes_nothing_until_return():
    snap = scenario()
    v = Scorer(snap, cfg()).value(snap.my_roster[3])
    assert v.games == 0 and v.expected == 0


def test_chopping_block_includes_named_and_pickups_but_respects_untouchable():
    names = {p.name for p in chopping_block(scenario(), cfg())}
    assert names == {"Late Pick A", "Late Pick B", "Old Stream"}
    names = {p.name for p in chopping_block(scenario(), cfg(untouchable=["Old Stream"]))}
    assert "Old Stream" not in names


def test_recommends_backup_for_injured_late_pick_and_skips_hurt_fa():
    r = build_report(scenario(), cfg())
    assert r.moves, "expected at least one move"
    first = r.moves[0]
    assert first.add.player.name == "Lakers Backup C"
    assert first.drop.player.name == "Late Pick B"
    assert all(m.add.player.name != "Hurt FA" for m in r.moves)
    assert any("Lakers Starting C" in reason for reason in first.reasons)


def test_min_gain_blocks_marginal_moves():
    r = build_report(scenario(), cfg(min_gain=1000))
    assert r.moves == []


def test_lineup_swap_benches_idle_starter_for_bench_player_with_game():
    r = build_report(scenario(), cfg())
    swap = next(s for s in r.swaps if s.bench.name == "Star Guard")
    assert swap.reason == "no game today"
    assert swap.start and swap.start.name == "Bench Guard"


def test_render_is_readable():
    text = render(build_report(scenario(), cfg()))
    assert "Add **Lakers Backup C**" in text
    assert "sit Star Guard" in text
    print(text)


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


def test_next_man_up_gets_bigger_bump_than_third_stringer():
    snap = scenario()
    s = Scorer(snap, cfg())
    backup = s.value(next(p for p in snap.free_agents if p.id == 30)).openings[0].boost
    third = s.value(next(p for p in snap.free_agents if p.id == 33)).openings[0].boost
    assert backup > 2 * third


def test_does_not_stack_two_pickups_on_one_injury():
    r = build_report(scenario(), cfg())
    lal = [m for m in r.moves if m.add.player.pro_team == "LAL"]
    assert len(lal) == 1


def test_snapshot_end_to_end_with_fake_league():
    """Drives EspnSource.snapshot() with a stand-in for the espn_api League object."""
    from types import SimpleNamespace

    def roster_entry(raw, slot_id, acq):
        return {"playerId": raw["id"], "lineupSlotId": slot_id, "acquisitionType": acq,
                "playerPoolEntry": {"player": raw["player"]}}

    starter = _raw_player(1, "Injured Starter", 40.0, 32.0, status="OUT")
    mine_raw = _raw_player(2, "My Late Pick", 4.0, 8.0)
    fa_raw = _raw_player(3, "Waiver Backup", 14.0, 16.0)

    ms = lambda d: int(datetime(2026, 10, 28 + d, 23).timestamp() * 1000)
    pro_schedule = {13: {str(10 + d): [{"date": ms(d), "homeProTeamId": 13, "awayProTeamId": 2}] for d in range(4)}}

    class FakeRequests:
        def get_player_card(self, ids, final, additional_filters=None):
            return {"players": [mine_raw]}

        def league_get(self, params=None, headers=None, extend=""):
            return {"players": [fa_raw]}

    my_team = SimpleNamespace(team_id=1, team_name="Mine",
                              roster=[Player(roster_entry(mine_raw, 4, "ADD"), 2027)])
    other = SimpleNamespace(team_id=2, team_name="Rival",
                            roster=[Player(roster_entry(starter, 4, "DRAFT"), 2027)])
    league = SimpleNamespace(teams=[my_team, other], scoringPeriodId=10, finalScoringPeriod=160,
                             pro_schedule=pro_schedule, espn_request=FakeRequests())

    src = object.__new__(EspnSource)
    src.cfg, src.league, src.points_league = cfg(chopping_block=[]), league, True
    src._games_by_team = src._build_game_index()
    snap = src.snapshot()

    assert snap.horizon == [10, 11, 12, 13]
    assert snap.my_roster[0].lineup_slot == "C" and snap.my_roster[0].acquisition_type == "ADD"
    assert snap.period_dates[10] == date(2026, 10, 28)  # 11pm UTC game stays on the US Eastern day

    report = build_report(snap, src.cfg)
    assert report.moves and report.moves[0].add.player.name == "Waiver Backup"
    assert report.moves[0].drop.player.name == "My Late Pick"
