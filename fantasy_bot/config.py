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

    # Everyone else on the roster can be suggested as a drop.
    untouchable: List[str] = field(default_factory=list)

    horizon_days: int = 7
    max_moves: int = 2
    # Your weakest N players are streaming spots: they rotate whenever someone
    # beats them by stream_min_gain fpts over the horizon. Everyone else is core
    # and only moves for a per game upgrade of min_edge.
    streaming_spots: int = 2
    stream_min_gain: float = 10.0
    min_edge: float = 4.0  # an add must out-score a core player by this many fpts per game
    # With a matchup to look at, streaming moves are judged by the win chance they add
    # this week and next (0.03 = 3 points), instead of stream_min_gain fpts.
    min_win_gain: float = 0.03
    projection_error: float = 0.15  # how far off a per game value typically is, as a share
    min_games_without: int = 3  # games a backup must have played without the injured starter
    free_agent_pool: int = 300
    min_minutes_for_depth: float = 18.0  # an injured player must play this much to matter
    absorb_rate: float = 0.5  # share of an injured player's output that flows to backups
    dtd_availability: float = 0.6  # chance a day-to-day player suits up
    lineup_min_gain: float = 2.0  # fpts a better lineup must add today before the bot says to change it

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
        untouchable=list(bot.get("untouchable", [])),
        horizon_days=int(bot.get("horizon_days", 7)),
        max_moves=int(bot.get("max_moves", 2)),
        streaming_spots=int(bot.get("streaming_spots", 2)),
        stream_min_gain=float(bot.get("stream_min_gain", 10.0)),
        min_edge=float(bot.get("min_edge", 4.0)),
        min_win_gain=float(bot.get("min_win_gain", 0.03)),
        projection_error=float(bot.get("projection_error", 0.15)),
        min_games_without=int(bot.get("min_games_without", 3)),
        free_agent_pool=int(bot.get("free_agent_pool", 300)),
        min_minutes_for_depth=float(bot.get("min_minutes_for_depth", 18.0)),
        absorb_rate=float(bot.get("absorb_rate", 0.5)),
        dtd_availability=float(bot.get("dtd_availability", 0.6)),
        lineup_min_gain=float(bot.get("lineup_min_gain", 2.0)),
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
