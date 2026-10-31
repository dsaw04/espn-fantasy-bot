"""Run with:  python -m fantasy_bot [--config bot.toml] [--no-post] [--list-teams]"""

from __future__ import annotations

import argparse

from .config import load_config
from .recommend import build_report
from .report import post_to_discord, render


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

    text = render(build_report(source.snapshot(), cfg))
    print(text)
    if cfg.discord_webhook and not args.no_post:
        post_to_discord(cfg.discord_webhook, text)


if __name__ == "__main__":
    main()
