"""Scrapes Hashtag Basketball's 9-cat rankings and joins them onto ESPN players.

Adapted from cdchan/fantasy-basketball's scrape_hashtagbasketball.py. That
script read the projections page, which now shows free users only the top 30.
The rankings page uses the same table, lists every player who has played, and
can show the season or just the last 7, 14 or 30 days, which is what waiver
decisions need.

The page is ASP.NET WebForms. Changing a dropdown is a form postback that
carries __VIEWSTATE, so each window costs two POSTs on top of the first GET.
Picking a window resets the page to 200 rows, so "show all" goes second.

Run on its own with:  python -m fantasy_bot.hashtag [--windows last_7 ...] [--out DIR]
"""

from __future__ import annotations

import argparse
import csv
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

import lxml.html
import requests

from .model import HashtagLine, PlayerView

URL = "https://hashtagbasketball.com/fantasy-basketball-rankings"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; espn-fantasy-bot)"}
EASTERN = ZoneInfo("America/New_York")

# DDDURATION option values on the page
WINDOWS = {"season": "1", "last_7": "7", "last_14": "14", "last_30": "30"}
DEFAULT_WINDOWS = ("season", "last_7", "last_14")
SHOW_ALL = "900"
ESPN_POSITIONS = "3"  # DDPOSFROM: list ESPN position eligibility

# Hashtag abbreviations that differ from espn-api's
TEAM_TO_ESPN = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "PHI": "PHL", "SA": "SAS"}
# table header -> category key, matching espn-api stat names
CATEGORIES = {
    "FG%": "FG%", "FT%": "FT%", "3PM": "3PM", "PTS": "PTS", "TREB": "REB",
    "AST": "AST", "STL": "STL", "BLK": "BLK", "TO": "TO",
}
_CONTROL = "ctl00$ContentPlaceHolder1$"
_TABLE = '//table[@id="ContentPlaceHolder1_GridView1"]'


class HashtagError(RuntimeError):
    """The page didn't look the way the scraper expects."""


@dataclass
class Rankings:
    updated: Optional[date]  # the "Updated:" date printed on the page
    windows: Dict[str, List[HashtagLine]] = field(default_factory=dict)


# ---- fetching ---------------------------------------------------------------
def form_fields(root) -> Dict[str, str]:
    """The values a browser would post back for the page's form."""
    data: Dict[str, str] = {}
    for el in root.xpath('//form[@id="form1"]//input[@name]'):
        name, kind = el.get("name"), (el.get("type") or "text").lower()
        # The grid's own hidden inputs are display state and only bloat the post.
        if el.get("disabled") is not None or kind in ("submit", "button", "image") or "$GridView1$" in name:
            continue
        if kind in ("checkbox", "radio"):
            if el.get("checked") is not None:
                data[name] = el.get("value") or "on"
            continue
        data[name] = el.get("value", "")
    for sel in root.xpath('//form[@id="form1"]//select[@name]'):
        if sel.get("disabled") is not None:
            continue
        opt = sel.xpath("./option[@selected]") or sel.xpath("./option")
        data[sel.get("name")] = opt[0].get("value", "") if opt else ""
    return data


def _selected(root, control: str) -> Optional[str]:
    vals = root.xpath(f'//select[@name="{_CONTROL}{control}"]/option[@selected]/@value')
    return vals[0] if vals else None


def _page(resp: requests.Response):
    resp.raise_for_status()
    return lxml.html.fromstring(resp.text)


def _postback(session: requests.Session, root, timeout: float, **changes: str):
    """Set dropdowns and post the form as if the last one had been changed."""
    data = form_fields(root)
    for control, value in changes.items():
        data[_CONTROL + control] = value
    data["__EVENTTARGET"] = _CONTROL + list(changes)[-1]
    data["__EVENTARGUMENT"] = ""
    root = _page(session.post(URL, data=data, headers=HEADERS, timeout=timeout))
    for control, value in changes.items():
        if _selected(root, control) != value:
            raise HashtagError(f"page ignored {control}={value}; the form may have changed")
    return root


def fetch_rankings(
    windows: Sequence[str] = DEFAULT_WINDOWS,
    session: Optional[requests.Session] = None,
    timeout: float = 30,
) -> Rankings:
    unknown = [w for w in windows if w not in WINDOWS]
    if unknown:
        raise ValueError(f"unknown window(s) {unknown}; pick from {list(WINDOWS)}")
    s = session or requests.Session()
    root = _page(s.get(URL, headers=HEADERS, timeout=timeout))
    out = Rankings(updated=updated_on(root))
    for w in windows:
        root = _postback(s, root, timeout, DDPOSFROM=ESPN_POSITIONS, DDDURATION=WINDOWS[w])
        root = _postback(s, root, timeout, DDSHOW=SHOW_ALL)
        out.windows[w] = parse_rankings(root)
    return out


# ---- parsing ----------------------------------------------------------------
def _num(text: str) -> float:
    try:
        return float(text.strip())
    except ValueError:
        return 0.0


def _zscore(td) -> Optional[float]:
    # Each stat cell hides its z-score in an input next to a css class name input.
    for v in td.xpath('.//input[@type="hidden"]/@value'):
        try:
            return float(v)
        except ValueError:
            continue
    return None


def _shown(td) -> str:
    spans = td.xpath("./span[@id]")
    return (spans[0] if spans else td).text_content().strip()


def _parse_row(tds, columns: List[str]) -> HashtagLine:
    cells = dict(zip(columns, tds))
    rank = re.match(r"\d+", cells["R#"].text_content().strip())
    link = cells["PLAYER"].xpath(".//a")
    if not rank or not link:
        raise ValueError("not a player row")
    pid = re.search(r"/(\d+)/player", link[0].get("href", ""))
    team = cells["TEAM"].text_content().strip()
    line = HashtagLine(
        name=link[0].text_content().strip(),
        team=TEAM_TO_ESPN.get(team, team),
        positions=[p for p in cells["POS"].text_content().strip().split("/") if p],
        rank=int(rank.group()),
        games=int(_num(cells["GP"].text_content())),
        mpg=_num(cells["MPG"].text_content()),
        total=_num(cells["TOTAL"].text_content()),
        hashtag_id=int(pid.group(1)) if pid else None,
    )
    for header, cat in CATEGORIES.items():
        td = cells.get(header)
        if td is None:
            continue
        line.stats[cat] = _num(_shown(td))
        z = _zscore(td)
        if z is not None:
            # Hashtag scores turnovers as "more is higher". Flip so every category reads higher = better.
            line.zscores[cat] = -z if cat == "TO" else z
        if cat in ("FG%", "FT%"):  # "(9.3/18.0)" under the percentage
            made_att = re.search(r"\(([\d.]+)/([\d.]+)\)", td.text_content())
            if made_att:
                line.stats[cat[:2] + "M"] = float(made_att.group(1))
                line.stats[cat[:2] + "A"] = float(made_att.group(2))
    return line


def parse_rankings(page) -> List[HashtagLine]:
    root = lxml.html.fromstring(page) if isinstance(page, (str, bytes)) else page
    table = root.xpath(_TABLE)
    if not table:
        raise HashtagError("rankings table not found")
    header = table[0].xpath(".//tr[th]")
    if not header:
        raise HashtagError("rankings table has no header row")
    columns = [th.text_content().strip().upper() for th in header[0].xpath("./th")]
    missing = {"R#", "PLAYER", "POS", "TEAM", "GP", "MPG", "TOTAL"} - set(columns)
    if missing:
        raise HashtagError(f"rankings table is missing columns {sorted(missing)}")

    lines = []
    for tr in table[0].xpath(".//tr[td]"):
        tds = tr.xpath("./td")
        if len(tds) != len(columns) or tds[0].text_content().strip() == "R#":
            continue  # the table repeats its header every so often
        try:
            lines.append(_parse_row(tds, columns))
        except ValueError:
            continue
    if not lines:
        raise HashtagError("rankings table has no player rows")
    return lines


def updated_on(root) -> Optional[date]:
    for label in root.xpath('//strong[normalize-space()="Updated:"]'):
        m = re.search(r"Updated:\s*(\d{1,2} [A-Za-z]+ \d{4})", label.getparent().text_content())
        if m:
            try:
                return datetime.strptime(m.group(1), "%d %B %Y").date()
            except ValueError:
                pass
    return None


# ---- joining onto ESPN players ----------------------------------------------
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
# Hashtag spelling -> ESPN spelling, both as name keys, for names normalising can't fix
ALIASES = {
    "alexandre sarr": "alex sarr",
    "ron holland": "ronald holland",
}


def name_key(name: str) -> str:
    """'Nikola Jokić' and 'Nikola Jokic' match, so do 'Michael Porter Jr.' and 'Michael Porter'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'`]", "", s)
    key = " ".join(p for p in re.split(r"[^a-z0-9]+", s) if p and p not in _SUFFIXES)
    return ALIASES.get(key, key)


def attach(players: Iterable[PlayerView], rankings: Rankings) -> int:
    """Puts each window's line on the matching player. Returns how many players matched."""
    indexes: Dict[str, Dict[str, List[HashtagLine]]] = {}
    for w, lines in rankings.windows.items():
        idx: Dict[str, List[HashtagLine]] = defaultdict(list)
        for line in lines:
            idx[name_key(line.name)].append(line)
        indexes[w] = idx

    matched = 0
    for p in players:
        key = name_key(p.name)
        for w, idx in indexes.items():
            found = idx.get(key, [])
            if len(found) > 1:  # two players share a name, so go by team
                found = [line for line in found if line.team == p.pro_team]
            if len(found) == 1:
                p.hashtag[w] = found[0]
        matched += bool(p.hashtag)
    return matched


# ---- saving -----------------------------------------------------------------
_STAT_COLUMNS = ["FG%", "FGM", "FGA", "FT%", "FTM", "FTA", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TO"]


def write_csv(rankings: Rankings, out_dir: str | Path, day: Optional[date] = None) -> List[Path]:
    """Writes rankings_<window>.csv plus a dated copy under historical/, like the original script."""
    out = Path(out_dir)
    hist = out / "historical"
    hist.mkdir(parents=True, exist_ok=True)
    stamp = f"{day or datetime.now(EASTERN).date():%Y-%m-%d}"
    fields = (
        ["rank", "name", "team", "positions", "games", "mpg", "total"]
        + _STAT_COLUMNS
        + [f"z_{c}" for c in CATEGORIES.values()]
        + ["hashtag_id"]
    )
    written = []
    for w, lines in rankings.windows.items():
        rows = [
            {
                "rank": l.rank, "name": l.name, "team": l.team, "positions": "/".join(l.positions),
                "games": l.games, "mpg": l.mpg, "total": l.total, "hashtag_id": l.hashtag_id,
                **{c: l.stats.get(c, "") for c in _STAT_COLUMNS},
                **{f"z_{c}": l.zscores.get(c, "") for c in CATEGORIES.values()},
            }
            for l in lines
        ]
        for path in (out / f"rankings_{w}.csv", hist / f"rankings_{w}_{stamp}.csv"):
            with path.open("w", newline="", encoding="utf8") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            written.append(path)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape Hashtag Basketball 9-cat rankings")
    ap.add_argument("--windows", nargs="+", default=list(DEFAULT_WINDOWS), choices=list(WINDOWS))
    ap.add_argument("--out", help="write CSVs to this directory")
    ap.add_argument("--top", type=int, default=15, help="rows to print per window")
    args = ap.parse_args()

    rankings = fetch_rankings(args.windows)
    print(f"Hashtag Basketball rankings (page updated {rankings.updated or 'unknown'})")
    for w, lines in rankings.windows.items():
        print(f"\n{w}: {len(lines)} players")
        for l in lines[: args.top]:
            print(f"  {l.rank:>3}. {l.name:<26} {l.team:<4} {'/'.join(l.positions):<6} "
                  f"{l.games:>2} gp {l.mpg:>4.1f} mpg  total {l.total:+.2f}")
    if args.out:
        for path in write_csv(rankings, args.out):
            print(f"wrote {path}")


if __name__ == "__main__":
    main()
