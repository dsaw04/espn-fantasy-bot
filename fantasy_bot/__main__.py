"""Run with:  python -m fantasy_bot [--config bot.toml] [--no-post] [--list-teams]"""

from __future__ import annotations

import argparse
import sys

import requests

from . import hashtag
from .config import Config, load_config
from .model import Snapshot
from .opportunity import needs_game_logs
from .recommend import build_report
from .report import post_to_discord, render


def add_hashtag_rankings(snap: Snapshot, cfg: Config) -> None:
    """Scraping is the flakiest step, so a failure here warns instead of killing the run."""
    try:
        rankings = hashtag.fetch_rankings(cfg.hashtag_windows)
    except (requests.RequestException, hashtag.HashtagError) as e:
        print(f"warning: skipped Hashtag Basketball rankings: {e}", file=sys.stderr)
        return
    matched = hashtag.attach(snap.all_players(), rankings)
    fa = sum(1 for p in snap.free_agents if p.hashtag)
    print(f"hashtag: matched {matched} players ({fa} of {len(snap.free_agents)} free agents)", file=sys.stderr)
    if cfg.hashtag_save_dir:
        hashtag.write_csv(rankings, cfg.hashtag_save_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily ESPN fantasy basketball scout")
    ap.add_argument("--config", default="bot.toml")
    ap.add_argument("--secrets", default="secrets.toml")
    ap.add_argument("--no-post", action="store_true", help="print only, skip Discord")
    ap.add_argument("--list-teams", action="store_true", help="print team ids and exit")
    args = ap.parse_args()

    cfg = load_config(args.config, args.secrets)

    from .espn_adapter import EspnSource  # imported late so tests don't need network

    source = EspnSource(cfg)
    if args.list_teams:
        for t in source.league.teams:
            print(f"{t.team_id:>3}  {t.team_name}")
        return

    snap = source.snapshot()
    source.attach_game_logs(needs_game_logs(snap, cfg))
    if cfg.hashtag_enabled:
        add_hashtag_rankings(snap, cfg)

    text = render(build_report(snap, cfg))
    print(text)
    if cfg.discord_webhook and not args.no_post:
        post_to_discord(cfg.discord_webhook, text)


if __name__ == "__main__":
    main()
