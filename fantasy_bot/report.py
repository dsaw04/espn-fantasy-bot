"""Formats a Report as text and optionally posts it to Discord."""

from __future__ import annotations

from typing import List, Sequence

import requests

from .model import n_games
from .history import brier
from .lineup import TodayPlan
from .recommend import HASHTAG_WINDOWS, Report
from .trade import Evaluation, TradeReport, TradeSearch, form, shape_label


WEAKEST_SHOWN = 5


def _pos(p) -> str:
    return "/".join(sorted(p.positions)) or "?"


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _matchup_lines(r: Report) -> List[str]:
    m, o = r.matchup, r.outlook
    days_left = sum(1 for sp in m.days if sp >= r.today)
    adds = "no add limit" if m.adds_left is None else f"{m.adds_left} of {m.adds_limit} adds left"
    lines = [
        f"**This week vs {m.opp.name}**: {m.me.score:.0f} to {m.opp.score:.0f}, projected "
        f"{o.now.mine:.0f} to {o.now.theirs:.0f}. You win {_pct(o.now.win)} of the time. "
        f"{days_left} {'day' if days_left == 1 else 'days'} left including today, {adds}."
    ]
    if o.next is not None:
        lines.append(
            f"**Next week vs {m.next_opp.name}**: projected {o.next.mine:.0f} to {o.next.theirs:.0f}. "
            f"You win {_pct(o.next.win)} of the time."
        )
    if o.now.decided and o.next is not None:
        lines.append("This week is all but decided, so pickups are judged on next week.")
    return lines + [""]


def _review_lines(r: Report) -> List[str]:
    v = r.review
    result = "won" if v.won else "lost"
    head = f"**Last week vs {v.opp}**: {result} {v.me:.0f} to {v.them:.0f}."
    if v.predicted is not None:
        head += f" The first report that week gave you {_pct(v.predicted)}."
    lines = [head]
    if v.suggested:
        line = f"- You made {v.made} of {v.suggested} suggested pickups."
        if v.made_gain is not None:
            line += f" Those adds out-scored their drops by {v.made_gain:+.0f} fpts in the week after."
        lines.append(line)
    if v.error is not None:
        side = "below" if v.bias < 0 else "above"
        lines.append(
            f"- Per game estimates for your players were off by {v.error:.1f} fpts/g on average "
            f"(actual {abs(v.bias):.1f} {side} the estimate; {_pct(v.rel_error)} miss vs projection_error {_pct(r.projection_error)})."
        )
    if r.season:
        wins = sum(x.won for x in r.season)
        b = brier(r.season)
        line = f"- Season: {wins}-{len(r.season) - wins}."
        if b is not None:
            line += f" Forecast Brier score {b:.2f} (0 is perfect, 0.25 is a coin flip)."
        lines.append(line)
    return lines + [""]


def _win_change(m) -> str:
    """Each week's fpts and win chance change. A decided week's fpts are left out: they don't matter."""
    parts = []
    for label, b, a in (("this week", m.before.now, m.after.now), ("next week", m.before.next, m.after.next)):
        if b is None or a is None or (b.decided and abs(a.win - b.win) < 0.005):
            continue
        parts.append(f"{a.mine - b.mine:+.0f} fpts {label} ({_pct(b.win)} to {_pct(a.win)})")
    return ", ".join(parts) or "no change to your win chances"


def move_value(m, r: Report) -> str:
    """What a move is worth: win chances with a matchup, fpts without one, per game for a core upgrade."""
    if m.kind == "stream" and m.win_gain is not None:
        return f"~{_win_change(m)}"
    if m.kind == "stream":
        return f"~{m.gain:+.0f} fpts over the next {r.horizon_days} days"
    return f"~{m.edge:+.1f} fpts/g ({m.per_game:.1f} vs {m.drop_per_game:.1f})"


def _move_lines(moves, r: Report, start: int = 1) -> List[str]:
    lines = []
    for i, m in enumerate(moves, start):
        a = m.add.player
        head = f"{i}. Add **{a.name}** ({a.pro_team} {_pos(a)}, {a.percent_owned:.0f}% owned), drop **{m.drop.player.name}**"
        head += f" (streaming spot). {move_value(m, r)}" if m.kind == "stream" else f". {move_value(m, r)}"
        lines.append(head)
        for reason in m.reasons:
            lines.append(f"   - {reason}")
    return lines


def _hashtag_rank(p) -> str:
    """Where Hashtag ranks him in the league's scoring, which is what the bot weighs, not its 9-cat rank."""
    for window, label in HASHTAG_WINDOWS:
        line = p.hashtag.get(window)
        if line and line.points_rank:
            return f" (Hashtag #{line.points_rank} in your scoring, {label})"
    return ""


def _slot(s: str) -> str:
    return "bench" if s == "BE" else s


def lineup_needs_changes(plan: TodayPlan, min_gain: float) -> bool:
    """Worth telling you about: it adds enough today, or it benches a starter who won't score."""
    if plan.gain <= 0:
        return False
    zero = any(c.new == "BE" and c.reason and c.reason != "others score more" for c in plan.changes)
    return plan.gain >= min_gain or zero


def lineup_lines(plan: TodayPlan, min_gain: float) -> List[str]:
    lines = [f"- {w}" for w in plan.warnings]
    if lineup_needs_changes(plan, min_gain):
        lines.insert(0, f"**Set your lineup today** (~{plan.gain:+.0f} fpts)")
        for c in plan.changes:
            why = f" ({c.reason})" if c.reason else ""
            lines.append(f"- {c.player.name}: {_slot(c.old)} to {_slot(c.new)}{why}")
    elif plan.idle:
        who = "; ".join(f"{p.name}: {why}" for p, why in plan.idle)
        lines.insert(0, f"**Lineup today**: {plan.empty} starting {'spot' if plan.empty == 1 else 'spots'} "
                        f"will score nothing ({who}), and nobody on your bench plays.")
    elif lines:
        lines.insert(0, "**Lineup today**")
    else:
        lines.append("**Lineup today**: set. No changes needed.")
    return lines + [""]


def render(r: Report) -> str:
    ahead = "the rest of this week and next" if r.matchup else f"{r.horizon_days} days ahead"
    lines: List[str] = [f"**{r.team_name}: daily scout** (scoring period {r.today}, looking {ahead})", ""]
    if r.matchup and r.outlook:
        lines += _matchup_lines(r)
    if r.review is not None:
        lines += _review_lines(r)
    if r.open_spots:
        days = ", ".join(f"{d:%a %b} {d.day}: {n}" for d, n in r.open_spots)
        lines += [f"**Empty lineup spots** (streamers score most here): {days}", ""]

    lines.append("**Injury report (your roster)**")
    if r.injured_on_roster:
        for p in r.injured_on_roster:
            note = f". {p.injury_note}" if p.injury_note else ""
            lines.append(f"- {p.name} ({p.pro_team} {_pos(p)}): {p.status_label()}{note}")
    else:
        lines.append("- Everyone is healthy.")
    lines.append("")

    lines += lineup_lines(r.lineup, r.lineup_min_gain)

    lines.append("**Pickups** (adds play from tomorrow)" if r.moves_start_tomorrow else "**Pickups**")
    if r.moves:
        lines += _move_lines(r.moves, r)
    elif r.matchup and r.matchup.adds_left == 0:
        lines.append(
            f"- Out of adds: all {r.matchup.adds_limit} used this matchup. They reset when the next one starts."
        )
    elif r.roster:
        names = " and ".join(v.player.name for v in r.roster if v.player.id in r.streamers)
        if r.matchup:
            bar = f"adds {r.win_bar * 100:.1f}+ points to your win chances this week and next"
        else:
            bar = f"beats them by {r.stream_min_gain:g}+ fpts over the next {r.horizon_days} days"
        lines.append(
            f"- Hold. No pickup over your streaming spots ({names}) {bar}, and nobody beats a core player "
            f"by a consistent {r.min_edge:g}+ fpts/g."
        )
    else:
        lines.append("- Everyone on your roster is untouchable, so there is nobody to drop.")
    if r.options:
        lines.append("Other options, each judged on its own against your roster as it is:" if r.moves
                     else "Closest options:")
        lines += _move_lines(r.options, r, start=len(r.moves) + 1)
    lines.append("")

    if r.roster:
        lines.append("**Your weakest players** (per game, untouchables left out)")
        for v in r.roster[:WEAKEST_SHOWN]:
            p = v.player
            spot = " (streaming spot)" if p.id in r.streamers else ""
            if r.week is not None:
                plays = f"starts {r.week.starts.get(p.id, 0)} of his {n_games(r.week.games.get(p.id, 0))}"
            else:
                plays = n_games(v.games)
            pg = r.per_game.get(p.id, v.base)
            lines.append(f"- {p.name}{spot}: ~{pg:.1f} fpts/g{_hashtag_rank(p)}, {p.status_label()}, "
                         f"{plays} in the next {r.horizon_days} days")
        lines.append("")

    if r.watchlist:
        weakest = r.roster[0].player.name if r.roster else "your weakest player"
        lines.append("**Depth watch** (roles opened by injuries)")
        for w in r.watchlist:
            p = w.player
            if w.games_without:
                basis = (
                    f"{w.per_game:.1f} fpts/g in {n_games(w.games_without)} without {w.injured.name}, "
                    f"{w.edge:+.1f}/g vs {weakest}, median game {w.median:.0f}"
                )
            else:
                basis = (
                    f"est. ~{w.per_game:.1f} fpts/g while {w.injured.name} is out (no games without him yet), "
                    f"{w.edge:+.1f}/g vs {weakest}"
                )
            lines.append(f"- {p.name} ({p.pro_team} {_pos(p)}, {p.percent_owned:.0f}% owned): {basis}")
    return "\n".join(lines).rstrip() + "\n"


def pickups_line(t) -> str:
    """What each side would be up with the free agents it could pick up after the trade. For
    reference only: a trade's value leaves free agents out."""
    return (f"With free agent pickups too (not counted): {t.moves_gain:+.1f}/week for you, "
            f"{t.their_moves_gain:+.1f}/week to them.")


def breakdown(t) -> str:
    """Face value next to the weekly number, and what free agents would add on top, which a
    trade's value never counts: a waiver pickup is a move either side can make without it."""
    line = f"Face value (the players over a free agent pickup): {t.face:+.1f}/week for you, {t.their_face:+.1f}/week to them"
    if t.new_add or t.their_new_add:
        line += f". {pickups_line(t)[:-1]}"
    return line


def bar_text(min_gain: float, fairness: float) -> str:
    """What a listed trade has to do: help your lineup, not lose you the trade, and, unless
    you've stopped asking, be fair enough to them."""
    you = (f"adds {min_gain:g}+ fpts/week for you on the players alone, no free agents, without losing you "
           "the trade at face value")
    if fairness == float("inf"):
        return you
    return f"{you}, and {their_bar(fairness)}"


def their_bar(fairness: float) -> str:
    """What a trade has to do for the other team at face value, from trade_fairness: above 0 it may be
    that much worse for them."""
    if fairness > 0:
        return f"is no more than {fairness:g} fpts/week worse for them, on the players alone and at face value"
    return "is at least even for them, on the players alone and at face value"


def _trade_head(i: int, t) -> str:
    give = " and ".join(f"**{v.player.name}**" for v in t.give)
    get = " and ".join(f"**{v.player.name}**" for v in t.get)
    return (
        f"{i}. Send {give} to **{t.team_name}** for {get}. ~{t.gain:+.1f} fpts/week for you "
        f"({t.total:+.0f} over the rest of the season). To them it looks {t.their_gain:+.1f}/week."
    )


def render_trades(r: TradeReport) -> str:
    through = f" through {r.through:%b} {r.through.day}" if r.through else ""
    lines: List[str] = [
        f"**{r.team_name}: trade scout** (rest of season, {r.days} days{through})",
        "",
        "Each player has two values: *now* is recent form, which is what other managers go by. *Rest of "
        "season* is his projected minutes times points per minute, with last season and ESPN's projection "
        "pulling hot and cold starts back. Trades are scored with "
        "your best lineup each day: rest of season values for you; for them, a blend of now and rest of "
        "season, since managers go by recent form but plan ahead too. A trade is valued on the players "
        "alone: the side with more drops its weakest, the side with fewer leaves the spots open, and no free "
        "agent is counted on either side. Face value counts each player over a free agent either manager "
        "could pick up, so waiver-level players are worth about nothing in a trade.",
        "",
        f"Replacement level: ~{r.replacement:.1f} fpts/g, the third best free agent on rest of season value. "
        "Players near it count for little at face value, however many fpts they score.",
        "",
    ]
    if r.before:
        lines += [r.before, ""]
    lines.append("**Top trades**")
    if len(r.trades) > 1:
        lines.append("Alternatives, each judged on its own against your roster as it is:")
    if r.trades:
        for i, t in enumerate(r.trades, 1):
            lines.append(_trade_head(i, t))
            lines += [f"   - {breakdown(t)}"] + [f"   - {reason}" for reason in t.reasons]
    else:
        lines.append(f"- None. No trade {bar_text(r.min_gain, r.fairness)}.")
    lines.append("")

    if r.sell_high:
        lines.append("**Sell high** (your players running above their rest of season value)")
        lines += [f"- {v.player.name} ({v.player.pro_team} {_pos(v.player)}): {form(v)} ({v.gap:+.1f})" for v in r.sell_high]
        lines.append("")
    if r.buy_low:
        lines.append("**Buy low** (other teams' players running below theirs, good enough to start for you)")
        lines += [f"- {v.player.name} ({v.player.pro_team} {_pos(v.player)}, {team}): {form(v)} ({v.gap:+.1f})"
                  for v, team in r.buy_low]
    return "\n".join(lines).rstrip() + "\n"


VERDICTS = {
    "good": "Take it",
    "feeding": "Careful, you lose this one",
    "even": "About even",
    "bad": "Pass",
}


def evaluation_headline(e: Evaluation) -> str:
    """The verdict in one line: what it does for you, then whether they'd go for it."""
    t = e.trade
    you = (f"{VERDICTS[e.verdict]}: ~{t.gain:+.1f} fpts/week for you ({t.total:+.0f} over the "
           f"rest of the season), {t.face:+.1f}/week at face value.")
    if e.accepts:
        them = f"They should go for it: {t.their_gain:+.1f}/week for them ({t.their_face:+.1f} at face value)."
    else:
        them = f"They'd likely say no: {t.their_gain:+.1f}/week for them ({t.their_face:+.1f} at face value)."
    return f"{you} {them}"


def render_evaluation(e: Evaluation) -> str:
    t = e.trade
    through = f" through {e.through:%b} {e.through.day}" if e.through else ""
    give = " and ".join(v.player.name for v in t.give)
    get = " and ".join(v.player.name for v in t.get)
    lines: List[str] = [
        f"**Trade check**: {give} to {t.team_name} for {get} (rest of season, {e.days} days{through})",
        "",
        f"**{evaluation_headline(e)}**",
        f"On rest of season values it really adds {e.their_ros_gain:+.1f}/week for them.",
        "",
        f"Per game, you send {sum(v.ros for v in t.give):.1f} rest of season ({sum(v.now for v in t.give):.1f} now) "
        f"and get {sum(v.ros for v in t.get):.1f} ({sum(v.now for v in t.get):.1f} now). Face value counts each "
        f"player over ~{e.replacement:.1f}/g, the third best free agent.",
        "",
        "**Lineups vs face value**",
        f"- Lineups, only games that make the best lineup each day, the side with more players dropping its "
        f"weakest and no free agents: {t.gain:+.1f}/week for you, {t.their_gain:+.1f}/week to them.",
        f"- Face value, the players in the deal alone, each over a free agent pickup, every game counted: "
        f"{t.face:+.1f}/week for you, "
        f"{t.their_face:+.1f}/week to them.",
    ]
    if t.new_add or t.their_new_add:
        lines.append(f"- {pickups_line(t)}")
    if e.notes:
        lines += [""] + [f"- {n}" for n in e.notes]
    lines += ["", "**Details**"] + [f"- {r}" for r in t.reasons]
    lines += ["", alternatives_heading(e)]
    if e.alternatives:
        lines.append("Each judged on its own against your roster as it is:")
        for i, a in enumerate(e.alternatives, 1):
            give = " and ".join(f"**{v.player.name}**" for v in a.give)
            get = " and ".join(f"**{v.player.name}**" for v in a.get)
            lines.append(f"{i}. {shape_label(a)}: send {give} for {get}. ~{a.gain:+.1f} fpts/week for you "
                         f"({a.total:+.0f} over the rest of the season). To them it looks {a.their_gain:+.1f}/week.")
            lines += [f"   - {breakdown(a)}"] + [f"   - {r}" for r in a.reasons]
    else:
        lines.append(f"- {no_alternatives(e)}")
    return "\n".join(lines).rstrip() + "\n"


def _either(names: List[str]) -> str:
    return " or ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + f" or {names[-1]}"


def search_subject(s: TradeSearch) -> str:
    """'sending Miller', 'getting Trae Young', 'sending Miller for Trae Young', 'with Bol Bol's
    Burner', or 'with Bol Bol's Burner sending Miller'."""
    parts = []
    if s.partner:
        parts.append(f"with {s.partner}")
    if s.core:
        parts.append(f"sending {_either(s.core)}")
    if s.targets:
        parts.append(f"getting {_either(s.targets)}" if not s.core else f"for {_either(s.targets)}")
    return " ".join(parts)


def search_none(s: TradeSearch) -> str:
    teams = "team" if s.teams_searched == 1 else "teams"
    where = "" if s.partner else f"Across {s.teams_searched} {teams}, no"
    return (f"None. {where or 'No'} trade {search_subject(s)} "
            f"{bar_text(s.min_gain, s.fairness)}.")


def render_search(s: TradeSearch, notes: Sequence[str] = ()) -> str:
    through = f" through {s.through:%b} {s.through.day}" if s.through else ""
    lines: List[str] = [
        f"**{s.team_name}: trades {search_subject(s)}** (rest of season, {s.days} days{through})",
        "",
    ]
    notes = list(notes) + ([s.before] if s.before else [])
    lines += [f"- {n}" for n in notes] + ([""] if notes else [])
    if not s.trades:
        lines.append(f"- {search_none(s)}")
        return "\n".join(lines).rstrip() + "\n"
    teams = "team" if s.teams_searched == 1 else "teams"
    where = f"Best with {s.partner}" if s.partner else f"Best across {s.teams_searched} {teams}"
    lines.append(f"{where}, each judged on its own against your roster as it is:")
    for i, t in enumerate(s.trades, 1):
        give = " and ".join(f"**{v.player.name}**" for v in t.give)
        get = " and ".join(f"**{v.player.name}**" for v in t.get)
        lines.append(f"{i}. {shape_label(t)} with **{t.team_name}**: send {give} for {get}. ~{t.gain:+.1f} fpts/week "
                     f"for you ({t.total:+.0f} over the rest of the season). To them it looks {t.their_gain:+.1f}/week.")
        lines += [f"   - {breakdown(t)}"] + [f"   - {r}" for r in t.reasons]
    return "\n".join(lines).rstrip() + "\n"


def alternatives_heading(e: Evaluation) -> str:
    core = " or ".join(e.core) if len(e.core) <= 2 else ", ".join(e.core[:-1]) + f" or {e.core[-1]}"
    return f"**Other trades with {e.trade.team_name}** (built around {core})"


def no_alternatives(e: Evaluation) -> str:
    return (f"None. No other 1-for-1, package or spread with {e.trade.team_name} "
            f"{bar_text(e.min_gain, e.fairness)}.")


def post_to_discord(webhook: str, text: str) -> None:
    # Discord caps messages at 2000 characters, so send in line-aligned chunks.
    chunk = ""
    for line in text.splitlines(keepends=True):
        if len(chunk) + len(line) > 1900:
            requests.post(webhook, json={"content": chunk}, timeout=15).raise_for_status()
            chunk = ""
        chunk += line
    if chunk.strip():
        requests.post(webhook, json={"content": chunk}, timeout=15).raise_for_status()
