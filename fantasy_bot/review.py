"""Season to date: how the bot's forecasts, suggestions and estimates have held up.

The workflows save run history on the bot-data branch. To read it locally:

    git fetch origin bot-data && git worktree add data origin/bot-data
    python -m fantasy_bot.review --data-dir data
"""

from __future__ import annotations

import argparse
from statistics import mean

from .config import load_config
from .history import brier, reviews


def main() -> None:
    ap = argparse.ArgumentParser(description="Season to date review of the bot's predictions")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--config", default="bot.toml")
    ap.add_argument("--secrets", default="secrets.toml")
    args = ap.parse_args()

    rs = reviews(args.data_dir)
    if not rs:
        print(f"No finished matchups reviewed in {args.data_dir} yet. The first report of each matchup adds one.")
        return
    cfg = load_config(args.config, args.secrets)

    print(f"{'wk':>3} {'opponent':<22} {'result':<15} {'forecast':>8} {'made':>6} {'made +/-':>9} {'miss/g':>7} {'bias':>6}")
    for r in rs:
        result = f"{'W' if r.won else 'L'} {r.me:.0f}-{r.them:.0f}"
        forecast = f"{r.predicted * 100:.0f}%" if r.predicted is not None else "-"
        gain = f"{r.made_gain:+.0f}" if r.made_gain is not None else "-"
        miss = f"{r.error:.1f}" if r.error is not None else "-"
        bias = f"{r.bias:+.1f}" if r.bias is not None else "-"
        print(f"{r.period:>3} {r.opp[:22]:<22} {result:<15} {forecast:>8} {r.made:>3}/{r.suggested:<2} {gain:>9} {miss:>7} {bias:>6}")

    wins = sum(r.won for r in rs)
    print(f"\nRecord {wins}-{len(rs) - wins}.")
    b = brier(rs)
    if b is not None:
        print(f"Forecast Brier score {b:.3f} (0 is perfect, 0.25 is always saying 50%).")
        buckets = [("under 40%", 0, 0.4), ("40-60%", 0.4, 0.6), ("over 60%", 0.6, 1.01)]
        for label, lo, hi in buckets:
            inside = [r for r in rs if r.predicted is not None and lo <= r.predicted < hi]
            if inside:
                print(f"  forecast {label}: won {sum(r.won for r in inside)} of {len(inside)}")
    gains = [r.made_gain for r in rs if r.made_gain is not None]
    if gains:
        print(f"Pickups you made out-scored their drops by {sum(gains):+.0f} fpts in total.")
    rel = [r.rel_error for r in rs if r.rel_error is not None]
    if rel:
        typical = mean(rel)
        print(f"Per game estimates miss by {typical * 100:.0f}% on average; projection_error is {cfg.projection_error * 100:.0f}%.")
        if abs(typical - cfg.projection_error) > 0.05 and len(rel) >= 3:
            print(f"  Consider projection_error = {typical:.2f} in bot.toml, so win chances are as sure as the estimates deserve.")
        bias = mean(r.bias for r in rs if r.bias is not None)
        if abs(bias) >= 1.5 and len(rel) >= 3:
            direction = "low" if bias > 0 else "high"
            print(f"  Estimates run {abs(bias):.1f} fpts/g {direction}. Try shifting [weights] toward last_7 and last_15.")


if __name__ == "__main__":
    main()
