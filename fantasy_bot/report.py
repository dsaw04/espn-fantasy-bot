"""Formats a Report as text and optionally posts it to Discord."""

from __future__ import annotations

from typing import List

import requests

from .model import n_games
from .recommend import Report


WEAKEST_SHOWN = 5


def _pos(p) -> str:
    return "/".join(sorted(p.positions)) or "?"


def render(r: Report) -> str:
    lines: List[str] = [f"**{r.team_name}: daily scout** (scoring period {r.today}, looking {r.horizon_days} days ahead)", ""]

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
            if m.kind == "stream":
                head += f" (streaming spot). ~{m.gain:+.0f} fpts over the next {r.horizon_days} days"
            else:
                head += f". ~{m.edge:+.1f} fpts/g ({m.per_game:.1f} vs {m.drop_per_game:.1f})"
            lines.append(head)
            for reason in m.reasons:
                lines.append(f"   - {reason}")
    elif r.roster:
        names = " and ".join(v.player.name for v in r.roster if v.player.id in r.streamers)
        lines.append(
            f"- Hold. Nobody beats your streaming spots ({names}) by {r.stream_min_gain:g}+ fpts over the next "
            f"{r.horizon_days} days, or a core player by a consistent {r.min_edge:g}+ fpts/g."
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
