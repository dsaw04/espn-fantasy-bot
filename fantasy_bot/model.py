"""Plain data types the recommendation logic works on.

Everything ESPN specific lives in espn_adapter.py. Keeping these types plain
means the scoring and recommendation code can be tested without network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Set, Tuple

BASE_POSITIONS = {"PG", "SG", "SF", "PF", "C"}
OUT_STATUSES = {"OUT", "SUSPENSION", "INJURY_RESERVE"}
RISKY_STATUSES = {"DAY_TO_DAY", "QUESTIONABLE", "DOUBTFUL", "GAME_TIME_DECISION"}
BENCH_SLOTS = {"BE", "IR"}


@dataclass
class HashtagLine:
    """One player's row in Hashtag Basketball's 9-cat rankings for one window."""

    name: str
    team: str  # ESPN abbreviation
    positions: List[str]
    rank: int
    games: int
    mpg: float
    # per game averages: FG%, FGM, FGA, FT%, FTM, FTA, 3PM, PTS, REB, AST, STL, BLK, TO
    stats: Dict[str, float] = field(default_factory=dict)
    # z-score per category, higher is better in every one (TO included)
    zscores: Dict[str, float] = field(default_factory=dict)
    # Hashtag's overall per game value: the z-scores summed with its default H2H weights (TO counts x0.25)
    total: float = 0.0
    hashtag_id: Optional[int] = None

    def fpts(self, scoring: Dict[str, float]) -> float:
        """Per game fantasy points under a league's points per stat."""
        return sum(self.stats.get(stat, 0.0) * pts for stat, pts in scoring.items())


@dataclass
class GameLog:
    """One game a player actually played in."""

    fpts: float
    minutes: float


@dataclass
class PlayerView:
    id: int
    name: str
    pro_team: str
    pro_team_id: int
    eligible_slots: List[str]
    lineup_slot: str = ""
    injury_status: str = "ACTIVE"
    expected_return: Optional[date] = None
    injury_note: str = ""  # latest line from ESPN's injury report
    # fantasy points per game by split: "season", "last_7", "last_15", "projected"
    fpts: Dict[str, float] = field(default_factory=dict)
    # minutes per game by split
    mpg: Dict[str, float] = field(default_factory=dict)
    games_played: int = 0  # this season
    # scoring period id -> game datetime for this player's pro team
    games: Dict[int, datetime] = field(default_factory=dict)
    acquisition_type: str = ""
    owner: str = "fa"  # "me", "other" or "fa"
    team_id: int = 0  # fantasy team that owns him, 0 for free agents
    # On waivers: the first day a claim put in now would have him on your roster.
    available_from: Optional[date] = None
    percent_owned: float = 0.0
    # Hashtag Basketball rankings by window: "season", "last_7", "last_14", "last_30"
    hashtag: Dict[str, HashtagLine] = field(default_factory=dict)
    # scoring period id -> stats, for games he played this season. None until fetched,
    # which only happens for injured rotation players and their free agent teammates.
    game_log: Optional[Dict[int, GameLog]] = None

    @property
    def positions(self) -> Set[str]:
        return {s for s in self.eligible_slots if s in BASE_POSITIONS}

    @property
    def is_out(self) -> bool:
        return (self.injury_status or "").upper() in OUT_STATUSES

    @property
    def is_risky(self) -> bool:
        return (self.injury_status or "").upper() in RISKY_STATUSES

    @property
    def is_starting_slot(self) -> bool:
        return bool(self.lineup_slot) and self.lineup_slot not in BENCH_SLOTS

    def status_label(self) -> str:
        s = (self.injury_status or "ACTIVE").upper()
        if s == "ACTIVE":
            return "healthy"
        label = s.replace("_", "-").lower()
        if self.expected_return:
            label += f" (back ~{self.expected_return:%b %d})"
        return label


@dataclass
class Side:
    """One team in a matchup."""

    name: str
    score: float  # points so far this matchup, live
    roster: List[PlayerView]


@dataclass
class MatchupState:
    """This week's matchup and the next one."""

    period: int  # matchup period id
    days: List[int]  # every scoring period in the matchup
    me: Side
    opp: Side
    next_days: List[int] = field(default_factory=list)
    next_opp: Optional[Side] = None  # None on a bye or when the schedule isn't set yet
    adds_limit: int = -1  # acquisitions allowed this matchup, -1 for no limit
    adds_used: int = 0
    # Last matchup's final: (opponent's name, your score, theirs). None in week 1 or after a bye.
    last: Optional[Tuple[str, float, float]] = None

    @property
    def adds_left(self) -> Optional[int]:
        return None if self.adds_limit < 0 else max(0, self.adds_limit - self.adds_used)


@dataclass
class Snapshot:
    """Everything the bot needs for one daily run."""

    today: int  # current scoring period id
    horizon: List[int]  # scoring periods to evaluate, today first
    my_team_name: str
    my_roster: List[PlayerView]
    other_rostered: List[PlayerView]
    free_agents: List[PlayerView]
    period_dates: Dict[int, date] = field(default_factory=dict)
    # My roster next scoring period, with today's adds and drops applied. None if unknown.
    next_roster: Optional[List[PlayerView]] = None
    # Scoring periods a move made now would play in. Empty means the same as horizon.
    move_horizon: List[int] = field(default_factory=list)
    # The league's points per stat ("PTS", "STL", ...). Empty in category leagues.
    scoring: Dict[str, float] = field(default_factory=dict)
    # Lineup slot counts from the league settings ({"PG": 1, "UT": 3, "BE": 3, ...}).
    # Empty means unknown, and every game a player has counts.
    lineup_slots: Dict[str, int] = field(default_factory=dict)
    # The matchup you're in. None when unknown, and moves are then judged on fpts alone.
    matchup: Optional[MatchupState] = None
    now: Optional[datetime] = None  # when the snapshot was taken; games that started before it are locked

    def roster_for_moves(self) -> List[PlayerView]:
        return self.my_roster if self.next_roster is None else self.next_roster

    def move_days(self) -> List[int]:
        return self.move_horizon or self.horizon

    def all_players(self) -> List[PlayerView]:
        seen: Set[int] = set()
        out = []
        for p in self.my_roster + (self.next_roster or []) + self.other_rostered + self.free_agents:
            if p.id not in seen:
                seen.add(p.id)
                out.append(p)
        return out


def n_games(n: int) -> str:
    return f"{n} game" if n == 1 else f"{n} games"
