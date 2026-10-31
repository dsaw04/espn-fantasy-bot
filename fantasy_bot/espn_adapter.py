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
from espn_api.basketball.constant import PRO_TEAM_MAP
from espn_api.basketball.player import Player

from .config import Config
from .model import PlayerView, Snapshot

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
        fpts = {k: v for k, s in splits.items() if (v := _split_fpts(s, self.points_league)) is not None}
        mpg = {k: v for k, s in splits.items() if (v := _split_minutes(s)) is not None}
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
            games=dict(self._games_by_team.get(team_id, {})),
            acquisition_type=p.acquisitionType or "",
            owner=owner,
            percent_owned=pct,
        )

    def _stat_filter(self) -> List[str]:
        y = self.cfg.year
        return [f"00{y}", f"10{y}", f"01{y}", f"02{y}"]

    def _my_roster(self, team) -> List[PlayerView]:
        """Roster entries carry lineup slots; player cards carry recent form. Merge them."""
        ids = [p.playerId for p in team.roster]
        card = self.league.espn_request.get_player_card(
            ids, self.league.finalScoringPeriod, additional_filters=[f"01{self.cfg.year}", f"02{self.cfg.year}"]
        )
        detailed = {}
        for entry in card.get("players", []):
            pl = Player(entry, self.cfg.year)
            detailed[pl.playerId] = pl
        views = []
        for p in team.roster:
            v = self._view(p, "me")
            d = detailed.get(p.playerId)
            if d:
                richer = self._view(d, "me")
                v.fpts.update(richer.fpts)
                v.mpg.update(richer.mpg)
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

        others = [self._view(p, "other") for t in lg.teams if t is not mine for p in t.roster]
        return Snapshot(
            today=today,
            horizon=horizon,
            my_team_name=mine.team_name,
            my_roster=self._my_roster(mine),
            other_rostered=others,
            free_agents=self._free_agents(),
            period_dates=self._period_dates(),
        )
