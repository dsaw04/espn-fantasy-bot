"""Run with:  python -m fantasy_bot [--config bot.toml] [--no-post] [--list-teams] [--trades]"""

from __future__ import annotations

import argparse
import sys

import requests

from . import hashtag, injuries
from .config import Config, load_config
from .history import Alerts, alert_key, log_run, review_last_matchup, reviews
from .lineup import todays_lineup
from .model import Snapshot
from .opportunity import needs_game_logs
from .recommend import build_report
from .report import lineup_lines, lineup_needs_changes, post_to_discord, render, render_trades
from .scoring import Scorer
from .trade import build_trades


def add_hashtag_rankings(snap: Snapshot, cfg: Config) -> None:
    """Scraping is the flakiest step, so a failure here warns instead of killing the run."""
    try:
        rankings = hashtag.fetch_rankings(cfg.hashtag_windows, cfg.hashtag_multipliers)
    except (requests.RequestException, hashtag.HashtagError) as e:
        print(f"warning: skipped Hashtag Basketball rankings: {e}", file=sys.stderr)
        return
    if snap.scoring:
        hashtag.rank_by_points(rankings, snap.scoring)
    matched = hashtag.attach(snap.all_players(), rankings)
    fa = sum(1 for p in snap.free_agents if p.hashtag)
    print(f"hashtag: matched {matched} players ({fa} of {len(snap.free_agents)} free agents)", file=sys.stderr)
    if cfg.hashtag_save_dir:
        hashtag.write_csv(rankings, cfg.hashtag_save_dir)


def add_injury_news(snap: Snapshot) -> None:
    """Return dates ESPN fantasy is missing. Like the scrape, optional."""
    try:
        news = injuries.fetch()
    except (requests.RequestException, ValueError) as e:
        print(f"warning: skipped ESPN injury report: {e}", file=sys.stderr)
        return
    matched = injuries.attach(snap.all_players(), news, snap.period_dates.get(snap.today))
    print(f"injuries: {len(news)} on ESPN's report, {matched} matched", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily ESPN fantasy basketball scout")
    ap.add_argument("--config", default="bot.toml")
    ap.add_argument("--secrets", default="secrets.toml")
    ap.add_argument("--no-post", action="store_true", help="print only, skip Discord")
    ap.add_argument("--list-teams", action="store_true", help="print team ids and exit")
    ap.add_argument("--lineup-only", action="store_true",
                    help="just check today's lineup, and post only when there's a new change to make")
    ap.add_argument("--trades", action="store_true",
                    help="suggest trades with the other teams instead of the daily report")
    ap.add_argument("--data-dir", help="folder for run history and alert state (the workflows use the bot-data branch)")
    args = ap.parse_args()

    cfg = load_config(args.config, args.secrets)

    from .espn_adapter import EspnSource  # imported late so tests don't need network

    source = EspnSource(cfg)
    if args.list_teams:
        for t in source.league.teams:
            print(f"{t.team_id:>3}  {t.team_name}")
        return

    post = bool(cfg.discord_webhook) and not args.no_post
    if args.lineup_only:
        lineup_check(source, cfg, args.data_dir, post)
        return

    if args.trades:
        trade_scout(source, cfg, post)
        return

    snap = source.snapshot()
    add_injury_news(snap)
    source.attach_game_logs(needs_game_logs(snap, cfg))
    if cfg.hashtag_enabled:
        add_hashtag_rankings(snap, cfg)

    review = review_last_matchup(args.data_dir, snap, source.game_logs)
    report = build_report(snap, cfg)
    if args.data_dir:
        report.review, report.season = review, reviews(args.data_dir)
    text = render(report)
    print(text)
    if post:
        post_to_discord(cfg.discord_webhook, text)
    if args.data_dir:
        log_run(args.data_dir, snap, report)
    if post or args.data_dir:
        # The hourly lineup check shouldn't repeat what this report just said.
        alerts = Alerts(args.data_dir, snap.period_dates.get(snap.today) or snap.now.date())
        if lineup_needs_changes(report.lineup, cfg.lineup_min_gain) or report.lineup.warnings:
            alerts.mark_sent(alert_key(report.lineup))


def trade_scout(source, cfg: Config, post: bool) -> None:
    """Sell players running hot to the other teams for players off to a slow start."""
    snap = source.snapshot()
    source.attach_cards(snap.other_rostered)  # their recent form, not just season totals
    add_injury_news(snap)
    if cfg.hashtag_enabled:
        add_hashtag_rankings(snap, cfg)
    text = render_trades(build_trades(snap, cfg))
    print(text)
    if post:
        post_to_discord(cfg.discord_webhook, text)


def lineup_check(source, cfg: Config, data_dir, post: bool) -> None:
    """Before tip-off: late scratches and rest days show up as OUT about an hour
    before the game, after the daily report has gone out."""
    snap = source.lineup_snapshot()
    plan = todays_lineup(snap, cfg, Scorer(snap, cfg).base)
    if not lineup_needs_changes(plan, cfg.lineup_min_gain) and not plan.warnings:
        print("lineup check: nothing to change")
        return
    alerts = Alerts(data_dir, snap.period_dates.get(snap.today) or snap.now.date())
    key = alert_key(plan)
    if alerts.already_sent(key):
        print("lineup check: already sent this one today")
        return
    stamp = f"{snap.now.hour % 12 or 12}:{snap.now:%M%p} ET" if snap.now else "before tip-off"
    head = [f"**{snap.my_team_name}: lineup check** ({stamp})", ""]
    text = "\n".join(head + lineup_lines(plan, cfg.lineup_min_gain))
    print(text)
    if post:
        post_to_discord(cfg.discord_webhook, text)
    if post or data_dir:
        alerts.mark_sent(key)


if __name__ == "__main__":
    main()
