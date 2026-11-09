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
Your side uses rest of season values. Their side uses now values, since that's how the
trade looks to them. A side that ends up with more players drops its weakest, and one with
fewer picks up the best free agent. The top trades add the most to your fpts per week while
looking at least even to the other team, and where the best player in the deal on current
form goes to them: two good players for one great one looks even on paper, but managers
rarely take it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher, get_close_matches
from itertools import combinations
from typing import Callable, Dict, FrozenSet, Iterator, List, Optional, Sequence, Set, Tuple

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
    their_gain: float  # fpts per week their best lineups add, on now values
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
        return sum(self.worth(p).values())

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


def _swap(roster: List[PlayerView], out: Sequence[PlayerView], into: Sequence[PlayerView], keep: Set[int],
          value: Callable[[PlayerView], float], pool: List[PlayerView]):
    """The roster after a trade, back to its size: an extra player means dropping the lowest
    per game value (never anyone in keep or just traded for), an open spot goes to the best
    free agent in pool. Per game, not this week, so an injured regular isn't the one cut.
    Returns (roster, dropped, added)."""
    gone = {p.id for p in out}
    after = [p for p in roster if p.id not in gone] + list(into)
    drop: List[PlayerView] = []
    add: List[PlayerView] = []
    if len(after) > len(roster):
        keep = keep | {p.id for p in into}
        drop = sorted((p for p in after if p.id not in keep), key=value)[: len(after) - len(roster)]
        cut = {p.id for p in drop}
        after = [p for p in after if p.id not in cut]
    elif len(after) < len(roster):
        add = pool[: len(roster) - len(after)]
        after += add
    return after, drop, add


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


def build_trades(snap: Snapshot, cfg: Config) -> TradeReport:
    scorer = Scorer(snap, cfg)
    ros_of: Dict[int, float] = {}

    def ros(p: PlayerView) -> float:
        if p.id not in ros_of:
            ros_of[p.id] = per_game_value(p, cfg, snap.scoring, cfg.trade_weights, cfg.trade_projection_games, ROS_HASHTAG)
        return ros_of[p.id]

    def val(p: PlayerView) -> Valued:
        return Valued(p, scorer.base(p), ros(p))

    days = trade_days(snap)
    weeks = len(days) / 7
    mine_ros = Lineups(snap, cfg, days, ros)  # how a trade really changes your team
    theirs_now = Lineups(snap, cfg, days, scorer.base)  # how it looks to them

    me = snap.roster_for_moves()
    tradeable = droppable(snap, cfg)
    safe = {p.id for p in me} - {p.id for p in tradeable}
    fas = [p for p in snap.free_agents if not p.is_out]
    fa_ros = sorted(fas, key=ros, reverse=True)
    fa_now = sorted(fas, key=scorer.base, reverse=True)
    teams: Dict[int, List[PlayerView]] = {}
    for p in snap.other_rostered:
        teams.setdefault(p.team_id, []).append(p)

    my_before = mine_ros.total(me)
    my_solo = sum(mine_ros.solo(p) for p in me)
    found: List[Trade] = []
    for tid, roster in teams.items():
        their_before = theirs_now.total(roster)
        their_solo = sum(theirs_now.solo(p) for p in roster)
        # A quick pass counts every game as a start, to pick which trades to score in full.
        quick = []
        for give, get in _deals(tradeable, roster, cfg.trade_max_players):
            if max(map(scorer.base, get)) > max(map(scorer.base, give)):
                continue  # managers rarely give up the best player in a deal for lesser ones
            mine, drop, add = _swap(me, give, get, safe, ros, fa_ros)
            theirs, tdrop, tadd = _swap(roster, get, give, set(), scorer.base, fa_now)
            ours = (sum(mine_ros.solo(p) for p in mine) - my_solo) / weeks
            theirs_quick = (sum(theirs_now.solo(p) for p in theirs) - their_solo) / weeks
            if ours > 0 and theirs_quick >= -cfg.trade_fairness - SHORTLIST_SLACK:
                quick.append((ours, give, get, mine, drop, add, theirs, tdrop, tadd))
        quick.sort(key=lambda q: q[0], reverse=True)
        for _, give, get, mine, drop, add, theirs, tdrop, tadd in quick[:SHORTLIST]:
            total = mine_ros.total(mine) - my_before
            gain = total / weeks
            their_gain = (theirs_now.total(theirs) - their_before) / weeks
            if gain >= cfg.trade_min_gain and their_gain >= -cfg.trade_fairness:
                found.append(Trade(
                    tid, snap.team_names.get(tid, f"team {tid}"), [val(p) for p in give], [val(p) for p in get],
                    gain, total, their_gain, [val(p) for p in drop], [val(p) for p in add],
                    [val(p) for p in tdrop], [val(p) for p in tadd],
                ))

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
        r.append(f"to make room you'd drop {_names(t.drop)}")
    if t.add:
        r.append(f"the open spot goes to free agent {_names(t.add)}")
    if t.their_drop:
        r.append(f"they'd have to drop {' and '.join(v.player.name for v in t.their_drop)}")
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
    accepts: bool  # whether it looks no worse than the fairness bar to them on current form
    notes: List[str] = field(default_factory=list)  # read-as corrections, untouchables, the best player
    min_gain: float = 0.0
    fairness: float = 0.0


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
                   notes: Sequence[str] = ()) -> Evaluation:
    """Score a trade you name the way the trade scout scores the ones it finds: your best
    lineups over the rest of the season on rest of season values, theirs on now values (how
    it looks to them) and on rest of season values (what it really does for them). Any
    number of players a side: the side that ends up with more drops its weakest, the side
    with fewer picks up the best free agents."""
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
    roster = [p for p in snap.other_rostered if p.team_id == tid]

    scorer = Scorer(snap, cfg)
    ros_of: Dict[int, float] = {}

    def ros(p: PlayerView) -> float:
        if p.id not in ros_of:
            ros_of[p.id] = per_game_value(p, cfg, snap.scoring, cfg.trade_weights, cfg.trade_projection_games, ROS_HASHTAG)
        return ros_of[p.id]

    def val(p: PlayerView) -> Valued:
        return Valued(p, scorer.base(p), ros(p))

    days = trade_days(snap)
    weeks = max(len(days), 1) / 7
    fas = [p for p in snap.free_agents if not p.is_out]
    # Making room never cuts an untouchable; you can still choose to send one.
    safe = mine_ids - {p.id for p in droppable(snap, cfg)}

    mine_after, drop, add = _swap(me, give, get, safe, ros, sorted(fas, key=ros, reverse=True))
    theirs_now, tdrop, tadd = _swap(roster, get, give, set(), scorer.base, sorted(fas, key=scorer.base, reverse=True))
    theirs_ros, _, _ = _swap(roster, get, give, set(), ros, sorted(fas, key=ros, reverse=True))

    ros_l = Lineups(snap, cfg, days, ros)
    now_l = Lineups(snap, cfg, days, scorer.base)
    total = ros_l.total(mine_after) - ros_l.total(me)
    their_gain = (now_l.total(theirs_now) - now_l.total(roster)) / weeks
    their_ros_gain = (ros_l.total(theirs_ros) - ros_l.total(roster)) / weeks

    t = Trade(tid, snap.team_names.get(tid, f"team {tid}"), [val(p) for p in give], [val(p) for p in get],
              total / weeks, total, their_gain, [val(p) for p in drop], [val(p) for p in add],
              [val(p) for p in tdrop], [val(p) for p in tadd])
    t.reasons = _reasons(t, snap)

    out = list(notes)
    sent = [p.name for p in give if p.id in safe]
    if sent:
        out.append(f"You've marked {' and '.join(sent)} untouchable.")
    if max(v.now for v in t.get) > max(v.now for v in t.give):
        best = max(t.get, key=lambda v: v.now).player.name
        out.append(f"{best} is the best player in the deal on current form. Managers rarely give that up "
                   "for lesser players, even when the totals look even.")
    return Evaluation(
        t, their_ros_gain, len(days), snap.period_dates.get(days[-1]) if days else None,
        _verdict(t.gain, cfg.trade_min_gain), their_gain >= -cfg.trade_fairness, out,
        cfg.trade_min_gain, cfg.trade_fairness,
    )
