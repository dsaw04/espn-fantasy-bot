"""Loads config.toml and lets environment variables override secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Weights:
    last_7: float = 0.35
    last_15: float = 0.20
    season: float = 0.20
    projected: float = 0.25


@dataclass
class Config:
    league_id: int
    year: int
    team_id: int
    espn_s2: Optional[str] = None
    swid: Optional[str] = None

    chopping_block: List[str] = field(default_factory=list)
    # Treat anyone you picked up off waivers or free agency as droppable too.
    # This is what keeps the daily streaming loop going without editing config.
    auto_chop_pickups: bool = True
    # Never suggest dropping these, even if picked up.
    untouchable: List[str] = field(default_factory=list)

    horizon_days: int = 4
    max_moves: int = 2
    min_gain: float = 5.0  # minimum expected fantasy points gained over the horizon
    free_agent_pool: int = 300
    min_minutes_for_depth: float = 18.0  # an injured player must play this much to matter
    absorb_rate: float = 0.5  # share of an injured player's output that flows to backups
    dtd_availability: float = 0.6  # chance a day-to-day player suits up

    weights: Weights = field(default_factory=Weights)
    discord_webhook: Optional[str] = None

    # Hashtag Basketball 9-cat rankings, joined onto every player each run
    hashtag_enabled: bool = True
    hashtag_windows: List[str] = field(default_factory=lambda: ["season", "last_7", "last_14"])
    hashtag_save_dir: Optional[str] = None  # also write the scraped rankings here as CSV


def _read(path: Path) -> Dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_config(path: str | Path = "bot.toml", secrets: str | Path = "secrets.toml") -> Config:
    """bot.toml holds settings and is safe to commit. secrets.toml (gitignored)
    holds cookies and webhook URLs. Env vars override both."""
    raw = _read(Path(path))
    secret = _read(Path(secrets))

    league = {**raw.get("league", {}), **secret.get("league", {})}
    raw.setdefault("bot", {}).update(secret.get("bot", {}))
    bot = raw.get("bot", {})
    w = raw.get("weights", {})
    htb = raw.get("hashtag", {})

    def env(name: str, fallback=None):
        return os.environ.get(name) or fallback

    cfg = Config(
        league_id=int(env("ESPN_LEAGUE_ID", league.get("league_id", 0))),
        year=int(env("ESPN_YEAR", league.get("year", 0))),
        team_id=int(env("ESPN_TEAM_ID", league.get("team_id", 0))),
        espn_s2=env("ESPN_S2", league.get("espn_s2")),
        swid=env("ESPN_SWID", league.get("swid")),
        chopping_block=list(bot.get("chopping_block", [])),
        auto_chop_pickups=bool(bot.get("auto_chop_pickups", True)),
        untouchable=list(bot.get("untouchable", [])),
        horizon_days=int(bot.get("horizon_days", 4)),
        max_moves=int(bot.get("max_moves", 2)),
        min_gain=float(bot.get("min_gain", 5.0)),
        free_agent_pool=int(bot.get("free_agent_pool", 300)),
        min_minutes_for_depth=float(bot.get("min_minutes_for_depth", 18.0)),
        absorb_rate=float(bot.get("absorb_rate", 0.5)),
        dtd_availability=float(bot.get("dtd_availability", 0.6)),
        weights=Weights(**{k: float(v) for k, v in w.items()}),
        discord_webhook=env("DISCORD_WEBHOOK_URL", bot.get("discord_webhook")),
        hashtag_enabled=bool(htb.get("enabled", True)),
        hashtag_windows=list(htb.get("windows", ["season", "last_7", "last_14"])),
        hashtag_save_dir=htb.get("save_dir") or None,
    )
    bad = set(cfg.hashtag_windows) - {"season", "last_7", "last_14", "last_30"}
    if bad:
        raise SystemExit(f"[hashtag] windows has unknown entries {sorted(bad)}.")
    if not (cfg.league_id and cfg.year and cfg.team_id):
        raise SystemExit(
            "league_id, year and team_id are required (config.toml or ESPN_* env vars)."
        )
    return cfg
