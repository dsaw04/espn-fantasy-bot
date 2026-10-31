"""Pulls one day's snapshot out of ESPN through the espn-api package.

espn-api is read only, so the bot recommends moves and you make them in the
ESPN app. Nothing here can change your roster.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from espn_api.basketball import League
from espn_api.basketball.constant import POSITION_MAP, PRO_TEAM_MAP, STATS_MAP
from espn_api.basketball.player import Player

from .config import Config
from .model import GameLog, PlayerView, Snapshot

EASTERN = ZoneInfo("America/New_York")  # NBA game days are US Eastern days
ABBREV_TO_ID = {abbrev: tid for tid, abbrev in PRO_TEAM_MAP.items() if isinstance(tid, int)}

# Standard ESPN points scoring, used to rank players in category leagues
# where ESPN does not compute a fantasy point average.
DEFAULT_POINTS = {
    "PTS": 1, "3PM": 1, "FGA": -1, "FGM": 2, "FTA": -1, "FTM": 1,
    "REB": 1, "AST": 2, "STL": 4, "BLK": 4, "TO": -2,
}


def _split_fpts(split: dict, points_league: bool) -> Optional[float]:
    if not split:
        return None
    if points_league:
        return float(split.get("applied_avg") or 0.0)
    avg = split.get("avg") or {}
    if not avg:
        return None
    return float(sum(avg.get(k, 0) * w for k, w in DEFAULT_POINTS.items()))


def _split_minutes(split: dict) -> Optional[float]:
    avg = (split or {}).get("avg") or {}
    m = avg.get("MIN") or avg.get("MPG")
    return float(m) if m else None


def _games_played(split: dict) -> int:
    """Total minutes over minutes per game."""
    total = ((split or {}).get("total") or {}).get("MIN")
    avg = _split_minutes(split)
    return round(total / avg) if total and avg else 0


class EspnSource:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.league = League(
            league_id=cfg.league_id,
            year=cfg.year,
            espn_s2=cfg.espn_s2,
            swid=cfg.swid,
        )
        scoring = (self.league.settings.scoring_type or "").upper()
        self.points_league = "POINTS" in scoring
        self._games_by_team = self._build_game_index()

    # ---- schedule ---------------------------------------------------------
    def _build_game_index(self) -> Dict[int, Dict[int, datetime]]:
        idx: Dict[int, Dict[int, datetime]] = {}
        for team_id, periods in (self.league.pro_schedule or {}).items():
            for sp, games in periods.items():
                if not games:
                    continue
                ts = datetime.fromtimestamp(games[0]["date"] / 1000.0, tz=timezone.utc)
                idx.setdefault(int(team_id), {})[int(sp)] = ts.astimezone(EASTERN)
        return idx

    def _period_dates(self) -> Dict[int, object]:
        dates = {}
        for periods in self._games_by_team.values():
            for sp, dt in periods.items():
                dates.setdefault(sp, dt.date())
        return dates

    # ---- conversion -------------------------------------------------------
    def _view(self, p: Player, owner: str, raw: Optional[dict] = None) -> PlayerView:
        y = self.cfg.year
        splits = {
            "season": p.stats.get(f"{y}_total"),
            "last_7": p.stats.get(f"{y}_last_7"),
            "last_15": p.stats.get(f"{y}_last_15"),
            "projected": p.stats.get(f"{y}_projected"),
        }
        mpg = {k: v for k, s in splits.items() if (v := _split_minutes(s)) is not None}
        # ESPN fills played splits with zeros for players who haven't played yet.
        # A split with no minutes means no games, so it says nothing about value.
        fpts = {
            k: v
            for k, s in splits.items()
            if (k == "projected" or k in mpg) and (v := _split_fpts(s, self.points_league)) is not None
        }
        team_id = ABBREV_TO_ID.get(p.proTeam, 0)
        pct = 0.0
        if raw:
            inner = raw.get("player", raw)
            pct = float(inner.get("ownership", {}).get("percentOwned", 0.0))
        return PlayerView(
            id=p.playerId,
            name=p.name,
            pro_team=p.proTeam,
            pro_team_id=team_id,
            eligible_slots=list(p.eligibleSlots),
            lineup_slot=getattr(p, "lineupSlot", "") or "",
            injury_status=p.injuryStatus or "ACTIVE",
            expected_return=p.expected_return_date,
            fpts=fpts,
            mpg=mpg,
            games_played=_games_played(splits["season"]),
            games=dict(self._games_by_team.get(team_id, {})),
            acquisition_type=p.acquisitionType or "",
            owner=owner,
            percent_owned=pct,
        )

    def _league_settings(self):
        """Points per stat, keyed like espn-api's stat names, and lineup slot counts."""
        settings = self.league.espn_request.league_get(params={"view": "mSettings"}).get("settings", {})
        scoring: Dict[str, float] = {}
        for it in settings.get("scoringSettings", {}).get("scoringItems", []):
            name = STATS_MAP.get(str(it.get("statId")))
            if name and it.get("points"):
                scoring[name] = float(it["points"])
        slots: Dict[str, int] = {}
        for slot_id, n in settings.get("rosterSettings", {}).get("lineupSlotCounts", {}).items():
            name = POSITION_MAP.get(int(slot_id))
            if name and n:
                slots[name] = int(n)
        return scoring, slots

    def _stat_filter(self) -> List[str]:
        y = self.cfg.year
        return [f"00{y}", f"10{y}", f"01{y}", f"02{y}"]

    def _roster_on(self, scoring_period: int) -> Optional[List[Player]]:
        """My roster as ESPN has it for one scoring period. Adds and drops made
        today only show up from the next period on."""
        data = self.league.espn_request.league_get(
            params={"view": "mRoster", "forTeamId": self.cfg.team_id, "scoringPeriodId": scoring_period}
        )
        team = next((t for t in data.get("teams", []) if t.get("id") == self.cfg.team_id), None)
        if team is None:
            return None
        return [Player(e, self.cfg.year) for e in team.get("roster", {}).get("entries", [])]

    def _my_roster(self, players: List[Player]) -> List[PlayerView]:
        """Roster entries carry lineup slots; player cards carry recent form. Merge them."""
        ids = [p.playerId for p in players]
        card = self.league.espn_request.get_player_card(
            ids, self.league.finalScoringPeriod, additional_filters=[f"01{self.cfg.year}", f"02{self.cfg.year}"]
        )
        detailed = {}
        logs = {}
        for entry in card.get("players", []):
            pl = Player(entry, self.cfg.year)
            detailed[pl.playerId] = pl
            logs[pl.playerId] = self._game_log(entry)  # the card already carries every game
        views = []
        for p in players:
            v = self._view(p, "me")
            d = detailed.get(p.playerId)
            if d:
                richer = self._view(d, "me")
                v.fpts.update(richer.fpts)
                v.mpg.update(richer.mpg)
                v.games_played = richer.games_played or v.games_played
                v.game_log = logs[p.playerId]
            views.append(v)
        return views

    def _free_agents(self) -> List[PlayerView]:
        filters = {
            "players": {
                "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                "limit": self.cfg.free_agent_pool,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                "filterStatsForTopScoringPeriodIds": {
                    "value": 2,
                    "additionalValue": self._stat_filter(),
                },
            }
        }
        data = self.league.espn_request.league_get(
            params={"view": "kona_player_info", "scoringPeriodId": self.league.scoringPeriodId},
            headers={"x-fantasy-filter": json.dumps(filters)},
        )
        out = []
        for raw in data.get("players", []):
            try:
                out.append(self._view(Player(raw, self.cfg.year), "fa", raw))
            except (KeyError, TypeError):
                continue  # skip malformed entries rather than failing the whole run
        return out

    SINGLE_GAME = 5  # statSplitTypeId of a one-game stat line
    ACTUAL = 0  # statSourceId of real stats, as opposed to projections

    def _game_log(self, raw: dict) -> Dict[int, GameLog]:
        """This season's games from a player card, keyed by scoring period."""
        player = raw.get("player", raw)
        log: Dict[int, GameLog] = {}
        for s in player.get("stats", []):
            if (
                s.get("seasonId") != self.cfg.year
                or s.get("statSplitTypeId") != self.SINGLE_GAME
                or s.get("statSourceId") != self.ACTUAL
            ):
                continue
            stats = {STATS_MAP.get(k, k): v for k, v in (s.get("stats") or {}).items()}
            mins = float(stats.get("MIN") or 0)
            if mins <= 0:
                continue  # did not play
            if self.points_league:
                fpts = float(s.get("appliedTotal") or 0)
            else:
                fpts = float(sum(stats.get(k, 0) * w for k, w in DEFAULT_POINTS.items()))
            log[int(s["scoringPeriodId"])] = GameLog(fpts, mins)
        return log

    def attach_game_logs(self, players: List[PlayerView]) -> None:
        """Fills in game_log for these players in one request. Players that already
        have one, like your roster from its player card, are left alone."""
        players = [p for p in players if p.game_log is None]
        if not players:
            return
        card = self.league.espn_request.get_player_card(
            sorted({p.id for p in players}), self.league.finalScoringPeriod
        )
        logs = {
            raw.get("id", raw.get("player", {}).get("id")): self._game_log(raw)
            for raw in card.get("players", [])
        }
        for p in players:
            if p.id in logs:
                p.game_log = logs[p.id]

    # ---- public -----------------------------------------------------------
    def snapshot(self) -> Snapshot:
        lg = self.league
        mine = next((t for t in lg.teams if t.team_id == self.cfg.team_id), None)
        if mine is None:
            ids = ", ".join(f"{t.team_id}={t.team_name}" for t in lg.teams)
            raise SystemExit(f"team_id {self.cfg.team_id} not found. Teams: {ids}")

        today = lg.scoringPeriodId
        last = lg.finalScoringPeriod or today + self.cfg.horizon_days
        horizon = [sp for sp in range(today, today + self.cfg.horizon_days) if sp <= last]

        # Moves made now land next period, so judge them against that roster and
        # only count games from then on.
        upcoming = self._roster_on(today + 1) if today < last else None
        today_ids = {p.playerId for p in mine.roster}
        new = [p for p in upcoming or [] if p.playerId not in today_ids]
        views = {v.id: v for v in self._my_roster(list(mine.roster) + new)}
        my_roster = [views[p.playerId] for p in mine.roster]
        next_roster = [views[p.playerId] for p in upcoming] if upcoming is not None else None
        move_horizon = [sp for sp in range(today + 1, today + 1 + self.cfg.horizon_days) if sp <= last]

        others = [self._view(p, "other") for t in lg.teams if t is not mine for p in t.roster]
        # The free agent list already shows today's drops and hides today's adds,
        # so leave out anyone on either version of my roster.
        rostered = set(views) | {p.id for p in others}
        scoring, slots = self._league_settings()
        return Snapshot(
            today=today,
            horizon=horizon,
            my_team_name=mine.team_name,
            my_roster=my_roster,
            other_rostered=others,
            free_agents=[p for p in self._free_agents() if p.id not in rostered],
            period_dates=self._period_dates(),
            next_roster=next_roster,
            move_horizon=move_horizon if next_roster is not None else [],
            scoring=scoring if self.points_league else {},
            lineup_slots=slots,
        )
