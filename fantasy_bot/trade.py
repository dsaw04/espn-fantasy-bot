"""Trade ideas: sell players running hot, buy players off to a slow start.

Every player gets two per game values:

now             the daily scout's value (scoring.per_game_value). It leans on the last
                week or two, which is also what other managers go by.
rest of season  the same blend with the trade scout's weights, which lean on the season
                average and ESPN's projection, with the projection counting as
                trade_projection_games real games. A hot or cold couple of weeks is pulled
                back toward what he was expected to do.

Now above rest of season is a player to sell high; below it, one to buy low.

Every trade of up to trade_max_players a side with each other team is scored the way the
daily scout scores moves: by the best lineup each day, here over the rest of the season.
Your side uses rest of season values. Their side blends now and rest of season values
(trade_their_recent), since managers go by recent form but plan for the long run too. Free
agents count at rest of season value on both sides: a hot week off waivers makes a streamer,
not a long-term piece. A side that ends up with more players drops its weakest, one with
fewer picks up the best free agent, and either side cuts a player it just got when a free
agent is better. The top trades add the most to your fpts per week while
looking at least even to the other team, and where the best player in the deal on current
form goes to them: two good players for one great one looks even on paper, but managers
rarely take it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher, get_close_matches
from itertools import combinations, islice
from typing import Callable, Dict, FrozenSet, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .config import Config
from .lineup import assign, starting_slots
from .model import PlayerView, Snapshot
from .recommend import droppable
from .scoring import Scorer, availability_on, per_game_value

# Hashtag windows for the rest of season value: the whole season first.
ROS_HASHTAG = ("season", "last_30", "last_14", "last_7")
SIGNAL = 2.0  # fpts per game between now and rest of season that makes a sell high or buy low
SHORTLIST = 60  # trades per team scored with full lineups, picked on a quick estimate
SHORTLIST_SLACK = 15.0  # fpts per week the quick estimate of their side may be off by
LISTED = 5  # sell high and buy low candidates listed


@dataclass
class Valued:
    player: PlayerView
    now: float  # per game, recent form first
    ros: float  # per game, rest of season

    @property
    def gap(self) -> float:
        """Above 0 he's playing over his value: sell high. Below 0: buy low."""
        return self.now - self.ros


@dataclass
class Trade:
    team_id: int
    team_name: str
    give: List[Valued]
    get: List[Valued]
    gain: float  # fpts per week your best lineups add over the rest of the season, on rest of season values
    total: float  # the same over the whole rest of the season
    their_gain: float  # fpts per week their best lineups add, the way they'd value it (_Market.their)
    drop: List[Valued] = field(default_factory=list)  # who you'd drop to make room
    add: List[Valued] = field(default_factory=list)  # free agents you'd pick up with open spots
    their_drop: List[Valued] = field(default_factory=list)
    their_add: List[Valued] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


@dataclass
class TradeReport:
    team_name: str
    days: int  # days left in the season, from tomorrow
    through: Optional[date]
    trades: List[Trade]
    sell_high: List[Valued]  # your players furthest above their rest of season value
    buy_low: List[Tuple[Valued, str]]  # other teams' players furthest below it, with the team
    min_gain: float = 0.0
    fairness: float = 0.0


def trade_days(snap: Snapshot) -> List[int]:
    """From the first day a move made now would play, to the end of the season."""
    start = (snap.move_days() or [snap.today])[0]
    end = snap.season_end or max(snap.period_dates, default=start)
    return list(range(start, end + 1))


class Lineups:
    """Best lineup totals over a stretch of days for any roster, on one set of per game
    values, the same way lineup.project counts them. Each day's total is cached by who
    plays that day, so scoring a trade only costs the days its players have games."""

    def __init__(self, snap: Snapshot, cfg: Config, days: List[int], value: Callable[[PlayerView], float]):
        self.snap, self.cfg, self.days, self.value = snap, cfg, days, value
        self.slots = starting_slots(snap.lineup_slots)
        self._worth: Dict[int, Dict[int, float]] = {}
        self._player: Dict[int, PlayerView] = {}
        self._solo: Dict[int, float] = {}
        self._day: Dict[Tuple[int, FrozenSet[int]], float] = {}

    def worth(self, p: PlayerView) -> Dict[int, float]:
        """Scoring period -> what he's worth that day, for days he plays and can score."""
        if p.id not in self._worth:
            v = self.value(p)
            w = {}
            for sp in self.days:
                if sp in p.games:
                    x = availability_on(p, self.snap.period_dates.get(sp), self.cfg) * v
                    if x > 0:
                        w[sp] = x
            self._worth[p.id], self._player[p.id] = w, p
        return self._worth[p.id]

    def solo(self, p: PlayerView) -> float:
        """Every game he plays, as if he always started."""
        if p.id not in self._solo:
            self._solo[p.id] = sum(self.worth(p).values())
        return self._solo[p.id]

    def total(self, roster: Sequence[PlayerView]) -> float:
        by_day: Dict[int, List[int]] = {}
        for p in roster:
            for sp in self.worth(p):
                by_day.setdefault(sp, []).append(p.id)
        total = 0.0
        for sp, ids in by_day.items():
            key = (sp, frozenset(ids))
            if key not in self._day:
                playing = [(self._player[i], self._worth[i][sp]) for i in ids]
                if self.slots:
                    chosen = assign(playing, self.slots)
                    self._day[key] = sum(v for p, v in playing if p.id in chosen)
                else:
                    self._day[key] = sum(v for _, v in playing)
            total += self._day[key]
        return total


def _deals(mine: List[PlayerView], theirs: List[PlayerView], most: int) -> Iterator[Tuple[tuple, tuple]]:
    for n in range(1, most + 1):
        for give in combinations(mine, n):
            for k in range(1, most + 1):
                for get in combinations(theirs, k):
                    yield give, get


CUT_EDGE = 1.25  # a player just traded for is cut only for a free agent worth this much more over the season


def _swap(roster: List[PlayerView], out: Sequence[PlayerView], into: Sequence[PlayerView], keep: Set[int],
          value: Callable[[PlayerView], float], pool: Sequence[PlayerView],
          exclude: Iterable[int] = (), long: Optional[Callable[[PlayerView], float]] = None):
    """The roster after a trade, back to its size.

    - An extra player means dropping the lowest per game value, never anyone in keep. Per
      game, not this week's, so an injured regular isn't the one cut. A player just traded
      for can be the one cut, as any manager would cut him.
    - An open spot goes to the best free agent in pool (sorted best first), skipping those
      in exclude: the ones the other side of the trade already picked up.
    - Then a player just traded for is cut for the best free agent left, but only when the
      free agent is worth CUT_EDGE times as much over the rest of the season (long, by
      default value). A manager doesn't cut a player he just traded for, out for a few days
      or in a slump, for a waiver pickup that's about as good.

    Returns (roster, dropped, added)."""
    long = long or value
    taken = set(exclude)
    free = (p for p in pool if p.id not in taken)
    gone = {p.id for p in out}
    after = [p for p in roster if p.id not in gone] + list(into)
    drop: List[PlayerView] = []
    add: List[PlayerView] = []
    if len(after) > len(roster):
        drop = sorted((p for p in after if p.id not in keep), key=value)[: len(after) - len(roster)]
    elif len(after) < len(roster):
        add = list(islice(free, len(roster) - len(after)))
    cut = {p.id for p in drop}
    best = next(free, None)
    for p in sorted((p for p in into if p.id not in cut), key=long):
        if best is None or long(best) < CUT_EDGE * long(p):
            break
        drop.append(p)
        cut.add(p.id)
        add.append(best)
        best = next(free, None)
    return [p for p in after if p.id not in cut] + add, drop, add


def _ids(vs: Sequence[Valued]) -> Set[int]:
    return {v.player.id for v in vs}


def _distinct(t: Trade, chosen: List[Trade]) -> bool:
    """Each trade shown is its own idea: no player you'd get twice, and no two offers to the
    same team built on the same player of yours. Selling one player to different teams is fine."""
    for c in chosen:
        if _ids(t.get) & _ids(c.get):
            return False
        if c.team_id == t.team_id and _ids(t.give) & _ids(c.give):
            return False
    return True


@dataclass
class _Deal:
    """A trade after the quick pass, with both rosters already put back to size."""

    ours: float  # quick estimate of fpts per week for you, every game counted as a start
    give: tuple
    get: tuple
    mine: List[PlayerView]
    drop: List[PlayerView]
    add: List[PlayerView]
    theirs: List[PlayerView]
    their_drop: List[PlayerView]
    their_add: List[PlayerView]


class _Market:
    """What every trade is scored with: both values for each player, best lineups on each,
    your roster and the free agents either side would fill an open spot with."""

    def __init__(self, snap: Snapshot, cfg: Config):
        self.snap, self.cfg = snap, cfg
        self.scorer = Scorer(snap, cfg)
        self._ros: Dict[int, float] = {}
        self.days = trade_days(snap)
        self.weeks = max(len(self.days), 1) / 7
        self.ros_lineups = Lineups(snap, cfg, self.days, self.ros)  # how a trade really changes a team
        self.their_lineups = Lineups(snap, cfg, self.days, self.their)  # how it looks to the other team
        self.me = snap.roster_for_moves()
        self.tradeable = droppable(snap, cfg)
        self.safe = {p.id for p in self.me} - {p.id for p in self.tradeable}  # never cut to make room
        # Either side fills an open spot with the free agent who'd score the most over the rest
        # of the season: rest of season value, not a hot week, since he's a streamer, times the
        # games he has left. A free agent without an NBA team has none and is never picked.
        self.fa_ros = sorted((p for p in snap.free_agents if not p.is_out and self.season(p) > 0),
                             key=self.season, reverse=True)
        self.teams: Dict[int, List[PlayerView]] = {}
        for p in snap.other_rostered:
            self.teams.setdefault(p.team_id, []).append(p)
        self.my_before = self.ros_lineups.total(self.me)
        self.my_solo = sum(self.ros_lineups.solo(p) for p in self.me)
        self._their_before: Dict[int, float] = {}

    def now(self, p: PlayerView) -> float:
        return self.scorer.base(p)

    def ros(self, p: PlayerView) -> float:
        if p.id not in self._ros:
            self._ros[p.id] = per_game_value(p, self.cfg, self.snap.scoring, self.cfg.trade_weights,
                                             self.cfg.trade_projection_games, ROS_HASHTAG)
        return self._ros[p.id]

    def season(self, p: PlayerView) -> float:
        """Fpts over the rest of the season on rest of season value, every game he can play
        counted: how long-term worth is compared when a free agent might replace someone."""
        return self.ros_lineups.solo(p)

    def their(self, p: PlayerView) -> float:
        """How the other manager values a player: partly recent form, which is what managers
        go by, partly rest of season, since they plan for the long run too. Free agents at
        rest of season value, the same as on your side."""
        if p.owner == "fa":
            return self.ros(p)
        w = self.cfg.trade_their_recent
        return w * self.now(p) + (1 - w) * self.ros(p)

    def val(self, p: PlayerView) -> Valued:
        return Valued(p, self.now(p), self.ros(p))

    def team_name(self, tid: int) -> str:
        return self.snap.team_names.get(tid, f"team {tid}")

    def quick(self, tid: int, deals: Iterable[Tuple[tuple, tuple]]) -> List[_Deal]:
        """Every deal that helps you on a quick estimate, which counts every game as a start,
        and that may look fair enough to them. The full scoring is too slow for every deal."""
        their_solo = sum(self.their_lineups.solo(p) for p in self.teams[tid])
        out = []
        for give, get in deals:
            d = self.quick_one(tid, give, get)
            d.ours = (sum(self.ros_lineups.solo(p) for p in d.mine) - self.my_solo) / self.weeks
            theirs_quick = (sum(self.their_lineups.solo(p) for p in d.theirs) - their_solo) / self.weeks
            if d.ours > 0 and theirs_quick >= -self.cfg.trade_fairness - SHORTLIST_SLACK:
                out.append(d)
        return out

    def quick_one(self, tid: int, give: Sequence[PlayerView], get: Sequence[PlayerView]) -> _Deal:
        """One deal put back to size on both sides, ready to score, whatever the quick estimate."""
        mine, drop, add = _swap(self.me, give, get, self.safe, self.ros, self.fa_ros, long=self.season)
        # A free agent you'd pick up isn't there for them too.
        theirs, tdrop, tadd = _swap(self.teams[tid], get, give, set(), self.their, self.fa_ros,
                                    {p.id for p in add}, self.season)
        return _Deal(0.0, tuple(give), tuple(get), mine, drop, add, theirs, tdrop, tadd)

    def score(self, tid: int, d: _Deal) -> Trade:
        """A deal scored in full, with the best lineup each day."""
        if tid not in self._their_before:
            self._their_before[tid] = self.their_lineups.total(self.teams[tid])
        total = self.ros_lineups.total(d.mine) - self.my_before
        their_gain = (self.their_lineups.total(d.theirs) - self._their_before[tid]) / self.weeks
        v = self.val
        return Trade(
            tid, self.team_name(tid), [v(p) for p in d.give], [v(p) for p in d.get],
            total / self.weeks, total, their_gain, [v(p) for p in d.drop], [v(p) for p in d.add],
            [v(p) for p in d.their_drop], [v(p) for p in d.their_add],
        )

    def acceptable(self, t: Trade) -> bool:
        """Adds min_gain for you and looks fair enough to them."""
        return t.gain >= self.cfg.trade_min_gain and t.their_gain >= -self.cfg.trade_fairness


def build_trades(snap: Snapshot, cfg: Config) -> TradeReport:
    m = _Market(snap, cfg)
    ros, val, me, tradeable, days = m.ros, m.val, m.me, m.tradeable, m.days

    found: List[Trade] = []
    for tid, roster in m.teams.items():
        # Managers rarely give up the best player in a deal for lesser ones.
        deals = ((give, get) for give, get in _deals(tradeable, roster, cfg.trade_max_players)
                 if max(map(m.now, get)) <= max(map(m.now, give)))
        quick = sorted(m.quick(tid, deals), key=lambda d: d.ours, reverse=True)
        found += [t for t in (m.score(tid, d) for d in quick[:SHORTLIST]) if m.acceptable(t)]

    # Best for you first; on a tie, the one that looks better to them.
    found.sort(key=lambda t: (round(t.gain, 1), t.their_gain), reverse=True)
    trades: List[Trade] = []
    for t in found:
        if len(trades) >= cfg.trades_shown:
            break
        if _distinct(t, trades):
            t.reasons = _reasons(t, snap)
            trades.append(t)

    sell_high = sorted((v for v in map(val, tradeable) if v.gap >= SIGNAL), key=lambda v: v.gap, reverse=True)
    # Buy low targets worth having: they'd be in the top half of your roster on rest of season value.
    mine_sorted = sorted(ros(p) for p in me)
    bar = mine_sorted[len(mine_sorted) // 2] if mine_sorted else 0.0
    buy_low = sorted(
        ((v, snap.team_names.get(v.player.team_id, "")) for v in map(val, snap.other_rostered)
         if v.gap <= -SIGNAL and v.ros >= bar),
        key=lambda x: x[0].gap,
    )
    return TradeReport(
        team_name=snap.my_team_name,
        days=len(days),
        through=snap.period_dates.get(days[-1]) if days else None,
        trades=trades,
        sell_high=sell_high[:LISTED],
        buy_low=buy_low[:LISTED],
        min_gain=cfg.trade_min_gain,
        fairness=cfg.trade_fairness,
    )


# ---- reasons ---------------------------------------------------------------------

SPLITS = (("last_7", "last 7"), ("last_15", "last 15"), ("season", "season"), ("projected", "ESPN projected"))


def _pos(p: PlayerView) -> str:
    return "/".join(sorted(p.positions)) or "?"


def form(v: Valued) -> str:
    """His splits, then the two values the bot takes from them."""
    p = v.player
    splits = ", ".join(f"{label} {p.fpts[k]:.1f}" for k, label in SPLITS if k in p.fpts)
    return f"{splits}. ~{v.now:.1f} fpts/g now, ~{v.ros:.1f} rest of season"


def _hashtag(p: PlayerView, scoring: Dict[str, float]) -> Optional[str]:
    """Hashtag Basketball's season line as a second opinion: in your scoring in a points
    league, its 9-cat rank otherwise."""
    line = p.hashtag.get("season")
    if not line:
        return None
    if scoring:
        rank = f" (#{line.points_rank})" if line.points_rank else ""
        return f"Hashtag season {line.fpts(scoring):.1f} fpts/g{rank}"
    return f"Hashtag 9-cat #{line.rank}"


def _player_line(v: Valued, giving: bool, scoring: Dict[str, float]) -> str:
    p = v.player
    if giving:
        tag = "Sell high" if v.gap >= SIGNAL else "Selling low" if v.gap <= -SIGNAL else "Send"
    else:
        tag = "Buy low" if v.gap <= -SIGNAL else "Buying high" if v.gap >= SIGNAL else "Get"
    line = f"{tag}: {p.name} ({p.pro_team} {_pos(p)}): {form(v)}"
    if p.is_out or p.is_risky:
        line += f". {p.status_label()}"
        if p.injury_note:
            line += f": {p.injury_note.rstrip('.')}"
    if (ht := _hashtag(p, scoring)) is not None:
        line += f". {ht}"
    return line


def _names(vs: Sequence[Valued]) -> str:
    return " and ".join(f"{v.player.name} (~{v.ros:.1f}/g)" for v in vs)


def _reasons(t: Trade, snap: Snapshot) -> List[str]:
    r = [_player_line(v, True, snap.scoring) for v in t.give]
    r += [_player_line(v, False, snap.scoring) for v in t.get]
    if t.drop:
        r.append(f"you'd drop {_names(t.drop)}")
    if t.add:
        r.append(f"you'd pick up free agent {_names(t.add)}, valued over the rest of the season since he's a streamer")
    if t.their_drop:
        r.append(f"they'd drop {' and '.join(v.player.name for v in t.their_drop)}")
    if t.their_add:
        r.append(f"they'd pick up free agent {_names(t.their_add)}")
    m = snap.matchup
    if m is not None:
        ids = {v.player.id for v in t.get}
        if ids & {p.id for p in m.opp.roster}:
            r.append(f"{t.team_name} is your opponent this week: whoever you send plays against you until it ends")
        elif m.next_opp is not None and ids & {p.id for p in m.next_opp.roster}:
            r.append(f"{t.team_name} is your opponent next week")
    return r


# ---- evaluating a trade you name ---------------------------------------------------


class TradeInputError(ValueError):
    """A name that matches nobody, more than one player, or a player on the wrong side."""


@dataclass
class Evaluation:
    trade: Trade
    their_ros_gain: float  # fpts per week it really adds for them, on rest of season values
    days: int
    through: Optional[date]
    verdict: str  # "good", "even" or "bad", for you
    accepts: bool  # whether it looks no worse than the fairness bar to them
    notes: List[str] = field(default_factory=list)  # read-as corrections, untouchables, the best player
    min_gain: float = 0.0
    fairness: float = 0.0
    # Other trades with the same team built around the players you chose to send, best first.
    alternatives: List[Trade] = field(default_factory=list)
    core: List[str] = field(default_factory=list)  # the players you chose to send


def _tokens_match(query: str, name: str) -> bool:
    """'jokic' and 'n jok' match 'nikola jokic': every word of the query starts a word of the name."""
    words = name.split()
    return all(any(w.startswith(q) for w in words) for q in query.split())


def find_player(query: str, pool: Sequence[PlayerView]) -> Tuple[PlayerView, Optional[str]]:
    """The one player in pool a typed name means, and a note when it was read as someone
    spelled differently. Accents, case, punctuation and Jr./III don't matter, a last name
    alone works when it's unique, and a small typo is fixed when only one player is close."""
    from .hashtag import name_key

    key = name_key(query)
    if not key:
        raise TradeInputError("An empty player name.")
    keys = {p.id: name_key(p.name) for p in pool}
    exact = [p for p in pool if keys[p.id] == key]
    if not exact:
        exact = [p for p in pool if _tokens_match(key, keys[p.id])]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        shown = ", ".join(f"{p.name} ({p.pro_team})" for p in exact[:6])
        raise TradeInputError(f'"{query}" could be {shown}. Use more of the name.')
    close = get_close_matches(key, sorted(set(keys.values())), n=3, cutoff=0.75)
    if close:
        ratios = [SequenceMatcher(None, key, k).ratio() for k in close]
        clear = len(close) == 1 or ratios[0] - ratios[1] >= 0.1
        hits = [p for p in pool if keys[p.id] == close[0]]
        if clear and ratios[0] >= 0.85 and len(hits) == 1:
            return hits[0], f'Read "{query}" as {hits[0].name}.'
        names = " or ".join(p.name for k in close for p in pool if keys[p.id] == k)
        raise TradeInputError(f'No player named "{query}". Did you mean {names}?')
    raise TradeInputError(f'No player named "{query}" on any roster or in the free agent pool.')


def find_players(snap: Snapshot, give: Sequence[str], get: Sequence[str]) -> Tuple[List[int], List[int], List[str]]:
    """Typed names for each side to player ids, plus a note for each name read as a
    different spelling. Raises TradeInputError listing every problem, not just the first."""
    pool = snap.all_players()
    mine = {p.id for p in snap.roster_for_moves()}
    ids: Tuple[List[int], List[int]] = ([], [])
    notes: List[str] = []
    errors: List[str] = []
    for side, names in enumerate((give, get)):
        for name in names:
            try:
                p, note = find_player(name, pool)
            except TradeInputError as e:
                errors.append(str(e))
                continue
            if note:
                notes.append(note)
            if side == 0 and p.id not in mine:
                where = "a free agent" if p.owner == "fa" else f"on {snap.team_names.get(p.team_id, 'another team')}"
                errors.append(f"{p.name} is {where}, not on your team, so you can't send him.")
            elif side == 1 and p.id in mine:
                errors.append(f"{p.name} is already on your team.")
            elif side == 1 and p.owner == "fa":
                errors.append(f"{p.name} is a free agent: pick him up instead of trading for him.")
            elif p.id in ids[side]:
                errors.append(f"{p.name} is listed twice.")
            else:
                ids[side].append(p.id)
    if not errors and len({p.team_id for p in pool if p.id in ids[1]}) > 1:
        errors.append("Everyone you get has to come from the same team.")
    if errors:
        raise TradeInputError("\n".join(errors))
    return ids[0], ids[1], notes


def _verdict(gain: float, min_gain: float) -> str:
    bar = max(min_gain, 0.5)
    if gain >= bar:
        return "good"
    if gain <= -bar:
        return "bad"
    return "even"


def evaluate_trade(snap: Snapshot, cfg: Config, give_ids: Sequence[int], get_ids: Sequence[int],
                   notes: Sequence[str] = (), alternatives: bool = True) -> Evaluation:
    """Score a trade you name the way the trade scout scores the ones it finds: your best
    lineups over the rest of the season on rest of season values, theirs on a blend of now
    and rest of season (how it looks to them) and on rest of season values alone (what it
    really does for them). Any
    number of players a side: the side that ends up with more drops its weakest, the side
    with fewer picks up the best free agents. Then, unless alternatives is False, the best
    other trades with the same team built around the players you chose to send."""
    if not give_ids or not get_ids:
        raise TradeInputError("Name at least one player on each side.")
    me = snap.roster_for_moves()
    mine_ids = {p.id for p in me}
    by_id = {p.id: p for p in snap.all_players()}
    missing = [i for i in list(give_ids) + list(get_ids) if i not in by_id]
    if missing:
        raise TradeInputError("Some of those players aren't in the league any more. Refresh and try again.")
    give = [by_id[i] for i in give_ids]
    get = [by_id[i] for i in get_ids]
    if any(p.id not in mine_ids for p in give):
        raise TradeInputError("Everyone you send has to be on your team.")
    teams = {p.team_id for p in get}
    if any(p.id in mine_ids or p.owner == "fa" for p in get) or len(teams) != 1:
        raise TradeInputError("Everyone you get has to come from the same other team.")
    tid = teams.pop()

    m = _Market(snap, cfg)
    roster = m.teams[tid]
    t = m.score(tid, m.quick_one(tid, give, get))
    # What it really does for them: their side on rest of season values.
    theirs_ros, _, _ = _swap(roster, get, give, set(), m.ros, m.fa_ros, _ids(t.add), m.season)
    their_ros_gain = (m.ros_lineups.total(theirs_ros) - m.ros_lineups.total(roster)) / m.weeks
    t.reasons = _reasons(t, snap)

    out = list(notes)
    sent = [p.name for p in give if p.id in m.safe]  # untouchable: never cut to make room, but you chose to send him
    if sent:
        out.append(f"You've marked {' and '.join(sent)} untouchable.")
    if (best := _best_ask(t)) is not None:
        out.append(f"{best} is the best player in the deal on current form. Managers rarely give that up "
                   "for lesser players, even when the totals look even.")
    days = m.days
    return Evaluation(
        t, their_ros_gain, len(days), snap.period_dates.get(days[-1]) if days else None,
        _verdict(t.gain, cfg.trade_min_gain), t.their_gain >= -cfg.trade_fairness, out,
        cfg.trade_min_gain, cfg.trade_fairness,
        alternatives=find_alternatives(m, tid, give, get) if alternatives else [],
        core=[p.name for p in give],
    )


def _best_ask(t: Trade) -> Optional[str]:
    """The player you'd get when he's the best in the deal on current form, else None."""
    if max(v.now for v in t.get) > max(v.now for v in t.give):
        return max(t.get, key=lambda v: v.now).player.name
    return None


# ---- trades built around players you pick ---------------------------------------------
# The trade check's alternatives (the same team, around the players you'd send) and the
# trade scout's "build around" search (every team, around players of yours or theirs) share
# one deal generator, one way of scoring and one way of picking what to show.

ALT_MAX_PLAYERS = 3  # most players a side
ALT_MAX_TOTAL = 4  # most players in a deal: up to 3-for-1, 1-for-3 or 2-for-2
ALT_SHORTLIST = 25  # deals of each shape per team scored in full, picked on the quick estimate
ALTERNATIVES = 5  # alternatives shown with a trade check
SEARCH_SHOWN = 8  # trades shown by a build-around search
SEARCH_PER_TEAM = 2  # most of those with any one team
SHAPES = ("1-for-1", "even", "package", "spread")


def shape_of(n_give: int, n_get: int) -> str:
    """1-for-1; even (2-for-2); package (you send more); spread (you get more back)."""
    if n_give == n_get:
        return "1-for-1" if n_give == 1 else "even"
    return "package" if n_give > n_get else "spread"


def shape_label(t: Trade) -> str:
    n, k = len(t.give), len(t.get)
    tail = {"1-for-1": "", "even": " swap", "package": " package", "spread": ", more back"}[shape_of(n, k)]
    return f"{n}-for-{k}{tail}"


def _sides(core: Sequence[PlayerView], others: Sequence[PlayerView]) -> Dict[int, List[tuple]]:
    """Every group of 1 to ALT_MAX_PLAYERS players with at least one from core, by size.
    With no core, every group of others."""
    out: Dict[int, List[tuple]] = {}
    if not core:
        for n in range(1, ALT_MAX_PLAYERS + 1):
            out[n] = list(combinations(others, n))
        return out
    for n in range(1, min(len(core), ALT_MAX_PLAYERS) + 1):
        for c in combinations(core, n):
            for e in range(ALT_MAX_PLAYERS - n + 1):
                for extra in combinations(others, e):
                    out.setdefault(n + e, []).append(c + extra)
    return out


def _deals_around(m: _Market, tid: int, core: Sequence[PlayerView], targets: Sequence[PlayerView] = (),
                  skip: Optional[Tuple[FrozenSet[int], FrozenSet[int]]] = None) -> Iterator[Tuple[tuple, tuple]]:
    """Every deal with one team that sends at least one of core (packaged with any others
    you'd trade, never an untouchable you didn't pick) and gets at least one of the targets
    on that team. An empty core means any of yours; no targets there, any of theirs. Up to
    ALT_MAX_TOTAL players in all. skip leaves out one deal, the one you asked about."""
    core_ids = {p.id for p in core}
    target_ids = {p.id for p in targets}
    roster = m.teams[tid]
    gives = _sides(core, [p for p in m.tradeable if p.id not in core_ids])
    tgt = [p for p in roster if p.id in target_ids]
    gets = _sides(tgt, [p for p in roster if p.id not in target_ids]) if target_ids else _sides((), roster)
    for n, give_sets in gives.items():
        for k, get_sets in gets.items():
            if n + k > ALT_MAX_TOTAL:
                continue
            for give in give_sets:
                give_ids = frozenset(p.id for p in give)
                for get in get_sets:
                    if skip is None or (give_ids, frozenset(p.id for p in get)) != skip:
                        yield give, get


def _search_team(m: _Market, tid: int, deals: Iterable[Tuple[tuple, tuple]]) -> List[Trade]:
    """The deals that add min_gain for you and look fair enough to them. Scoring every deal in
    full is too slow, so the best ALT_SHORTLIST of each shape on the quick estimate are."""
    by_shape: Dict[str, List[_Deal]] = {}
    for d in sorted(m.quick(tid, deals), key=lambda d: d.ours, reverse=True):
        bucket = by_shape.setdefault(shape_of(len(d.give), len(d.get)), [])
        if len(bucket) < ALT_SHORTLIST:
            bucket.append(d)
    return [t for t in (m.score(tid, d) for b in by_shape.values() for d in b) if m.acceptable(t)]


def _explain(trades: List[Trade], snap: Snapshot) -> List[Trade]:
    for t in trades:
        t.reasons = _reasons(t, snap)
        if (best := _best_ask(t)) is not None:
            t.reasons.append(f"{best} is the best player in the deal on current form, so expect pushback")
    return trades


def find_alternatives(m: _Market, tid: int, give: Sequence[PlayerView], get: Sequence[PlayerView]) -> List[Trade]:
    """Other trades with the same team built around the players you chose to send: a 1-for-1,
    a package of yours for one of theirs, one of yours for two or three of theirs. Only ones
    that add min_gain for you and look fair enough to them, scored the same way as the trade
    you asked about. The best of each shape first, then the best of the rest, so a list
    isn't five versions of the same deal."""
    skip = (frozenset(p.id for p in give), frozenset(p.id for p in get))
    found = _search_team(m, tid, _deals_around(m, tid, give, (), skip))
    return _explain(_pick_alternatives(found), m.snap)


def _pick(trades: List[Trade], limit: int, per_team: Optional[int] = None) -> List[Trade]:
    """Up to limit, best for you first: the best of each shape, then the best of the rest.
    No two ask for the same players, no player of theirs leads more than two, and no more
    than per_team with any one team."""
    order = sorted(trades, key=lambda t: (round(t.gain, 1), t.their_gain), reverse=True)

    def lead(t: Trade) -> int:
        return max(t.get, key=lambda v: v.ros).player.id

    picked: List[Trade] = []

    def fits(t: Trade) -> bool:
        return (all(_ids(t.get) != _ids(c.get) for c in picked)
                and sum(lead(c) == lead(t) for c in picked) < 2
                and (per_team is None or sum(c.team_id == t.team_id for c in picked) < per_team))

    for s in SHAPES:
        if len(picked) >= limit:
            break
        t = next((t for t in order if shape_of(len(t.give), len(t.get)) == s and fits(t)), None)
        if t is not None:
            picked.append(t)
    for t in order:
        if len(picked) >= limit:
            break
        if all(t is not c for c in picked) and fits(t):
            picked.append(t)
    return sorted(picked, key=lambda t: (round(t.gain, 1), t.their_gain), reverse=True)


def _pick_alternatives(trades: List[Trade]) -> List[Trade]:
    return _pick(trades, ALTERNATIVES)


@dataclass
class TradeSearch:
    """Trades built around players you picked, with one team you picked, or both."""

    team_name: str
    core: List[str]  # yours: each trade sends at least one
    targets: List[str]  # theirs: each trade gets at least one
    trades: List[Trade]
    teams_searched: int
    days: int
    through: Optional[date]
    min_gain: float = 0.0
    fairness: float = 0.0
    partner: Optional[str] = None  # the one team searched, when you picked it


def search_trades(snap: Snapshot, cfg: Config, include_ids: Sequence[int] = (),
                  team_id: Optional[int] = None) -> TradeSearch:
    """Trades built around the players you pick (yours to send, theirs to get), with the team
    you pick, or both. With only players of yours, every other team is searched; players of
    theirs limit it to the teams that have them; a team limits it to that team. Every shape
    up to ALT_MAX_TOTAL players, scored like every other trade, and only ones that add
    min_gain for you and look fair enough to them."""
    if not include_ids and team_id is None:
        raise TradeInputError("Pick a team to trade with, or at least one player to build around.")
    m = _Market(snap, cfg)
    if team_id is not None and team_id not in m.teams:
        raise TradeInputError("That team isn't in the league any more. Refresh and try again.")
    by_id = {p.id: p for p in snap.all_players()}
    mine = {p.id for p in m.me}
    core: List[PlayerView] = []
    targets: List[PlayerView] = []
    errors = []
    for i in include_ids:
        p = by_id.get(i)
        if p is None:
            errors.append("One of those players isn't in the league any more. Refresh and try again.")
        elif p.id in mine:
            core.append(p)
        elif p.owner == "fa":
            errors.append(f"{p.name} is a free agent: pick him up instead of trading for him.")
        elif team_id is not None and p.team_id != team_id:
            errors.append(f"{p.name} is on {m.team_name(p.team_id)}, not {m.team_name(team_id)}.")
        elif p.team_id in m.teams:
            targets.append(p)
    if errors:
        raise TradeInputError("\n".join(errors))
    if team_id is not None:
        tids = [team_id]
    else:
        tids = sorted({p.team_id for p in targets}) if targets else sorted(m.teams)
    found: List[Trade] = []
    for tid in tids:
        found += _search_team(m, tid, _deals_around(m, tid, core, targets))
    days = m.days
    # One team: show its best, not just two. Several: no one manager fills the list.
    per_team = None if len(tids) == 1 else SEARCH_PER_TEAM
    return TradeSearch(
        snap.my_team_name, [p.name for p in core], [p.name for p in targets],
        _explain(_pick(found, SEARCH_SHOWN, per_team), snap), len(tids),
        len(days), snap.period_dates.get(days[-1]) if days else None, cfg.trade_min_gain, cfg.trade_fairness,
        m.team_name(team_id) if team_id is not None else None,
    )


def find_team(snap: Snapshot, query: str) -> int:
    """The other team a typed name means: an exact name, else the one whose name contains it,
    case and spacing ignored."""
    key = " ".join(query.lower().split())
    teams = {tid: name for tid, name in snap.team_names.items()
             if tid in {p.team_id for p in snap.other_rostered}}
    exact = [tid for tid, name in teams.items() if " ".join(name.lower().split()) == key]
    if len(exact) == 1:
        return exact[0]
    hits = [tid for tid, name in teams.items() if key and key in " ".join(name.lower().split())]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise TradeInputError(f'"{query}" could be {", ".join(teams[t] for t in hits)}. Use more of the name.')
    raise TradeInputError(f'No other team named "{query}". Teams: {", ".join(sorted(teams.values()))}.')


def find_included(snap: Snapshot, names: Sequence[str]) -> Tuple[List[int], List[str]]:
    """Typed names to build a search around, anyone rostered, and a note for each name read
    as a different spelling. Raises TradeInputError listing every problem."""
    pool = snap.all_players()
    ids: List[int] = []
    notes: List[str] = []
    errors: List[str] = []
    for name in names:
        try:
            p, note = find_player(name, pool)
        except TradeInputError as e:
            errors.append(str(e))
            continue
        if note:
            notes.append(note)
        if p.owner == "fa":
            errors.append(f"{p.name} is a free agent: pick him up instead of trading for him.")
        elif p.id not in ids:
            ids.append(p.id)
    if errors:
        raise TradeInputError("\n".join(errors))
    return ids, notes
