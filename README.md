# ESPN Fantasy Basketball Scout

A daily bot built on [cwendt94/espn-api](https://github.com/cwendt94/espn-api). Each run it:

1. **Reads the injury report** for your roster and every NBA player in the league's pool.
2. **Scouts the depth chart.** When a starter is out, it pulls game logs for the free agent teammates who play his position and splits their games into with him and without him, so you see what each backup has actually done in the bigger role. If the injury is too fresh for that, it estimates the bump instead.
3. **Sets today's lineup.** It works out the best lineup for today's games that haven't tipped off and lists the changes from the one set in ESPN. Players whose game has started are locked, and it tries to keep everyone in his current slot. It also warns when a healthy player is still on IR, since ESPN then blocks adds and drops.
4. **Suggests add/drops from your whole roster.** Your weakest couple of players are streaming spots that rotate whenever someone should score more this week. Everyone else is core and only moves for a clear, consistent upgrade. After the moves it would make, it lists the next best swaps (10 in all), so you have options.
5. **Plays the matchup.** It reads the live score, both opponents' rosters and your adds left, and projects your chance of winning this week and next. Streaming moves are judged by how much they raise those chances.

espn-api is read only, so the bot recommends and you make the moves in the ESPN app.

## How players are valued

```
expected value = sum over games in the next N days of
                 availability x (per game value + depth bump)
```

- **Per game value** blends last 7 days, last 15 days, season average, ESPN's projection and, in points leagues, Hashtag Basketball's per game stats scored with your league's points (see below). Weights live in `bot.toml`. The projection counts as much as 5 real games, so its weight fades as a player's games pile up and a preseason guess can't hold down a breakout.
- **Availability** is 0 while a player is out (until his expected return date), discounted when day-to-day, else 1. Return dates come from ESPN fantasy when it has one, otherwise from ESPN's free [injury report](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries), which also supplies the note shown next to each injured player. If that feed is down the run carries on without it.
- **Depth bump** is `injured player's fpts x absorb_rate x position overlap x depth share`, applied only on days the injured player misses. It's an estimate, used for the next few days' value and for injuries too fresh to have game logs. Players out all season are skipped, since their backups' stats already reflect the bigger role.
- Points leagues use ESPN's own fantasy point averages. Category leagues fall back to standard ESPN points as a ranking proxy.
- Two pickups are never stacked on the same injury, since both lose value when that starter returns.

## Hashtag Basketball rankings

Each run also scrapes the free [9-cat rankings](https://hashtagbasketball.com/fantasy-basketball-rankings) from Hashtag Basketball for the season and the last 7 and 14 days. It covers every player who has played, not just the top 200, and lists ESPN position eligibility. Each player's line (per game stats, a z-score per category, and Hashtag's overall value) is attached by name to the ESPN player as `PlayerView.hashtag[window]`, so the scoring and reports can use it. Hashtag's rank weighs all nine categories equally, which doesn't match a points league, so in points leagues the bot scores Hashtag's per game stats with your league's own points per stat (read from the ESPN league settings), ranks every player again on those points, and shows both next to each move and each of your weakest players. A player's Hashtag rank in your scoring can be far from his 9-cat rank: a high volume, low efficiency scorer ranks much better in points.

In points leagues that line is also part of each player's per game value, weighted by `hashtag` under `[weights]`, from the last 14 days (or the next window you scrape). Hashtag updates once a day and can miss last night's late games. When its season line has fewer games than ESPN has counted, it's left out of the blend for that player, so a big night doesn't count against him, and the report notes the missing game. The scraper is adapted from [cdchan/fantasy-basketball](https://github.com/cdchan/fantasy-basketball/blob/master/scrape_hashtagbasketball.py), whose projections page now shows free users only the top 30.

Settings live under `[hashtag]` in `bot.toml`. A new session gets the site's default table, not the settings saved in your browser, so the scrape sets them on every request and checks the page applied them: per game averages, H2H, and the category `multipliers` (STL and BLK x1.25, TO x0.25 by default). The multipliers change Hashtag's TOTAL and rank, which go into the CSVs and category league reports. The per game stats and z-scores the bot values players by stay the same. If the scrape fails the run logs a warning and carries on without it. To look at the rankings on their own or save CSVs:

```bash
python -m fantasy_bot.hashtag --windows last_7 last_14 --out data/hashtag
```

If a player won't match because the two sites spell his name differently, add the pair to `ALIASES` in `fantasy_bot/hashtag.py`.

## How moves are picked

Anyone on your roster can be suggested as a drop, except the names in `untouchable` in `bot.toml`. Your `streaming_spots` weakest players by per game value (2 by default) are **streaming spots**. Everyone else is **core**. Nothing to maintain: a pickup who breaks out climbs out of the bottom and becomes core on his own.

Each free agent gets a per game value for the role he has right now:

- **Backup to an injured starter**, with at least `min_games_without` games played while that starter sat: his actual average in those games, but only until the starter is due back. After that he counts at his usual value, so a role that ends in two days isn't valued as if it lasts all week.
- **Anyone else:** his blended per game value.

Your own players get the same role based value, so a pickup isn't valued one way before you add him and another way after.

Moves are valued by your **best lineup each day** over the days a move made today would play in (from tomorrow). The bot reads your league's starting slots (PG, SG, SF, PF, C, G, F, 3 UT, for example) and, for each day, fills them with the highest scoring set of players who play that day. A pickup's games only count when he'd start over someone you already have, and a drop only costs the games he'd have started. On a night when 12 of your players have games, the extra games of a busy schedule are worth nothing.

Each free agent is tried against your roster:

- **Streaming spot:** he replaces whichever streaming spot raises your win chances the most (this week plus next, see below), if that's at least `min_win_gain`. Without a matchup to look at, it's fpts over the next `horizon_days` instead, with a bar of `stream_min_gain`.
- **Core player**, only if no streaming spot works: he has to out-score the player by at least `min_edge` fpts per game, judged per game and not on this week, so an injured regular is never dropped for being worth 0 right now. For an injury pickup, his median game without the starter also has to beat the player's median game, so one huge night can't carry the average.

The bot takes the swap that helps most, then looks again with that move made, up to `max_moves` (never more than the adds you have left). After its picks, the report lists the next best swaps up to `options_shown` in all, each with the drop that suits it best and judged on its own against your roster as it is. Those are alternatives, not extra moves: several usually share the same drop. They're listed even when they don't clear the bar, as long as they'd add something.

When nobody clears either bar the report says to hold, and still lists the closest options. The **depth watch** shows the roles that are growing but not there yet: too few games, a smaller edge, or a fresh injury with only an estimate. That's where tomorrow's pickups usually come from.

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

## Trade scout

`python -m fantasy_bot --trades` suggests trades with the other teams: sell players running hot, buy players off to a slow start. Every player gets two per game values:

- **Now** is the daily scout's value. It leans on the last week or two, which is also what other managers go by.
- **Rest of season** is the same blend with the weights under `[trade.weights]`, which lean on the season average and ESPN's projection. The projection counts as `projection_games` real games (20 by default), so a hot or cold couple of weeks is pulled back toward what the player was expected to do. In points leagues, Hashtag's season line scored with your league's points is part of it too.

A player whose now value is above his rest of season value is a **sell high**. One below it is a **buy low**.

Every trade of up to `max_players` a side (1-for-1 up to 2-for-2) with every other team is scored the way the daily scout scores moves: by your best lineup each day, here from tomorrow to the end of the season. Your side uses rest of season values. Their side blends now and rest of season values (`their_recent`, half and half by default): managers go by recent form, but they plan for the long run too. When a side ends up with an extra player it drops its weakest per game (never an untouchable). When it has an open spot, it adds the best healthy free agent. Either side also cuts a player it just got for a free agent, but only one who'd score at least 25% more over the rest of the season: nobody cuts a player they just traded for, out a few days or in a slump, for a waiver pickup who's about as good. Free agents are valued on rest of season value on both sides, since a big week off waivers makes a streamer, not a long-term piece, and the one picked is whoever would score the most over the rest of the season, games left included, so a free agent without an NBA team is never picked. A free agent one side picks up isn't there for the other. A trade is listed when it adds `min_gain` fpts a week for you, looks no worse than `fairness` fpts a week to them (even or better by default), and the best player in the deal on current form goes to them. Two good players for one great one can add up on paper, but managers rarely give up the best player in a deal. The best 5 are shown, best for you first. Each is its own idea: no player you'd get shows up twice, but the same player of yours can go to different teams as alternatives.

Each trade lists every player's splits (last 7, last 15, season, ESPN projection), both values, injury status, Hashtag's season line as a second opinion, and who each side would drop or pick up. Under the trades, the report lists your biggest sell high candidates and the biggest buy low targets on other teams who'd start for you.

### Build trades around a player

To shop a player, or go after one, name them: `python -m fantasy_bot --trades --with "Brandon Miller"`. In the app, pick them under *Build around* on *Find trades*.

- **Your players:** trades with every other team that send at least one of them.
- **Their players:** trades that get you at least one of them, searched only on the teams that have them.
- **Both:** each trade sends at least one of yours and gets at least one of theirs.

The players you pick can be packaged with any others you'd trade (untouchables only when you pick them), in any shape up to 4 players in all: 1-for-1, 2-for-1, 1-for-2, 2-for-2, 3-for-1 and 1-for-3. Every deal is scored like the rest, and only ones that add `min_gain`+ fpts a week for you and look fair enough to them are listed: the best 8, the best of each shape first, at most 2 with any one team, and no two asking for the same players. *Check this one* opens a trade in *Check a trade* for the full breakdown and alternatives.

Other teams' rosters only come with season totals, so the trade scout fetches every rostered player's card for the last 7 and 15 day splits (a few extra requests). `trade-scout.yml` posts it to Discord every Monday. Like everything else, it only recommends: propose the trade in the ESPN app.

### Check a trade

To score a trade you have in mind, or one someone offered you, name both sides:

```bash
python -m fantasy_bot --give "Jokic, Tyler Herro" --get "Bam Adebayo" --no-post
```

Any number of players a side, so 3-for-1 works: the side that ends up with more players drops its weakest, and the side with fewer picks up the best healthy free agents. Names are forgiving: case, accents, punctuation and Jr./III don't matter, a last name alone works when only one player has it, and a small typo is fixed (the report says how it read the name). A name that could be several players, a player on the wrong side, a free agent, or players from more than one team is an error that says what to fix.

The trade is scored exactly like the trade scout's: your best lineup each day over the rest of the season on rest of season values. The verdict is **Take it** when it adds `min_gain`+ fpts a week for you, **Pass** when it costs that much, and **About even** in between. It also says whether they'd likely accept (it looks no worse than `fairness` to them, valued the same blended way), what it really does for them on rest of season values, and warns when you'd send an untouchable or ask for the best player in the deal.

Then it lists up to 5 **other trades with the same team** built around the players you chose to send, so a bad offer turns into one that works: a 1-for-1, a 2-for-2, a package (2 or 3 of yours, your chosen player plus others, for 1 of theirs), or a spread (your player for 2 or 3 of theirs), up to 4 players in all. Each one must add `min_gain`+ fpts a week for you and look fair enough to them, scored the same way as above. The best of each shape comes first, then the best of the rest, and no two ask for the same players. Untouchables are only packaged in when you chose to send them. In the app, *Check this one* loads an alternative into the pickers.

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
3. Two workflows run on their own. You can also trigger either by hand from the Actions tab.
   - `daily-scout.yml` posts the full report at 18:30 UTC (7:30am NZ time, early afternoon US Eastern before tip-off).
   - `trade-scout.yml` posts trade ideas on Mondays at 18:45 UTC. It keeps no state.
   - `lineup-check.yml` runs every hour from noon to 11pm US Eastern and posts only when your lineup needs a change it hasn't already told you about today. Late scratches and rest days usually show up about an hour before tip-off, after the daily report has gone out.

Both keep their state on a `bot-data` branch, which the first run creates: which alerts went out today, and (see below) the run history. That's about 12 short runs a day, roughly 400 Actions minutes a month: free on a public repo, and inside the 2,000 free minutes on a private one. GitHub turns off scheduled workflows in a public repo after 60 days with no activity.

To try the lineup check locally: `python -m fantasy_bot --lineup-only --no-post`.

To get a Discord post: Server Settings > Integrations > Webhooks > New Webhook, then copy the URL.

## Phone app (Streamlit)

`streamlit_app.py` shows the same reports as a web app built for phones: **Today** (the matchup, today's lineup, injuries and your roster's per game values), **Pickups**, **Trades** (*Find trades* or *Check a trade*, loaded when you tap a button, since that reads every roster) and **Settings**, where you can try other untouchables, move limits and trade bars without editing `bot.toml`. Those changes last until the page reloads.

Each read from ESPN is reused for 15 minutes. *Refresh* reads it again. Run history isn't shown, since it lives on the `bot-data` branch.

To try it locally (it reads `bot.toml` and `secrets.toml` like the bot):

```bash
streamlit run streamlit_app.py
```

To deploy it on [Streamlit Community Cloud](https://share.streamlit.io) for free:

1. Push the repo to GitHub, then on share.streamlit.io choose **Create app**, pick the repo, branch `main` and main file `streamlit_app.py`.
2. Under **Advanced settings**, choose Python 3.12, and paste the contents of your `secrets.toml` into **Secrets**. Add a password too, since anyone with the link would otherwise see your league:

   ```toml
   password = "pick-something"

   [league]
   espn_s2 = "..."
   swid = "{...}"
   ```

   The same `[league]` table can also override `league_id`, `year` and `team_id` from `bot.toml`.
3. Deploy. On your phone, open the link and use **Add to Home Screen** so it opens like an app.

When the ESPN cookies expire, the app says so: paste the new ones under the app's **Settings > Secrets**. When nobody has opened it for a while the app goes to sleep, and the next visit takes a minute to wake it up.

### On Render

`render.yaml` is a [Render Blueprint](https://render.com/docs/blueprint-spec) for the app, on the free plan:

1. Push the repo to GitHub. On [render.com](https://render.com), choose **New > Blueprint** and pick the repo.
2. Render asks for three values: `ESPN_S2` and `ESPN_SWID` (the cookies from `secrets.toml`) and `APP_PASSWORD`, since anyone with the link would otherwise see your league.
3. Apply. The app is at the `onrender.com` link Render shows, and redeploys on every push to `main`.

League, year and team come from `bot.toml`. To use others, add `ESPN_LEAGUE_ID`, `ESPN_YEAR` or `ESPN_TEAM_ID` under the service's **Environment**, which is also where new cookies go when the old ones expire. A free service sleeps after 15 minutes without visits, and the next visit takes about a minute to wake it up.

## Track record

Each daily report is logged to `runs.jsonl` on the `bot-data` branch: the matchup, both win chances, every suggested move and your roster's per game estimates. The first report of each matchup checks the one before it and adds a **Last week** section:

- the result, next to the win chance the first report that week gave you;
- how many suggested pickups you made, and how many fpts those adds scored over their drops in the week after;
- how far off the per game estimates for your players were, and in which direction.

For the whole season so far, with hints for `projection_error` and the weights once there are a few weeks of data:

```bash
git fetch origin bot-data && git worktree add data origin/bot-data
python -m fantasy_bot.review --data-dir data
```

Local runs only log when given `--data-dir`.

## Tuning

| Setting | What it does |
| --- | --- |
| `horizon_days` | Without matchup data only: days of games counted for streaming, starting tomorrow. |
| `streaming_spots` | How many of your weakest players rotate freely. 0 makes everyone core. |
| `options_shown` | How many swaps the report lists: its own picks, then the next best. |
| `min_win_gain` | Win chance a streaming move must add, this week plus next (0.03 = 3 points). Raise it to churn less. |
| `projection_error` | How far off a per game estimate typically is. Higher means less certain win chances. |
| `stream_min_gain` | Without matchup data only: fpts over the horizon a pickup must beat a streaming spot by. |
| `min_edge` | How many fpts per game a pickup must out-score a core player by. |
| `lineup_min_gain` | Fpts a better lineup must add today before the report says to change yours. Benching a starter who won't score always counts. |
| `min_games_without` | Games a backup must have played without the injured starter before his new role counts. Lower it to jump on injuries earlier, at the cost of smaller samples. |
| `absorb_rate` | How much of an injured star's output flows to backups. 0.5 is a reasonable starting point. |
| `min_minutes_for_depth` | Ignore injuries to players who weren't playing real minutes anyway. |
| `weights` | Lean on `projected` early in the season, then shift to `last_7`/`last_15`. `hashtag` sets how much Hashtag's stats count in points leagues. |
| `[trade]` | `max_players` a side, `min_gain` fpts a week for you, `fairness` fpts a week it may look worse to them, `shown` trades listed. Raise `fairness` to see bolder offers. |
| `[trade.weights]` | The rest of season blend. More `projected` and a higher `projection_games` regress hot and cold starts harder. |

## Tests

```bash
pytest -q
```

Tests run on simulated league data, including a fake espn-api League, so they need no network or credentials.

## Limits worth knowing

- ESPN's injury designations lag official NBA reports by a bit, so check late scratches before lock.
- Game logs show who has stepped up, but the first game or two after an injury is a small sample. The bot doesn't read beat reporter news about who is actually starting.
- The bot assumes an add counts against the matchup you make it in. If ESPN counts it against the matchup it takes effect in, a Sunday add is really one of next week's.
- The trade scout values an injured player without a return date at nothing for the rest of the season, the same way the daily scout treats him as out. Check the news before buying low on one.
- Return dates are estimates. A player still out past his date is treated as having no date at all, not as healthy.
- espn-api is unofficial and ESPN occasionally changes its endpoints. If a run breaks, update with `pip install -U espn-api`.
