# ESPN Fantasy Basketball Scout

A daily bot built on [cwendt94/espn-api](https://github.com/cwendt94/espn-api). Each run it:

1. **Reads the injury report** for your roster and every NBA player in the league's pool.
2. **Scouts the depth chart.** When a starter is out, it finds the healthy teammates who play his position and estimates how much of his production they pick up. The next man up gets the biggest share, deeper reserves get less.
3. **Fixes today's lineup.** Starters with no game or who are ruled out get swapped for the best bench player who plays today and fits the slot.
4. **Suggests add/drops from your chopping block.** It compares every free agent against your late round picks over the next few days and only suggests a move when the gain clears a threshold.

espn-api is read only, so the bot recommends and you make the moves in the ESPN app.

## How players are valued

```
expected value = sum over games in the next N days of
                 availability x (per game value + depth bump)
```

- **Per game value** blends last 7 days, last 15 days, season average and ESPN's projection. Weights live in `bot.toml`.
- **Availability** is 0 while a player is out (until his expected return date), discounted when day-to-day, else 1.
- **Depth bump** is `injured player's fpts x absorb_rate x position overlap x depth share`, applied only on days the injured player misses.
- Points leagues use ESPN's own fantasy point averages. Category leagues fall back to standard ESPN points as a ranking proxy.
- Two pickups are never stacked on the same injury, since both lose value when that starter returns.

## Hashtag Basketball rankings

Each run also scrapes the free [9-cat rankings](https://hashtagbasketball.com/fantasy-basketball-rankings) from Hashtag Basketball for the season and the last 7 and 14 days. It covers every player who has played, not just the top 200, and lists ESPN position eligibility. Each player's line (per game stats, a z-score per category, and Hashtag's overall value) is attached by name to the ESPN player as `PlayerView.hashtag[window]`, so the scoring and reports can use it. The scraper is adapted from [cdchan/fantasy-basketball](https://github.com/cdchan/fantasy-basketball/blob/master/scrape_hashtagbasketball.py), whose projections page now shows free users only the top 30.

Settings live under `[hashtag]` in `bot.toml`. If the scrape fails the run logs a warning and carries on without it. To look at the rankings on their own or save CSVs:

```bash
python -m fantasy_bot.hashtag --windows last_7 last_14 --out data/hashtag
```

If a player won't match because the two sites spell his name differently, add the pair to `ALIASES` in `fantasy_bot/hashtag.py`.

## The chopping block

Put your late round picks in `chopping_block` in `bot.toml`. With `auto_chop_pickups = true` (the default) anyone you pick up from waivers or free agency is also droppable, so the daily streaming loop keeps going without editing the file. Move a pickup into `untouchable` once he earns a permanent spot.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp secrets.example.toml secrets.toml   # paste espn_s2 and SWID cookies
# edit bot.toml: league_id, year, chopping_block
python -m fantasy_bot --list-teams     # find your team_id, put it in bot.toml
python -m fantasy_bot --no-post        # print today's report
```

`year` is the year the NBA season ends, so 2026-27 is `2027`.

## Run it daily for free (GitHub Actions)

1. Push this folder to a **private** GitHub repo.
2. Add repo secrets: `ESPN_S2`, `ESPN_SWID`, and optionally `DISCORD_WEBHOOK_URL`.
3. The workflow in `.github/workflows/daily-scout.yml` runs at 18:30 UTC (7:30am NZ time, early afternoon US Eastern before tip-off). Change the cron to suit. You can also trigger it by hand from the Actions tab.

To get a Discord post: Server Settings > Integrations > Webhooks > New Webhook, then copy the URL.

## Tuning

| Setting | What it does |
| --- | --- |
| `horizon_days` | Days of games counted. Shorter favours whoever plays tonight, longer favours busy schedules. |
| `min_gain` | Raise it if the bot churns too much, lower it to stream harder. |
| `absorb_rate` | How much of an injured star's output flows to backups. 0.5 is a reasonable starting point. |
| `min_minutes_for_depth` | Ignore injuries to players who weren't playing real minutes anyway. |
| `weights` | Lean on `projected` early in the season, then shift to `last_7`/`last_15`. |

## Tests

```bash
pytest -q
```

Tests run on simulated league data, including a fake espn-api League, so they need no network or credentials.

## Limits worth knowing

- ESPN's injury designations lag official NBA reports by a bit, so check late scratches before lock.
- The depth model infers roles from minutes and positions. It does not read beat reporter news about who is actually starting.
- espn-api is unofficial and ESPN occasionally changes its endpoints. If a run breaks, update with `pip install -U espn-api`.
