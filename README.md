# ESPN Fantasy Basketball Scout

A daily bot built on [cwendt94/espn-api](https://github.com/cwendt94/espn-api). Each run it:

1. **Reads the injury report** for your roster and every NBA player in the league's pool.
2. **Scouts the depth chart.** When a starter is out, it pulls game logs for the free agent teammates who play his position and splits their games into with him and without him, so you see what each backup has actually done in the bigger role. If the injury is too fresh for that, it estimates the bump instead.
3. **Fixes today's lineup.** Starters with no game or who are ruled out get swapped for the best bench player who plays today and fits the slot.
4. **Suggests add/drops from your whole roster.** Your weakest couple of players are streaming spots that rotate whenever someone should score more this week. Everyone else is core and only moves for a clear, consistent upgrade.
5. **Plays the matchup.** It reads the live score, both opponents' rosters and your adds left, and projects your chance of winning this week and next. Streaming moves are judged by how much they raise those chances.

espn-api is read only, so the bot recommends and you make the moves in the ESPN app.

## How players are valued

```
expected value = sum over games in the next N days of
                 availability x (per game value + depth bump)
```

- **Per game value** blends last 7 days, last 15 days, season average and ESPN's projection. Weights live in `bot.toml`. The projection counts as much as 5 real games, so its weight fades as a player's games pile up and a preseason guess can't hold down a breakout.
- **Availability** is 0 while a player is out (until his expected return date), discounted when day-to-day, else 1.
- **Depth bump** is `injured player's fpts x absorb_rate x position overlap x depth share`, applied only on days the injured player misses. It's an estimate, used for the next few days' value and for injuries too fresh to have game logs. Players out all season are skipped, since their backups' stats already reflect the bigger role.
- Points leagues use ESPN's own fantasy point averages. Category leagues fall back to standard ESPN points as a ranking proxy.
- Two pickups are never stacked on the same injury, since both lose value when that starter returns.

## Hashtag Basketball rankings

Each run also scrapes the free [9-cat rankings](https://hashtagbasketball.com/fantasy-basketball-rankings) from Hashtag Basketball for the season and the last 7 and 14 days. It covers every player who has played, not just the top 200, and lists ESPN position eligibility. Each player's line (per game stats, a z-score per category, and Hashtag's overall value) is attached by name to the ESPN player as `PlayerView.hashtag[window]`, so the scoring and reports can use it. Hashtag's rank weighs all nine categories equally, which doesn't match a points league, so in points leagues the bot scores Hashtag's per game stats with your league's own points per stat (read from the ESPN league settings) and shows that next to each move. The scraper is adapted from [cdchan/fantasy-basketball](https://github.com/cdchan/fantasy-basketball/blob/master/scrape_hashtagbasketball.py), whose projections page now shows free users only the top 30.

Settings live under `[hashtag]` in `bot.toml`. If the scrape fails the run logs a warning and carries on without it. To look at the rankings on their own or save CSVs:

```bash
python -m fantasy_bot.hashtag --windows last_7 last_14 --out data/hashtag
```

If a player won't match because the two sites spell his name differently, add the pair to `ALIASES` in `fantasy_bot/hashtag.py`.

## How moves are picked

Anyone on your roster can be suggested as a drop, except the names in `untouchable` in `bot.toml`. Your `streaming_spots` weakest players by per game value (2 by default) are **streaming spots**. Everyone else is **core**. Nothing to maintain: a pickup who breaks out climbs out of the bottom and becomes core on his own.

Each free agent gets a per game value for the role he has right now:

- **Backup to an injured starter**, with at least `min_games_without` games played while that starter sat: his actual average in those games.
- **Anyone else:** his blended per game value.

Your own players get the same role based value, so a pickup isn't valued one way before you add him and another way after.

Moves are valued by your **best lineup each day** over the days a move made today would play in (from tomorrow). The bot reads your league's starting slots (PG, SG, SF, PF, C, G, F, 3 UT, for example) and, for each day, fills them with the highest scoring set of players who play that day. A pickup's games only count when he'd start over someone you already have, and a drop only costs the games he'd have started. On a night when 12 of your players have games, the extra games of a busy schedule are worth nothing.

Free agents are tried best first:

- **Streaming spot:** he replaces whichever streaming spot raises your win chances the most (this week plus next, see below), if that's at least `min_win_gain`. Without a matchup to look at, it's fpts over the next `horizon_days` instead, with a bar of `stream_min_gain`.
- **Core player**, only if no streaming spot works: he has to out-score the player by at least `min_edge` fpts per game, judged per game and not on this week, so an injured regular is never dropped for being worth 0 right now. For an injury pickup, his median game without the starter also has to beat the player's median game, so one huge night can't carry the average.

When nobody clears either bar the report says to hold. The **depth watch** shows the roles that are growing but not there yet: too few games, a smaller edge, or a fresh injury with only an estimate. That's where tomorrow's pickups usually come from.

ESPN applies adds and drops from the next scoring period, so moves are judged against tomorrow's roster (today's adds and drops included) and count games from tomorrow.

### Win chances

ESPN gives the live matchup score; the bot projects the rest the same way for both teams: every player's per game value over the games he has left, with the best lineup each day. Games that have started are already in the live score, so they're skipped. Next week's opponent is projected over all of next week's days.

The spread around each projection has two parts: game to game noise (each start varies by about `cv` x its value, with `cv` measured from your roster's game logs) and error in the estimate itself (`projection_error`, which repeats in every game a player plays). Your chance to win is the normal probability that your final score beats theirs.

So a move is worth what it does to your chances:

- **A decided week** (under 2% or over 98%) can't be swung, so pickups are judged on next week alone.
- **Adds expire** when the matchup ends. ESPN allows 1 per day of the matchup (7 a week, 14 in a two week playoff round). With more adds left than days, the bar halves so they get used; down to your last one early in the week, it doubles. With none left the report says so and suggests nothing.

The report also lists the days your best lineup leaves starting spots empty. Those are the days a streamer scores the most, and the per move values already count them.

Players on waivers only count from the day their claim clears.

ESPN doesn't publish future matchup dates. Matchups follow its Monday to Sunday weeks, with the All-Star break week and the one after it merged, which the bot finds as the longest stretch without games from January on.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp secrets.example.toml secrets.toml   # paste espn_s2 and SWID cookies
# edit bot.toml: league_id, year, untouchable
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
| `horizon_days` | Without matchup data only: days of games counted for streaming, starting tomorrow. |
| `streaming_spots` | How many of your weakest players rotate freely. 0 makes everyone core. |
| `min_win_gain` | Win chance a streaming move must add, this week plus next (0.03 = 3 points). Raise it to churn less. |
| `projection_error` | How far off a per game estimate typically is. Higher means less certain win chances. |
| `stream_min_gain` | Without matchup data only: fpts over the horizon a pickup must beat a streaming spot by. |
| `min_edge` | How many fpts per game a pickup must out-score a core player by. |
| `min_games_without` | Games a backup must have played without the injured starter before his new role counts. Lower it to jump on injuries earlier, at the cost of smaller samples. |
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
- Game logs show who has stepped up, but the first game or two after an injury is a small sample. The bot doesn't read beat reporter news about who is actually starting.
- The bot assumes an add counts against the matchup you make it in. If ESPN counts it against the matchup it takes effect in, a Sunday add is really one of next week's.
- ESPN usually has no return date for injured players, so the bot can't tell a 3-day absence from a 3-week one.
- espn-api is unofficial and ESPN occasionally changes its endpoints. If a run breaks, update with `pip install -U espn-api`.
