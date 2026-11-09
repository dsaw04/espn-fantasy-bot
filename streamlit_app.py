"""Phone-first web app for the daily scout and the trade scout.

Run with:  streamlit run streamlit_app.py

Like the bot, it only reads from ESPN: make the moves in the ESPN app.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import replace
from pathlib import Path

import requests
import streamlit as st

from fantasy_bot.__main__ import scout_snapshot, trade_snapshot
from fantasy_bot.config import Config, load_config
from fantasy_bot.model import PlayerView, Snapshot, n_games
from fantasy_bot.recommend import Report, build_report
from fantasy_bot.report import (
    WEAKEST_SHOWN, _hashtag_rank, evaluation_headline, lineup_needs_changes, move_value, no_alternatives, render,
    render_evaluation, render_search, search_none, search_subject,
)
from fantasy_bot.trade import (
    SEARCH_PER_TEAM, Evaluation, TradeInputError, TradeReport, TradeSearch, build_trades, evaluate_trade, form,
    search_trades, shape_label,
)

ROOT = Path(__file__).parent
TTL = 15 * 60  # seconds a read from ESPN is reused before the next one

# Phones get tighter margins, smaller headings and smaller metrics, so two fit side by side.
STYLE = """<style>
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"] { padding: 4rem 0.75rem 4rem; }
  [data-testid="stMetricValue"] { font-size: 1.4rem; }
  h1 { font-size: 1.6rem !important; }
  h3 { font-size: 1.25rem !important; }
}
</style>"""


def md(text: str) -> str:
    """Streamlit's markdown strikes through text between single tildes, and the bot writes ~ for "about"."""
    return text.replace("~", r"\~")


def pos(p: PlayerView) -> str:
    return "/".join(sorted(p.positions)) or "?"


def who(p: PlayerView) -> str:
    return f"{p.pro_team} {pos(p)}"


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def slot(s: str) -> str:
    return "bench" if s == "BE" else s


def status(p: PlayerView) -> str:
    label = p.status_label()
    if p.is_out:
        return f":red-badge[{label}]"
    if p.is_risky:
        return f":orange-badge[{label}]"
    return label


# ---- setup -----------------------------------------------------------------------


def secrets() -> dict:
    """Streamlit's secrets: App settings > Secrets on Streamlit Cloud, .streamlit/secrets.toml locally."""
    try:
        return st.secrets.to_dict()
    except FileNotFoundError:
        return {}


def config(sec: dict) -> Config:
    """bot.toml and secrets.toml as the CLI reads them, with the [league] table of Streamlit's
    secrets on top, so secrets.toml can be pasted into Streamlit Cloud as it is."""
    cfg = load_config(ROOT / "bot.toml", ROOT / "secrets.toml")
    league = sec.get("league", {})
    return replace(
        cfg,
        league_id=int(league.get("league_id", cfg.league_id)),
        year=int(league.get("year", cfg.year)),
        team_id=int(league.get("team_id", cfg.team_id)),
        espn_s2=league.get("espn_s2") or cfg.espn_s2,
        swid=league.get("swid") or cfg.swid,
    )


def unlocked(password: str) -> bool:
    """Anyone with the link sees your league, so an optional password keeps it to you."""
    if not password or st.session_state.get("unlocked"):
        return True
    st.title("🏀 Fantasy scout", anchor=False)
    entered = st.text_input("Password", type="password")
    if entered and hmac.compare_digest(entered.encode(), password.encode()):
        st.session_state.unlocked = True
        st.rerun()
    if entered:
        st.error("Wrong password.")
    return False


# ---- data ------------------------------------------------------------------------
# A read from ESPN is reused for TTL seconds by everyone, and each report is cached by the
# settings it was built with. Arguments starting with _ aren't part of the cache key.


@st.cache_data(ttl=TTL, show_spinner="Reading your league from ESPN, about 20 seconds...")
def scout_data(_cfg: Config, league: tuple) -> Snapshot:
    from fantasy_bot.espn_adapter import EspnSource

    return scout_snapshot(EspnSource(_cfg), _cfg)


@st.cache_data(ttl=TTL, show_spinner="Reading every team's roster from ESPN, about 20 seconds...")
def trade_data(_cfg: Config, league: tuple) -> Snapshot:
    from fantasy_bot.espn_adapter import EspnSource

    return trade_snapshot(EspnSource(_cfg), _cfg)


@st.cache_data(ttl=TTL, show_spinner=False)
def daily_report(_snap: Snapshot, _cfg: Config, taken: str, settings: tuple) -> Report:
    return build_report(_snap, _cfg)


@st.cache_data(ttl=TTL, show_spinner="Scoring trades...")
def trade_report(_snap: Snapshot, _cfg: Config, taken: str, settings: tuple) -> TradeReport:
    return build_trades(_snap, _cfg)


@st.cache_data(ttl=TTL, show_spinner="Searching every team for trades around them...")
def search(_snap: Snapshot, _cfg: Config, taken: str, settings: tuple, include: tuple) -> TradeSearch:
    return search_trades(_snap, _cfg, include)


@st.cache_data(ttl=TTL, show_spinner="Scoring the trade and looking for better ones with the same team...")
def evaluation(_snap: Snapshot, _cfg: Config, taken: str, settings: tuple, give: tuple, get: tuple) -> Evaluation:
    return evaluate_trade(_snap, _cfg, give, get)


def fetch(load, cfg: Config) -> Snapshot:
    try:
        return load(cfg, (cfg.league_id, cfg.year, cfg.team_id))
    except SystemExit as e:  # the adapter's explanation of what's wrong, e.g. expired cookies
        st.error(str(e))
        st.caption("Deployed, the cookies go in the ESPN_S2 and ESPN_SWID environment variables, "
                   "or the [league] table of Streamlit's secrets.")
    except requests.RequestException as e:
        st.error(f"Couldn't reach ESPN: {e}")
    st.stop()


def pickup_key(cfg: Config) -> tuple:
    return tuple(cfg.untouchable), cfg.max_moves, cfg.streaming_spots, cfg.min_win_gain, cfg.min_edge


def trade_key(cfg: Config) -> tuple:
    return tuple(cfg.untouchable), cfg.trade_max_players, cfg.trade_min_gain, cfg.trade_fairness, cfg.trade_their_recent


THEM_HELP = ("How it looks to them: their players on a blend of recent form and rest of season (see Settings), "
             "free agents on rest of season")


# ---- tabs ------------------------------------------------------------------------


def header(snap: Snapshot) -> None:
    st.title(f"🏀 {snap.my_team_name.strip()}", anchor=False)
    row = st.container(horizontal=True, vertical_alignment="center")
    day = snap.period_dates.get(snap.today)
    when = f"{day:%a %b} {day.day} · " if day else ""
    row.caption(f"{when}updated {snap.now.hour % 12 or 12}:{snap.now:%M %p} ET")
    if row.button("Refresh", icon=":material/refresh:", width="content"):
        scout_data.clear()
        trade_data.clear()
        st.rerun()


def settings(cfg: Config, snap: Snapshot) -> Config:
    names = sorted({p.name for p in snap.my_roster + (snap.next_roster or [])} | set(cfg.untouchable))
    sides = sorted({1, 2, cfg.trade_max_players})
    with st.form("settings", border=False):
        st.subheader("Pickups", anchor=False)
        untouchable = st.multiselect("Never drop", names, default=cfg.untouchable)
        max_moves = st.number_input("Most moves a day", 0, 7, cfg.max_moves)
        streaming = st.number_input(
            "Streaming spots", 0, 6, cfg.streaming_spots,
            help="Your weakest N players rotate freely. Everyone else only moves for a clear upgrade.",
        )
        win_gain = st.slider("Win chance a streaming move must add (points)", 0.0, 10.0, cfg.min_win_gain * 100, 0.5, format="%.1f")
        min_edge = st.number_input("Fpts/g a pickup must beat a core player by", 0.0, 20.0, cfg.min_edge, 0.5)
        st.subheader("Trades", anchor=False)
        max_players = st.radio("Most players a side", sides, index=sides.index(cfg.trade_max_players), horizontal=True)
        min_gain = st.number_input("Fpts/week a trade must add for you", 0.0, 50.0, cfg.trade_min_gain, 0.5)
        fairness = st.number_input(
            "Fpts/week it may look worse to them", 0.0, 50.0, cfg.trade_fairness, 0.5,
            help="Raise it to see bolder offers.",
        )
        their_recent = st.slider(
            "How much other managers go by recent form (%)", 0, 100, round(cfg.trade_their_recent * 100), 10,
            help="The rest is rest of season value. 100 judges trades for them on the last week or two only.",
        )
        st.form_submit_button("Apply", type="primary", width="stretch")
    st.caption("These last until you reload the page. The defaults live in bot.toml.")
    return replace(
        cfg, untouchable=untouchable, max_moves=int(max_moves), streaming_spots=int(streaming),
        min_win_gain=win_gain / 100, min_edge=min_edge,
        trade_max_players=max_players, trade_min_gain=min_gain, trade_fairness=fairness,
        trade_their_recent=their_recent / 100,
    )


def matchup(r: Report) -> None:
    m, o = r.matchup, r.outlook
    st.subheader(f"This week vs {m.opp.name}", anchor=False)
    row = st.container(horizontal=True)
    row.metric("Score", f"{m.me.score:.0f}–{m.opp.score:.0f}", border=True)
    row.metric("Win chance", pct(o.now.win), border=True)
    days_left = sum(1 for sp in m.days if sp >= r.today)
    adds = "no add limit" if m.adds_left is None else f"{m.adds_left} of {m.adds_limit} adds left"
    st.caption(f"Projected {o.now.mine:.0f}–{o.now.theirs:.0f} · {days_left} {'day' if days_left == 1 else 'days'} "
               f"left including today · {adds}")
    if o.next is not None:
        st.markdown(f"**Next week vs {m.next_opp.name}**: projected {o.next.mine:.0f}–{o.next.theirs:.0f}, "
                    f"you win {pct(o.next.win)} of the time.")
    if o.now.decided and o.next is not None:
        st.info("This week is all but decided, so pickups are judged on next week.")


def lineup(r: Report) -> None:
    plan = r.lineup
    st.subheader("Lineup today", anchor=False)
    warnings = [f"- {md(w)}" for w in plan.warnings]
    if lineup_needs_changes(plan, r.lineup_min_gain):
        changes = [f"- **{c.player.name}**: {slot(c.old)} → {slot(c.new)}" + (f" ({c.reason})" if c.reason else "")
                   for c in plan.changes]
        st.warning("\n".join([f"**Set your lineup** ({md(f'~{plan.gain:+.0f}')} fpts)", ""] + changes + warnings))
    elif plan.idle:
        idle = "; ".join(f"{p.name}: {why}" for p, why in plan.idle)
        spots = f"{plan.empty} starting {'spot' if plan.empty == 1 else 'spots'}"
        st.info("\n".join([f"{spots} will score nothing ({md(idle)}), and nobody on your bench plays.", ""] + warnings))
    elif warnings:
        st.warning("\n".join(warnings))
    else:
        st.success("Set. No changes needed.")
    if r.open_spots:
        days = ", ".join(f"{d:%a} {d.day}: {n}" for d, n in r.open_spots)
        st.caption(f"Empty starting spots, where streamers score most: {days}")


def today(r: Report, snap: Snapshot) -> None:
    if r.matchup and r.outlook:
        matchup(r)
    lineup(r)

    st.subheader("Injuries", anchor=False)
    if not r.injured_on_roster:
        st.caption("Everyone is healthy.")
    for p in r.injured_on_roster:
        st.markdown(f"**{p.name}** · {who(p)} · {status(p)}")
        if p.injury_note:
            st.caption(md(p.injury_note))

    st.subheader("Roster", anchor=False)
    rows = [{"Player": p.name, "Slot": slot(p.lineup_slot), "Fpts/g": r.per_game.get(p.id),
             "Status": "" if p.injury_status in ("", "ACTIVE") else p.status_label()}
            for p in snap.my_roster]
    rows.sort(key=lambda x: x["Fpts/g"] or 0.0, reverse=True)
    st.dataframe(rows, hide_index=True, width="stretch", column_config={
        "Fpts/g": st.column_config.NumberColumn(format="%.1f", help="Per game value in his current role"),
    })

    with st.expander("Text report"):
        st.code(render(r), language=None, wrap_lines=True)


def move_card(i: int, m, r: Report, expanded: bool) -> None:
    a, d = m.add.player, m.drop.player
    with st.container(border=True):
        st.markdown(f"**{i}. Add {a.name}** · {who(a)} · {a.percent_owned:.0f}% owned  \n"
                    f"Drop **{d.name}**{' (streaming spot)' if m.kind == 'stream' else ''}")
        st.markdown(md(move_value(m, r)))
        with st.expander("Why", expanded=expanded):
            st.markdown("\n".join(f"- {md(x)}" for x in m.reasons))


def pickups(r: Report) -> None:
    st.subheader("Pickups", anchor=False)
    if r.moves_start_tomorrow:
        st.caption("Adds play from tomorrow.")
    for i, m in enumerate(r.moves, 1):
        move_card(i, m, r, expanded=True)
    if not r.moves:
        hold(r)
    if r.options:
        st.markdown("**Other options**" if r.moves else "**Closest options**")
        st.caption("Each judged on its own against your roster as it is.")
        for i, m in enumerate(r.options, len(r.moves) + 1):
            move_card(i, m, r, expanded=False)
    weakest(r)
    depth_watch(r)


def hold(r: Report) -> None:
    if r.matchup and r.matchup.adds_left == 0:
        st.info(f"Out of adds: all {r.matchup.adds_limit} used this matchup. They reset when the next one starts.")
    elif r.roster:
        names = " and ".join(v.player.name for v in r.roster if v.player.id in r.streamers)
        if r.matchup:
            bar = f"adds {r.win_bar * 100:.1f}+ points to your win chances this week and next"
        else:
            bar = f"beats them by {r.stream_min_gain:g}+ fpts over the next {r.horizon_days} days"
        st.info(f"**Hold.** No pickup over your streaming spots ({names}) {bar}, and nobody beats "
                f"a core player by a consistent {r.min_edge:g}+ fpts/g.")
    else:
        st.info("Everyone on your roster is untouchable, so there is nobody to drop.")


def weakest(r: Report) -> None:
    if r.roster:
        st.subheader("Your weakest players", anchor=False)
        st.caption("Per game, untouchables left out.")
        for v in r.roster[:WEAKEST_SHOWN]:
            p = v.player
            if r.week is not None:
                plays = f"starts {r.week.starts.get(p.id, 0)} of his {n_games(r.week.games.get(p.id, 0))}"
            else:
                plays = n_games(v.games)
            spot = " · streaming spot" if p.id in r.streamers else ""
            st.markdown(f"**{p.name}**{spot} · {status(p)}  \n"
                        + md(f"~{r.per_game.get(p.id, v.base):.1f} fpts/g{_hashtag_rank(p)}, "
                             f"{plays} in the next {r.horizon_days} days"))


def depth_watch(r: Report) -> None:
    if r.watchlist:
        weakest = r.roster[0].player.name if r.roster else "your weakest player"
        st.subheader("Depth watch", anchor=False)
        st.caption("Roles opened by injuries. Tomorrow's pickups usually come from here.")
        for w in r.watchlist:
            p = w.player
            if w.games_without:
                basis = (f"{w.per_game:.1f} fpts/g in {n_games(w.games_without)} without {w.injured.name}, "
                         f"{w.edge:+.1f}/g vs {weakest}, median game {w.median:.0f}")
            else:
                basis = (f"est. ~{w.per_game:.1f} fpts/g while {w.injured.name} is out (no games without him yet), "
                         f"{w.edge:+.1f}/g vs {weakest}")
            st.markdown(f"**{p.name}** · {who(p)} · {p.percent_owned:.0f}% owned  \n{md(basis)}")


def trade_card(i: int, t, title: str = ""):
    """One trade. Returns the card, so a caller can add to it."""
    card = st.container(border=True)
    with card:
        give = " and ".join(v.player.name for v in t.give)
        get = " and ".join(v.player.name for v in t.get)
        st.markdown(f"**{i}. {title or f'Trade with {t.team_name}'}**  \nSend **{give}**  \nGet **{get}**")
        row = st.container(horizontal=True)
        row.metric("For you", f"{t.gain:+.1f}/wk", border=True)
        row.metric("To them", f"{t.their_gain:+.1f}/wk", border=True, help=THEM_HELP)
        st.caption(f"{t.total:+.0f} fpts for you over the rest of the season")
        with st.expander("Details"):
            st.markdown("\n".join(f"- {md(x)}" for x in t.reasons))
    return card


def trades(cfg: Config, scout: Snapshot) -> None:
    st.caption("*Now* is recent form. *Rest of season* leans on the season and ESPN's projection. Trades are "
               "scored with your best lineup each day: rest of season for you, a blend of both for them. Free "
               "agents count at rest of season value, since a hot week off waivers is a streamer.")
    st.session_state.setdefault("trade_mode", "Find trades")  # here, not default=, so Check this one can set it
    mode = st.segmented_control("Trades", ["Find trades", "Check a trade"], key="trade_mode",
                                label_visibility="collapsed")
    if mode == "Check a trade":
        check_trade(cfg, scout)
    else:
        find_trades(cfg, scout)


VERDICT_BOX = {"good": st.success, "even": st.info, "bad": st.error}


def player_label(p: PlayerView) -> str:
    return f"{p.name} · {who(p)}"


def check_trade(cfg: Config, scout: Snapshot) -> None:
    """Pick both sides from the rosters, so every name is a real player on the right team.
    Any number a side: a 3-for-1 makes room by dropping your weakest or adding free agents."""
    teams = {}
    for p in scout.other_rostered:
        teams.setdefault(p.team_id, []).append(p)
    if not teams:
        st.info("No other teams found in the league.")
        return
    mine = sorted(scout.roster_for_moves(), key=lambda p: p.name)
    by_id = {p.id: p for p in mine + scout.other_rostered}
    tids = sorted(teams, key=lambda t: scout.team_names.get(t, ""))
    with st.container(border=True):
        if st.session_state.get("check_team") not in tids:
            st.session_state.check_team = tids[0]
        tid = st.selectbox("Trade with", tids, format_func=lambda t: scout.team_names.get(t, f"team {t}"),
                           key="check_team")
        give = st.multiselect("You send", [p.id for p in mine], format_func=lambda i: player_label(by_id[i]),
                              placeholder="Type a name", key="check_give")
        get = st.multiselect("You get", [p.id for p in sorted(teams[tid], key=lambda p: p.name)],
                             format_func=lambda i: player_label(by_id[i]), placeholder="Type a name",
                             key=f"check_get_{tid}")
    if not give or not get:
        st.caption("Pick at least one player a side. Uneven trades are fine: the side with more players "
                   "drops its weakest, the side with fewer picks up the best free agents.")
        return
    # The first check reads every roster's recent form; after that it updates as you pick.
    if not st.session_state.get("trade_data_loaded"):
        if not st.button("Evaluate", type="primary", width="stretch"):
            st.caption("Reads every team's roster for recent form, so the first check takes about 30 seconds.")
            return
        st.session_state.trade_data_loaded = True

    snap = fetch(trade_data, cfg)
    try:
        e = evaluation(snap, cfg, snap.now.isoformat(), trade_key(cfg), tuple(give), tuple(get))
    except TradeInputError as err:
        st.error(str(err))
        return
    show_evaluation(e)


def show_evaluation(e: Evaluation) -> None:
    t = e.trade
    give = " and ".join(v.player.name for v in t.give)
    get = " and ".join(v.player.name for v in t.get)
    st.subheader(f"{give} for {get}", anchor=False)
    VERDICT_BOX[e.verdict](md(evaluation_headline(e)))
    row = st.container(horizontal=True)
    row.metric("For you", f"{t.gain:+.1f}/wk", border=True, help="Rest of season values, best lineup each day")
    row.metric("To them", f"{t.their_gain:+.1f}/wk", border=True, help=THEM_HELP)
    row.metric("To them, really", f"{e.their_ros_gain:+.1f}/wk", border=True, help="Their side on rest of season values")
    through = f" through {e.through:%b} {e.through.day}" if e.through else ""
    st.caption(f"{t.total:+.0f} fpts for you over the rest of the season ({e.days} days{through}).")
    for n in e.notes:
        st.warning(md(n))

    rows = []
    for side, vs in (("Send", t.give), ("Get", t.get), ("You drop", t.drop), ("You add", t.add),
                     ("They drop", t.their_drop), ("They add", t.their_add)):
        for v in vs:
            p = v.player
            rows.append({"Side": side, "Player": p.name, "Team": who(p), "Now": v.now, "ROS": v.ros,
                         "L7": p.fpts.get("last_7"), "L15": p.fpts.get("last_15"), "Season": p.fpts.get("season"),
                         "Proj": p.fpts.get("projected"),
                         "Status": "" if p.injury_status in ("", "ACTIVE") else p.status_label()})
    num = st.column_config.NumberColumn(format="%.1f")
    st.dataframe(rows, hide_index=True, width="stretch", column_config={
        "Now": st.column_config.NumberColumn(format="%.1f", help="Fpts/g on recent form"),
        "ROS": st.column_config.NumberColumn(format="%.1f", help="Fpts/g rest of season"),
        "L7": num, "L15": num, "Season": num, "Proj": num,
    })
    with st.expander("Details"):
        st.markdown("\n".join(f"- {md(x)}" for x in t.reasons))
    alternatives(e)
    with st.expander("Text report"):
        st.code(render_evaluation(e), language=None, wrap_lines=True)


def load_trade(tid: int, give: list, get: list) -> None:
    """Open a trade in Check a trade. A button callback, since widget values can only be set
    before the widgets are drawn."""
    st.session_state.trade_mode = "Check a trade"
    st.session_state.check_team = tid
    st.session_state.check_give = give
    st.session_state[f"check_get_{tid}"] = get
    st.session_state.trade_data_loaded = True  # the rosters are already read


def alternatives(e: Evaluation) -> None:
    st.subheader(f"Other trades with {e.trade.team_name}", anchor=False)
    if not e.alternatives:
        st.info(no_alternatives(e))
        return
    st.caption(f"Built around {' or '.join(e.core)}: 1-for-1s, packages and deals where you get more back, "
               "each judged on its own against your roster as it is.")
    for i, a in enumerate(e.alternatives, 1):
        card = trade_card(i, a, title=shape_label(a))
        card.button("Check this one", key=f"alt_{i}", width="stretch", on_click=load_trade,
                    args=(a.team_id, [v.player.id for v in a.give], [v.player.id for v in a.get]))


def build_around_picker(scout: Snapshot) -> list:
    """Players to build trades around: yours to send, theirs to get. Picked from the rosters,
    so every name is a real player."""
    mine = sorted(scout.roster_for_moves(), key=lambda p: p.name)
    theirs = sorted(scout.other_rostered, key=lambda p: (scout.team_names.get(p.team_id, ""), p.name))
    by_id = {p.id: p for p in mine + theirs}
    mine_ids = {p.id for p in mine}

    def label(i: int) -> str:
        p = by_id[i]
        owner = "yours" if i in mine_ids else scout.team_names.get(p.team_id, "")
        return f"{player_label(p)} · {owner}"

    return st.multiselect(
        "Build around (optional)", [p.id for p in mine + theirs], format_func=label, key="build_around",
        placeholder="Any player, yours or theirs",
        help="Yours: trades with every team that send at least one of them. Theirs: trades that get you at "
             "least one of them, from the teams that have them. Packages and uneven deals included.",
    )


def find_trades(cfg: Config, scout: Snapshot) -> None:
    include = build_around_picker(scout)
    if not st.session_state.get("trades"):
        if not st.button("Find trades", type="primary", width="stretch"):
            st.caption("Reads every team's roster, so the first look takes about 30 seconds.")
            return
        st.session_state.trades = True
        st.rerun()  # without the button
    snap = fetch(trade_data, cfg)
    if include:
        try:
            s = search(snap, cfg, snap.now.isoformat(), trade_key(cfg), tuple(include))
        except TradeInputError as err:
            st.error(str(err))
            return
        show_search(s)
        return
    tr = trade_report(snap, cfg, snap.now.isoformat(), trade_key(cfg))

    through = f" through {tr.through:%b} {tr.through.day}" if tr.through else ""
    st.subheader("Top trades", anchor=False)
    st.caption(f"Rest of season: {tr.days} days{through}. Each is judged on its own against your roster as it is.")
    for i, t in enumerate(tr.trades, 1):
        trade_card(i, t)
    if not tr.trades:
        even = "looks even" if tr.fairness <= 0 else f"looks no more than {tr.fairness:g} fpts/week worse"
        st.info(f"None. No trade adds {tr.min_gain:g}+ fpts/week for you and {even} to the other team.")

    if tr.sell_high:
        st.subheader("Sell high", anchor=False)
        st.caption("Your players running above their rest of season value.")
        for v in tr.sell_high:
            st.markdown(f"**{v.player.name}** · {who(v.player)} · :green-badge[{v.gap:+.1f}/g]")
            st.caption(md(form(v)))
    if tr.buy_low:
        st.subheader("Buy low", anchor=False)
        st.caption("Other teams' players running below theirs, good enough to start for you.")
        for v, team in tr.buy_low:
            st.markdown(f"**{v.player.name}** · {who(v.player)} · {team} · :blue-badge[{v.gap:+.1f}/g]")
            st.caption(md(form(v)))


def show_search(s: TradeSearch) -> None:
    subject = search_subject(s)
    st.subheader(f"Trades {subject}", anchor=False)
    if not s.trades:
        st.info(search_none(s))
        return
    teams = "team" if s.teams_searched == 1 else "teams"
    through = f" through {s.through:%b} {s.through.day}" if s.through else ""
    st.caption(f"Best across {s.teams_searched} {teams}, at most {SEARCH_PER_TEAM} with any one. Rest of season: "
               f"{s.days} days{through}. Each is judged on its own against your roster as it is.")
    for i, t in enumerate(s.trades, 1):
        card = trade_card(i, t, title=f"{shape_label(t)} with {t.team_name}")
        card.button("Check this one", key=f"search_{i}", width="stretch", on_click=load_trade,
                    args=(t.team_id, [v.player.id for v in t.give], [v.player.id for v in t.get]))
    with st.expander("Text report"):
        st.code(render_search(s), language=None, wrap_lines=True)


def main() -> None:
    st.set_page_config(page_title="Fantasy scout", page_icon="🏀", layout="centered")
    st.html(STYLE)
    sec = secrets()
    if not unlocked(str(sec.get("password") or os.environ.get("APP_PASSWORD", ""))):
        return
    try:
        base = config(sec)
    except SystemExit as e:
        st.error(str(e))
        return

    snap = fetch(scout_data, base)
    header(snap)
    today_tab, pickups_tab, trades_tab, settings_tab = st.tabs(["Today", "Pickups", "Trades", "Settings"])
    with settings_tab:  # first, since the other tabs use what's set here
        cfg = settings(base, snap)
    r = daily_report(snap, cfg, snap.now.isoformat(), pickup_key(cfg))
    with today_tab:
        today(r, snap)
    with pickups_tab:
        pickups(r)
    with trades_tab:
        trades(cfg, snap)


main()
