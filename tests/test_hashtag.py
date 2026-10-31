from datetime import date
from pathlib import Path

import lxml.html
import pytest
import requests

from fantasy_bot import hashtag
from fantasy_bot.__main__ import add_hashtag_rankings
from fantasy_bot.config import load_config
from fantasy_bot.model import HashtagLine, PlayerView, Snapshot
from test_bot import cfg

FIXTURE = (Path(__file__).parent / "fixtures" / "hashtag_rankings.html").read_text(encoding="utf8")


def test_parses_player_rows_and_skips_repeated_headers():
    lines = hashtag.parse_rankings(FIXTURE)
    assert [l.name for l in lines] == ["Nikola Jokic", "Stephen Curry", "Michael Porter Jr.", "Joshua Jefferson"]

    jokic = lines[0]
    assert (jokic.rank, jokic.team, jokic.positions, jokic.games, jokic.mpg) == (1, "DEN", ["C"], 3, 37.7)
    assert jokic.hashtag_id == 9196 and jokic.total == 15.35
    assert jokic.stats["FG%"] == 0.518 and (jokic.stats["FGM"], jokic.stats["FGA"]) == (9.3, 18.0)
    assert (jokic.stats["FTM"], jokic.stats["FTA"]) == (5.0, 5.3)
    assert jokic.stats["REB"] == 11.7 and jokic.stats["TO"] == 4.7
    assert set(jokic.zscores) == {"FG%", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TO"}
    assert jokic.zscores["TO"] < 0  # 4.7 turnovers a game hurts, so it reads negative


def test_team_abbreviations_follow_espn():
    curry = next(l for l in hashtag.parse_rankings(FIXTURE) if l.name == "Stephen Curry")
    assert curry.team == "GSW"


def test_rank_ignores_movement_arrow():
    last = hashtag.parse_rankings(FIXTURE)[-1]
    assert last.rank == 420 and last.positions == ["SF", "PF"]


def test_updated_date():
    assert hashtag.updated_on(lxml.html.fromstring(FIXTURE)) == date(2026, 10, 30)


def test_layout_change_raises():
    with pytest.raises(hashtag.HashtagError):
        hashtag.parse_rankings("<html><body><p>Down for maintenance</p></body></html>")


def test_form_posts_what_a_browser_would():
    data = hashtag.form_fields(lxml.html.fromstring(FIXTURE))
    assert data["__VIEWSTATE"] == "VIEWSTATE-TOKEN"
    assert data["ctl00$ContentPlaceHolder1$DDSHOW"] == "200"
    assert data["ctl00$ContentPlaceHolder1$CBFGP"] == "on"  # checked category boxes are sent
    assert "ctl00$ContentPlaceHolder1$CBFGM" not in data  # unchecked ones are not
    assert "ctl00$ContentPlaceHolder1$DDFGM" not in data  # nor disabled dropdowns
    assert not any("$GridView1$" in k for k in data)


class FakeSession:
    """Echoes the fixture back with the posted dropdown values selected."""

    def __init__(self, ignore=()):
        self.calls = []
        self.ignore = set(ignore)

    def _respond(self, selected):
        root = lxml.html.fromstring(FIXTURE)
        for name, value in selected.items():
            for opt in root.xpath(f'//select[@name="{name}"]/option'):
                if opt.get("value") == value:
                    opt.set("selected", "selected")
                else:
                    opt.attrib.pop("selected", None)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = lxml.html.tostring(root)
        resp.encoding = "utf8"
        return resp

    def get(self, url, **kw):
        self.calls.append(("GET", None))
        return self._respond({})

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", data))
        return self._respond({k: v for k, v in data.items() if k.split("$")[-1] not in self.ignore})


def test_fetch_picks_window_then_shows_all_players():
    s = FakeSession()
    r = hashtag.fetch_rankings(["last_7", "last_14"], session=s)
    assert set(r.windows) == {"last_7", "last_14"} and len(r.windows["last_7"]) == 4
    posts = [d for kind, d in s.calls if kind == "POST"]
    targets = [d["__EVENTTARGET"].split("$")[-1] for d in posts]
    assert targets == ["DDDURATION", "DDSHOW", "DDDURATION", "DDSHOW"]
    assert posts[0]["ctl00$ContentPlaceHolder1$DDDURATION"] == "7"
    assert posts[0]["ctl00$ContentPlaceHolder1$DDPOSFROM"] == "3"
    assert posts[1]["ctl00$ContentPlaceHolder1$DDSHOW"] == "900"
    assert posts[2]["ctl00$ContentPlaceHolder1$DDDURATION"] == "14"


def test_fetch_sets_the_table_and_multipliers_on_every_postback():
    s = FakeSession()
    hashtag.fetch_rankings(["season"], session=s)
    for post in [d for kind, d in s.calls if kind == "POST"]:
        field = {k.split("$")[-1]: v for k, v in post.items()}
        assert (field["DDRANK"], field["DDTYPE"]) == ("AVG", "H2H")  # per game averages, not totals
        assert (field["DDSTL"], field["DDBLK"], field["DDTO"], field["DDPTS"]) == ("1.25", "1.25", ".25", "1")

    s = FakeSession()
    hashtag.fetch_rankings(["season"], multipliers={"STL": 2, "TO": 0.5}, session=s)
    field = {k.split("$")[-1]: v for k, v in s.calls[1][1].items()}
    assert (field["DDSTL"], field["DDTO"], field["DDBLK"]) == ("2", ".5", "1")  # BLK left at the page's x1


def test_fetch_notices_when_the_page_ignores_a_multiplier():
    with pytest.raises(hashtag.HashtagError, match="DDSTL"):
        hashtag.fetch_rankings(["season"], session=FakeSession(ignore={"DDSTL"}))


def test_multipliers_must_be_ones_the_page_offers(tmp_path):
    with pytest.raises(ValueError, match="FGM"):
        hashtag.table_settings({"FGM": 1})  # unticked by default, so its dropdown is disabled
    with pytest.raises(ValueError, match="isn't offered"):
        hashtag.table_settings({"STL": 1.1})
    toml = tmp_path / "bot.toml"
    toml.write_text('[league]\nleague_id = 1\nyear = 2027\nteam_id = 1\n[hashtag]\nmultipliers = { STL = 4 }\n')
    with pytest.raises(SystemExit, match="STL multiplier 4"):
        load_config(toml, tmp_path / "none.toml")
    toml.write_text('[league]\nleague_id = 1\nyear = 2027\nteam_id = 1\n[hashtag]\nmultipliers = { STL = 1.5 }\n')
    assert load_config(toml, tmp_path / "none.toml").hashtag_multipliers["STL"] == 1.5


def test_fetch_notices_when_the_page_ignores_a_postback():
    with pytest.raises(hashtag.HashtagError, match="DDSHOW"):
        hashtag.fetch_rankings(["season"], session=FakeSession(ignore={"DDSHOW"}))


def test_name_key_handles_accents_suffixes_and_punctuation():
    k = hashtag.name_key
    assert k("Nikola Jokić") == k("Nikola Jokic") == "nikola jokic"
    assert k("Michael Porter Jr.") == k("Michael Porter")
    assert k("De'Aaron Fox") == k("DeAaron Fox")
    assert k("Shai Gilgeous-Alexander") == "shai gilgeous alexander"
    assert k("P.J. Washington") == k("PJ Washington")
    assert k("Alexandre Sarr") == k("Alex Sarr")  # via ALIASES


def _player(pid, name, team):
    return PlayerView(id=pid, name=name, pro_team=team, pro_team_id=pid, eligible_slots=["C"])


def _line(name, team, total):
    return HashtagLine(name=name, team=team, positions=["C"], rank=1, games=3, mpg=30.0, total=total)


def test_attach_matches_by_name_and_breaks_ties_by_team():
    jokic = _player(1, "Nikola Jokić", "DEN")
    a_lal = _player(2, "Same Name", "LAL")
    nobody = _player(3, "Two Way Guy", "BOS")
    rankings = hashtag.Rankings(
        updated=None,
        windows={
            "season": [_line("Nikola Jokic", "DEN", 10.0), _line("Same Name", "MIA", 1.0), _line("Same Name", "LAL", 2.0)],
            "last_7": [_line("Nikola Jokic", "DEN", 12.0)],
        },
    )
    assert hashtag.attach([jokic, a_lal, nobody], rankings) == 2
    assert jokic.hashtag["season"].total == 10.0 and jokic.hashtag["last_7"].total == 12.0
    assert a_lal.hashtag["season"].total == 2.0 and "last_7" not in a_lal.hashtag
    assert nobody.hashtag == {}


def test_rank_by_points_uses_the_leagues_scoring():
    lines = hashtag.parse_rankings(FIXTURE)
    hashtag.rank_by_points(hashtag.Rankings(updated=None, windows={"season": lines}), {"STL": 4, "BLK": 4})
    by_points = sorted(lines, key=lambda l: l.points_rank)
    assert [l.points_rank for l in by_points] == [1, 2, 3, 4]
    stocks = [4 * (l.stats["STL"] + l.stats["BLK"]) for l in by_points]
    assert stocks == sorted(stocks, reverse=True)


def test_write_csv_keeps_latest_and_dated_copy(tmp_path):
    rankings = hashtag.Rankings(updated=None, windows={"last_7": hashtag.parse_rankings(FIXTURE)})
    paths = hashtag.write_csv(rankings, tmp_path, day=date(2026, 10, 30))
    assert {p.relative_to(tmp_path).as_posix() for p in paths} == {
        "rankings_last_7.csv", "historical/rankings_last_7_2026-10-30.csv"}
    text = (tmp_path / "rankings_last_7.csv").read_text(encoding="utf8").splitlines()
    assert text[0].startswith("rank,name,team,positions") and text[1].startswith("1,Nikola Jokic,DEN,C")


def test_daily_run_survives_a_scrape_failure(monkeypatch, capsys):
    def boom(*a, **kw):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(hashtag, "fetch_rankings", boom)
    snap = Snapshot(10, [10], "Mine", [_player(1, "Nikola Jokic", "DEN")], [], [])
    add_hashtag_rankings(snap, cfg())
    assert "skipped Hashtag Basketball" in capsys.readouterr().err
    assert snap.my_roster[0].hashtag == {}
