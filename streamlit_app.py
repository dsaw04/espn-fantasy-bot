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
    WEAKEST_SHOWN, _hashtag_rank, evaluation_headline, lineup_needs_changes, move_value, render, render_evaluation,
)
from fantasy_bot.trade import Evaluation, TradeInputError, TradeReport, build_trades, evaluate_trade, form

ROOT = Path(__file__).parent
TTL = 15 * 60  # seconds a read from ESPN is reused before the next one

# Dark glass on a violet and cyan glow, on top of the dark theme in .streamlit/config.toml.
# Phones get tighter margins, smaller headings and smaller metrics, so two fit side by side.
STYLE = """<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --violet: #8B5CF6;
  --cyan: #22D3EE;
  --pink: #E879F9;
  --glass: rgba(18, 20, 42, 0.62);
  --edge: rgba(139, 92, 246, 0.28);
  --muted: #9AA0C3;
  --grad: linear-gradient(110deg, var(--violet) 0%, var(--pink) 45%, var(--cyan) 100%);
}

html, body, [class*="st-"], .stMarkdown, button, input, textarea {
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
}
code, pre, [data-testid="stCode"] * { font-family: 'JetBrains Mono', ui-monospace, monospace !important; }

/* A slow aurora behind everything */
[data-testid="stApp"] {
  background:
    radial-gradient(60rem 30rem at 10% -10%, rgba(139, 92, 246, 0.22), transparent 60%),
    radial-gradient(50rem 28rem at 110% 10%, rgba(34, 211, 238, 0.14), transparent 60%),
    radial-gradient(40rem 30rem at 50% 120%, rgba(232, 121, 249, 0.10), transparent 60%),
    #07080F;
  background-attachment: fixed;
}
[data-testid="stHeader"] { background: transparent; }

/* Headings: gradient title, grotesk subheads */
h1, h2, h3 { font-family: 'Space Grotesk', 'Inter', sans-serif !important; letter-spacing: -0.02em; }
h1 {
  background: var(--grad);
  -webkit-background-clip: text; background-clip: text;
  color: transparent !important;
  font-weight: 700 !important;
}
h3 { color: #F1F2FA !important; font-weight: 600 !important; }
h3::before {
  content: ""; display: inline-block; width: 0.5rem; height: 0.5rem; margin: 0 0.55rem 0.18rem 0;
  border-radius: 50%; background: var(--grad); box-shadow: 0 0 10px var(--violet);
}
[data-testid="stCaptionContainer"] { color: var(--muted) !important; }

/* The "AI scout" pill above the title */
.ai-pill {
  display: inline-flex; align-items: center; gap: 0.4rem;
  padding: 0.22rem 0.7rem; border-radius: 999px;
  font: 600 0.72rem/1.2 'JetBrains Mono', monospace; letter-spacing: 0.08em; text-transform: uppercase;
  color: #E9D5FF; background: rgba(139, 92, 246, 0.14); border: 1px solid var(--edge);
}
.ai-pill .dot {
  width: 0.45rem; height: 0.45rem; border-radius: 50%; background: var(--cyan);
  box-shadow: 0 0 8px var(--cyan); animation: pulse 2s ease-in-out infinite;
}
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

/* Glass cards: bordered containers, metrics, expanders, alerts */
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stMetric"], [data-testid="stExpander"] details {
  background: var(--glass) !important;
  border: 1px solid var(--edge) !important;
  backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.02) inset, 0 10px 30px -12px rgba(0, 0, 0, 0.6);
  transition: border-color 0.2s, box-shadow 0.2s;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
  border-color: rgba(34, 211, 238, 0.45) !important;
  box-shadow: 0 0 24px -6px rgba(139, 92, 246, 0.45);
}
[data-testid="stMetric"] { padding: 0.8rem 1rem !important; }
[data-testid="stMetricLabel"] p {
  font: 500 0.72rem 'JetBrains Mono', monospace !important; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted) !important;
}
[data-testid="stMetricValue"] {
  font-family: 'Space Grotesk', sans-serif; font-weight: 700;
  background: var(--grad); -webkit-background-clip: text; background-clip: text; color: transparent;
}
[data-testid="stAlert"] > div {
  background: var(--glass) !important; border: 1px solid var(--edge);
  backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
}

/* Tabs as a floating segmented bar */
[data-testid="stTabs"] [role="tablist"] {
  gap: 0.25rem; padding: 0.3rem; border-radius: 999px;
  background: var(--glass); border: 1px solid var(--edge); width: fit-content; max-width: 100%;
  overflow-x: auto;
}
[data-testid="stTabs"] [role="tab"] { border-radius: 999px; padding: 0.35rem 0.95rem; color: var(--muted); }
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
  color: #fff; background: linear-gradient(110deg, rgba(139, 92, 246, 0.55), rgba(34, 211, 238, 0.35));
  box-shadow: 0 0 16px -4px var(--violet);
}
[data-testid="stTabs"] [data-baseweb="tab-highlight"], [data-testid="stTabs"] [data-baseweb="tab-border"] { display: none; }

/* Primary buttons glow */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
  background: var(--grad) !important; border: none !important; color: #0B0B14 !important; font-weight: 700;
  box-shadow: 0 6px 24px -6px rgba(139, 92, 246, 0.7);
}
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover {
  filter: brightness(1.1); box-shadow: 0 6px 30px -4px rgba(34, 211, 238, 0.7);
}
[data-testid="stBaseButton-secondary"] { background: var(--glass) !important; border-color: var(--edge) !important; }

/* Inputs and tables */
[data-baseweb="select"] > div, [data-baseweb="input"] { background: rgba(10, 11, 24, 0.7) !important; border-color: var(--edge) !important; }
[data-baseweb="tag"] { background: rgba(139, 92, 246, 0.25) !important; }
[data-testid="stDataFrame"] { border: 1px solid var(--edge); border-radius: 0.75rem; overflow: hidden; }

@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"] { padding: 4rem 0.75rem 4rem; }
  [data-testid="stMetricValue"] { font-size: 1.4rem; }
  h1 { font-size: 1.6rem !important; }
  h3 { font-size: 1.25rem !important; }
  [data-testid="stTabs"] [role="tab"] { padding: 0.3rem 0.7rem; }
}
@media (prefers-reduced-motion: reduce) { .ai-pill .dot { animation: none; } }
</style>"""

PILL = '<span class="ai-pill"><span class="dot"></span>AI fantasy scout</span>'


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
    st.html(PILL)
    st.title("Fantasy scout", anchor=False)
    st.caption("Locked. Enter the password to see your league.")
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


@st.cache_data(ttl=TTL, show_spinner="Scoring the trade...")
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
    return tuple(cfg.untouchable), cfg.trade_max_players, cfg.trade_min_gain, cfg.trade_fairness


# ---- tabs ------------------------------------------------------------------------


def header(snap: Snapshot) -> None:
    st.html(PILL)
    st.title(snap.my_team_name.strip(), anchor=False)
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
        st.form_submit_button("Apply", type="primary", icon=":material/check:", width="stretch")
    st.caption("These last until you reload the page. The defaults live in bot.toml.")
    return replace(
        cfg, untouchable=untouchable, max_moves=int(max_moves), streaming_spots=int(streaming),
        min_win_gain=win_gain / 100, min_edge=min_edge,
        trade_max_players=max_players, trade_min_gain=min_gain, trade_fairness=fairness,
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


def trade_card(i: int, t) -> None:
    with st.container(border=True):
        give = " and ".join(v.player.name for v in t.give)
        get = " and ".join(v.player.name for v in t.get)
        st.markdown(f"**{i}. Trade with {t.team_name}**  \nSend **{give}**  \nGet **{get}**")
        row = st.container(horizontal=True)
        row.metric("For you", f"{t.gain:+.1f}/wk", border=True)
        row.metric("To them, now", f"{t.their_gain:+.1f}/wk", border=True)
        st.caption(f"{t.total:+.0f} fpts for you over the rest of the season")
        with st.expander("Details"):
            st.markdown("\n".join(f"- {md(x)}" for x in t.reasons))


TRADE_MODES = {"Find trades": ":material/travel_explore:", "Check a trade": ":material/balance:"}


def trades(cfg: Config, scout: Snapshot) -> None:
    st.caption("*Now* is recent form, which other managers go by. *Rest of season* leans on the season "
               "and ESPN's projection. Trades are scored with your best lineup each day.")
    mode = st.segmented_control("Trades", ["Find trades", "Check a trade"], default="Find trades",
                                format_func=lambda m: TRADE_MODES[m] + " " + m,
                                label_visibility="collapsed")
    if mode == "Check a trade":
        check_trade(cfg, scout)
    else:
        find_trades(cfg)


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
        tid = st.selectbox("Trade with", tids, format_func=lambda t: scout.team_names.get(t, f"team {t}"))
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
        if not st.button("Evaluate", type="primary", icon=":material/auto_awesome:", width="stretch"):
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
    row.metric("To them, now", f"{t.their_gain:+.1f}/wk", border=True, help="How it looks on current form")
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
    with st.expander("Text report"):
        st.code(render_evaluation(e), language=None, wrap_lines=True)


def find_trades(cfg: Config) -> None:
    if not st.session_state.get("trades"):
        if not st.button("Find trades", type="primary", icon=":material/auto_awesome:", width="stretch"):
            st.caption("Reads every team's roster, so the first look takes about 30 seconds.")
            return
        st.session_state.trades = True
        st.rerun()  # without the button
    snap = fetch(trade_data, cfg)
    tr = trade_report(snap, cfg, snap.now.isoformat(), trade_key(cfg))

    through = f" through {tr.through:%b} {tr.through.day}" if tr.through else ""
    st.subheader("Top trades", anchor=False)
    st.caption(f"Rest of season: {tr.days} days{through}. Each is judged on its own against your roster as it is.")
    for i, t in enumerate(tr.trades, 1):
        trade_card(i, t)
    if not tr.trades:
        even = "looks even" if tr.fairness <= 0 else f"looks no more than {tr.fairness:g} fpts/week worse"
        st.info(f"None. No trade adds {tr.min_gain:g}+ fpts/week for you and {even} to the other team on current form.")

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
    today_tab, pickups_tab, trades_tab, settings_tab = st.tabs([
        ":material/today: Today", ":material/person_add: Pickups", ":material/swap_horiz: Trades", ":material/tune: Settings",
    ])
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
