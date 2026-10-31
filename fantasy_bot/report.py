"""Formats a Report as text and optionally posts it to Discord."""

from __future__ import annotations

from typing import List

import requests

from .model import n_games
from .recommend import Report


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


def _win_change(m) -> str:
    parts = []
    for label, b, a in (("this week", m.before.now, m.after.now), ("next week", m.before.next, m.after.next)):
        if b is not None and a is not None and abs(a.win - b.win) >= 0.005:
            parts.append(f"{label} {_pct(b.win)} to {_pct(a.win)}")
    return ", ".join(parts) or "no change to your win chances"


def render(r: Report) -> str:
    ahead = "the rest of this week and next" if r.matchup else f"{r.horizon_days} days ahead"
    lines: List[str] = [f"**{r.team_name}: daily scout** (scoring period {r.today}, looking {ahead})", ""]
    if r.matchup and r.outlook:
        lines += _matchup_lines(r)

    lines.append("**Injury report (your roster)**")
    if r.injured_on_roster:
        for p in r.injured_on_roster:
            lines.append(f"- {p.name} ({p.pro_team} {_pos(p)}): {p.status_label()}")
    else:
        lines.append("- Everyone is healthy.")
    lines.append("")

    lines.append("**Lineup swaps for today**")
    if r.swaps:
        for s in r.swaps:
            if s.start:
                lines.append(f"- {s.slot}: sit {s.bench.name} ({s.reason}), start {s.start.name}")
            else:
                lines.append(f"- {s.slot}: {s.bench.name} won't score ({s.reason}) and nobody on your bench can fill it.")
    else:
        lines.append("- Your starters all play today. No changes needed.")
    lines.append("")

    lines.append("**Pickups** (adds play from tomorrow)" if r.moves_start_tomorrow else "**Pickups**")
    if r.moves:
        for i, m in enumerate(r.moves, 1):
            a = m.add.player
            head = f"{i}. Add **{a.name}** ({a.pro_team} {_pos(a)}, {a.percent_owned:.0f}% owned), drop **{m.drop.player.name}**"
            if m.kind == "stream" and m.win_gain is not None:
                head += f" (streaming spot). ~{m.gain:+.0f} fpts, {_win_change(m)}"
            elif m.kind == "stream":
                head += f" (streaming spot). ~{m.gain:+.0f} fpts over the next {r.horizon_days} days"
            else:
                head += f". ~{m.edge:+.1f} fpts/g ({m.per_game:.1f} vs {m.drop_per_game:.1f})"
            lines.append(head)
            for reason in m.reasons:
                lines.append(f"   - {reason}")
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
            lines.append(f"- {p.name}{spot}: ~{pg:.1f} fpts/g, {p.status_label()}, {plays} in the next {r.horizon_days} days")
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
