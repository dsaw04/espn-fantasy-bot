"""Return dates and notes from ESPN's public NBA injury report.

ESPN fantasy usually has no return date for an injured player, so the bot
couldn't tell a 3-day absence from a 3-week one. ESPN's site has a free
injury feed, no login needed, with an estimated return date and a short note
for every injured player. Players match on ESPN's athlete id, which fantasy
uses too, so no name matching is needed in the usual case.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional

import requests

from .model import PlayerView

URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
NOTE_CHARS = 200
_ID = re.compile(r"/id/(\d+)")


@dataclass
class Injury:
    athlete_id: Optional[int]
    name: str
    status: str  # "Out", "Day-To-Day"
    return_date: Optional[date]
    note: str


def _norm(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(ascii_name.lower().replace(".", "").replace("'", "").split())


def parse(data: dict) -> List[Injury]:
    out = []
    for team in data.get("injuries", []):
        for i in team.get("injuries", []):
            a = i.get("athlete", {})
            athlete_id = None
            for link in a.get("links", []):
                m = _ID.search(link.get("href", ""))
                if m:
                    athlete_id = int(m.group(1))
                    break
            raw = (i.get("details") or {}).get("returnDate")
            try:
                back = date.fromisoformat(raw) if raw else None
            except ValueError:
                back = None
            out.append(Injury(athlete_id, a.get("displayName", ""), i.get("status", ""), back, i.get("shortComment", "")))
    return out


def fetch(timeout: float = 20.0) -> List[Injury]:
    resp = requests.get(URL, timeout=timeout)
    resp.raise_for_status()
    return parse(resp.json())


def attach(players: Iterable[PlayerView], injuries: List[Injury], today: Optional[date] = None) -> int:
    """Fills in return dates ESPN fantasy doesn't have, and the latest note, for
    injured players. A date that has already passed while he's still out is stale,
    so it's left out rather than making him look available. Returns how many matched."""
    by_id: Dict[int, Injury] = {i.athlete_id: i for i in injuries if i.athlete_id}
    by_name: Dict[str, List[Injury]] = {}
    for i in injuries:
        by_name.setdefault(_norm(i.name), []).append(i)
    matched = 0
    for p in players:
        inj = by_id.get(p.id)
        if inj is None:
            same = by_name.get(_norm(p.name), [])
            inj = same[0] if len(same) == 1 else None
        if inj is None or not (p.is_out or p.is_risky):
            continue
        matched += 1
        fresh = inj.return_date is not None and (today is None or inj.return_date > today)
        if p.expected_return is None and fresh:
            p.expected_return = inj.return_date
        note = inj.note.strip()
        p.injury_note = note if len(note) <= NOTE_CHARS else note[: NOTE_CHARS - 3].rsplit(" ", 1)[0] + "..."
    return matched
