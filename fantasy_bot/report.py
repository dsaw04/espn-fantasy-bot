"""Formats a Report as text and optionally posts it to Discord."""

from __future__ import annotations

from typing import List

import requests

from .model import n_games
from .recommend import Report


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
                lines.append(f"- {s.slot}: {s.bench.name} won't score ({s.reason}) and nobody on your bench can fill it. A pickup below may help.")
    else:
        lines.append("- Your starters all play today. No changes needed.")
    lines.append("")

    lines.append("**Pickups**")
    if r.moves:
        for i, m in enumerate(r.moves, 1):
            a = m.add.player
            lines.append(
                f"{i}. Add **{a.name}** ({a.pro_team} {_pos(a)}, {a.percent_owned:.0f}% owned), "
                f"drop **{m.drop.player.name}**. Expected gain ~{m.gain:.0f} fpts"
            )
            for reason in m.reasons:
                lines.append(f"   - {reason}")
    elif r.chopping_block:
        lines.append("- Nobody available beats your chopping block by enough to be worth a move today.")
    else:
        lines.append("- Your chopping block is empty. Add names in bot.toml.")
    lines.append("")

    if r.chopping_block:
        lines.append("**Chopping block (weakest first)**")
        for v in r.chopping_block:
            p = v.player
            lines.append(f"- {p.name}: {n_games(v.games)}, ~{v.base:.1f} fpts/g, {p.status_label()}, value {v.expected:.0f}")
        lines.append("")

    if r.watchlist:
        lines.append("**Depth watch** (backups getting a bump from injuries)")
        for v in r.watchlist:
            p = v.player
            top = max(v.openings, key=lambda o: o.boost)
            lines.append(
                f"- {p.name} ({p.pro_team} {_pos(p)}): {top.injured.name} out, "
                f"~+{top.boost:.1f} fpts/g, {n_games(v.games)}, value {v.expected:.0f}"
            )
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
