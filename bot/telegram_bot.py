"""Telegram front end: command handlers + scheduled digest jobs.

One always-on async process. python-telegram-bot's JobQueue (APScheduler
under the hood) fires the pre/post-waiver and gameday digests; the command
handlers serve on-demand queries. Everything shares a single SleeperClient
and the cached LeagueContext from analysis.build_context().
"""

import logging
import re
from datetime import datetime
from functools import wraps
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import analysis, config, digest, grok, journal, prompting, valuation
from .sleeper import SleeperClient, is_out, player_name

logger = logging.getLogger("fantasy_bot")


def projected_points_of(row: dict):
    """Actual fantasy points from a stats row, for scoring past decisions."""
    stats = row.get("stats") if isinstance(row.get("stats"), dict) else row
    if not isinstance(stats, dict):
        return None
    for k in ("pts_ppr", "pts_half_ppr", "pts_std"):
        v = stats.get(k)
        if isinstance(v, (int, float)):
            return round(float(v), 1)
    return None

# Shared across all handlers/jobs for the process lifetime.
client = SleeperClient()

HELP_TEXT = (
    "🏈 <b>Fantasy Assistant</b>\n\n"
    "<b>/pre</b> — pre-waiver digest (needs, FAAB bids, drops, live buzz)\n"
    "<b>/post</b> — post-waiver digest (league recap + next targets)\n"
    "<b>/waivers</b> — hot free agents + suggested FAAB bids\n"
    "<b>/drops</b> — droppable players on your roster\n"
    "<b>/roster</b> — your team, grouped by position (injuries flagged)\n"
    "<b>/needs</b> — where your roster is thin\n"
    "<b>/log</b> — record a move you made (and what you expected)\n"
    "<b>/journal</b> — review recent decisions\n"
    "<b>/review</b> — score past calls against what actually happened\n"
    "<b>/usage</b> — whose role grew or shrank, and where points lag the role\n"
    "<b>/stash</b> — who is one injury away from starter value\n"
    "<b>/bench</b> — why do I own each bench player\n"
    "<b>/plan</b> — the next 2-4 weeks: byes, thin spots, buy early\n"
    "<b>/news</b> — scan X + news now for anything actionable on your wire\n"
    "<b>/trending</b> — most-added players across Sleeper right now\n"
    "<b>/player &lt;name&gt;</b> — outlook + availability + FAAB bid for any player\n"
    "<b>/startsit</b> — optimal lineup + start/sit calls for the week\n"
    "<b>/trade</b> — find an ideal trade: fair deal + opening offer + how to pitch\n"
    "<b>/tradecheck</b> A for B — price a specific offer instantly, no model\n"
    "<b>/deep &lt;question&gt;</b> — force the flagship model for a big call\n"
    "<b>/gameday</b> — quick injury sweep of your starters\n"
    "<b>/reset</b> — clear conversation memory / start a fresh topic\n"
    "<b>/help</b> — this message\n\n"
    "💬 <b>Or just text me any question</b> — e.g. \"who should I start at "
    "FLEX?\" or \"best waiver TE for my team?\" — and I'll answer using your "
    "roster + live search. Trade/lineup-optimization questions auto-upgrade to "
    "the flagship model."
)


# --- Auth -------------------------------------------------------------------
def authorized_only(func):
    """Only respond in the configured chat. Prevents strangers who find the
    bot from driving it."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id) if update.effective_chat else ""
        if config.TELEGRAM_CHAT_ID and chat_id != str(config.TELEGRAM_CHAT_ID):
            logger.warning("Ignoring command from unauthorized chat %s", chat_id)
            return
        return await func(update, context)

    return wrapper


# --- Helpers ----------------------------------------------------------------
async def _send(update: Update, text: str) -> None:
    for chunk in digest.split_for_telegram(text):
        try:
            await update.effective_chat.send_message(
                chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        except Exception:
            # A single malformed tag makes Telegram reject the whole message.
            # Losing the formatting beats losing the answer, so retry as plain
            # text before letting the error handler swallow it.
            logger.warning("HTML send failed; retrying as plain text", exc_info=True)
            await update.effective_chat.send_message(
                chunk, disable_web_page_preview=True
            )


async def _typing(update: Update) -> None:
    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
    except Exception:
        pass


async def _ctx(force: bool = False) -> analysis.LeagueContext:
    return await analysis.build_context(client, force=force)


# --- Command handlers -------------------------------------------------------
@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, HELP_TEXT)


@authorized_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, HELP_TEXT)


@authorized_only
async def cmd_pre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _typing(update)
    try:
        ctx = await _ctx()
        text = await digest.build_pre_waiver_digest(ctx, client)
    except Exception as exc:
        text = f"⚠️ Couldn't build the pre-waiver digest: {digest.esc(exc)}"
    await _send(update, text)


@authorized_only
async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _typing(update)
    try:
        ctx = await _ctx()
        text = await digest.build_post_waiver_digest(ctx, client)
    except Exception as exc:
        text = f"⚠️ Couldn't build the post-waiver digest: {digest.esc(exc)}"
    await _send(update, text)


@authorized_only
async def cmd_waivers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _typing(update)
    try:
        ctx = await _ctx()
        text = digest._header(ctx, "Waiver Targets") + await digest._faab_block(
            ctx, client
        )
    except Exception as exc:
        text = f"⚠️ Couldn't fetch waiver targets: {digest.esc(exc)}"
    await _send(update, text)


@authorized_only
async def cmd_drops(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _typing(update)
    try:
        ctx = await _ctx()
        block = await digest._drop_block(ctx, client)
        text = digest._header(ctx, "Drop Candidates") + (
            block or "\nNothing obvious to drop — your bench is lean.\n"
        )
    except Exception as exc:
        text = f"⚠️ Couldn't compute drops: {digest.esc(exc)}"
    await _send(update, text)


@authorized_only
async def cmd_needs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _typing(update)
    try:
        ctx = await _ctx()
        text = digest._header(ctx, "Roster Needs") + digest._needs_block(ctx)
    except Exception as exc:
        text = f"⚠️ Couldn't analyze needs: {digest.esc(exc)}"
    await _send(update, text)


@authorized_only
async def cmd_roster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _typing(update)
    try:
        ctx = await _ctx()
        grouped = analysis.my_players_by_position(ctx)
        lines = [digest._header(ctx, "Your Roster")]
        for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
            players = grouped.get(pos, [])
            if not players:
                continue
            lines.append(f"\n<b>{pos}</b>")
            for p in players:
                flag = f" ⛔{p.get('injury_status')}" if is_out(p) else (
                    f" 🩹{p.get('injury_status')}" if p.get("injury_status") else ""
                )
                lines.append(
                    f"• {digest.esc(player_name(p))} "
                    f"<i>({digest.esc(p.get('team') or 'FA')})</i>{digest.esc(flag)}"
                )
        text = "\n".join(lines)
    except Exception as exc:
        text = f"⚠️ Couldn't load your roster: {digest.esc(exc)}"
    await _send(update, text)


@authorized_only
async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _typing(update)
    try:
        ctx = await _ctx()
        trending = await client.get_trending("add", lookback_hours=24, limit=15)
        lines = ["🔥 <b>Most-added players (24h)</b>\n"]
        for e in trending:
            p = ctx.players.get(e.get("player_id"), {})
            owned = "✅ owned" if e.get("player_id") in ctx.rostered_ids else "🆓 FA"
            lines.append(
                f"• <b>{digest.esc(player_name(p))}</b> "
                f"({digest.esc(p.get('position') or '?')}·{digest.esc(p.get('team') or 'FA')}) "
                f"— {int(e.get('count') or 0):,} adds <i>{owned}</i>"
            )
        text = "\n".join(lines)
    except Exception as exc:
        text = f"⚠️ Couldn't fetch trending players: {digest.esc(exc)}"
    await _send(update, text)


@authorized_only
async def cmd_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = " ".join(context.args).strip() if context.args else ""
    if not name:
        await _send(update, "Usage: <code>/player Puka Nacua</code>")
        return
    # Route through the full-context brain so the answer covers this-season
    # value, availability in YOUR league, and a free-pickup vs. FAAB-bid call.
    question = (
        f"{name}: give me the fantasy outlook for this season and how "
        "immediately they help, whether they're available in my league (free "
        "pickup or waiver claim with a suggested winning FAAB bid), or if "
        "rostered, who holds them."
    )
    await _answer(update, context, question, deep=False)


async def _answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE, question: str, deep: bool
) -> None:
    """Shared free-form answer flow for on_message and /deep.

    Injects the user's roster + real available free agents, and carries a short
    rolling conversation history so follow-ups ('in my league?') keep context.
    """
    if not question:
        await _send(update, "Ask me anything, e.g. <code>who should I start at FLEX?</code>")
        return
    if not config.ENABLE_GROK:
        await _send(
            update,
            "Ask-anything needs Grok — set <code>XAI_API_KEY</code> to enable it. "
            "Meanwhile /help lists the commands that work without it.",
        )
        return
    # Directives are attached inside grok.answer_question so every entry
    # point — chat, commands and scheduled digests — gets the same process.
    deep = deep or prompting.wants_deep(question)
    await _typing(update)
    note = "🔎 On it — analyzing your league + live X/news…"
    if deep:
        note += " (deep dive — this can take a minute or two)"
    await update.effective_chat.send_message(note)

    try:
        ctx = await _ctx()
        full_ctx = await analysis.full_league_context(ctx, client)
    except Exception as exc:
        # Don't answer blindly with no league data — say what's wrong instead.
        await _send(
            update,
            "⚠️ I couldn't load your league from Sleeper, so I won't guess "
            f"without your data. Error: <code>{digest.esc(exc)}</code>\n\n"
            "Check that <code>LEAGUE_ID</code> and <code>SLEEPER_USER_ID</code> "
            "are set correctly in Railway → Variables.",
        )
        return

    history = context.chat_data.setdefault("qa_history", [])
    result = await grok.answer_question(
        question, full_ctx, deep=deep, history=list(history)
    )
    if not result:
        await _send(update, "No response from Grok.")
        return

    answer = result["text"]
    # Remember the turn (skip error replies) so follow-ups keep context.
    if not answer.startswith("⚠️"):
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        del history[:-6]  # keep ~3 turns

    text = digest.esc(answer)
    cites = result.get("citations") or []
    if cites:
        text += "\n\n<b>Sources:</b>\n" + "\n".join(
            f"• {digest.esc(c)}" for c in cites[:4]
        )
    await _send(update, text)


@authorized_only
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-form Q&A. Auto-routes high-stakes questions to the flagship model."""
    question = (update.message.text or "").strip() if update.message else ""
    await _answer(update, context, question, deep=prompting.wants_deep(question))


@authorized_only
async def cmd_deep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force the flagship model for a big decision: /deep <question>."""
    question = " ".join(context.args).strip() if context.args else ""
    await _answer(update, context, question, deep=True)


@authorized_only
async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Structured trade finder: /trade [player or team to focus on]."""
    focus = " ".join(context.args).strip() if context.args else ""
    question = (
        "Find me the best trade in my league right now. Using the full rosters, "
        "identify the ideal trade partner and which of my player(s) to send for "
        "which of theirs, based on our complementary roster needs. Give me: "
        "(1) the ideal target and the reasoning, (2) a FAIR, balanced proposal "
        "both managers should be happy with, (3) a realistic OPENING OFFER to "
        "send first — slightly in my favor to leave room to negotiate, and "
        "(4) how to pitch it and what counter-offer to expect."
    )
    if focus:
        question += f" Build the trade around: {focus}."
    # Trades are multi-factor — always use the flagship model.
    await _answer(update, context, question, deep=True)


@authorized_only
async def cmd_tradecheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Price a specific proposal with no model in the loop:
    /tradecheck <players I send> for <players I get>."""
    raw = " ".join(context.args).strip() if context.args else ""
    parts = re.split(r"\s+for\s+", raw, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        await _send(
            update,
            "Usage: <code>/tradecheck James Cook for Cam Skattebo</code>\n"
            "Multiple players per side, separated by commas:\n"
            "<code>/tradecheck Cook, Pittman for Skattebo</code>",
        )
        return

    await _typing(update)
    try:
        ctx = await _ctx()
    except Exception as exc:
        await _send(update, f"⚠️ Couldn't load your league: <code>{digest.esc(exc)}</code>")
        return

    def resolve(side: str) -> tuple[list[str], list[str]]:
        found, missing = [], []
        for name in (n.strip() for n in side.split(",")):
            if not name:
                continue
            pid = valuation.find_player(ctx, name)
            (found if pid else missing).append(pid or name)
        return found, missing

    send_ids, send_missing = resolve(parts[0])
    recv_ids, recv_missing = resolve(parts[1])
    missing = send_missing + recv_missing
    if missing:
        await _send(
            update,
            "⚠️ Couldn't find: " + digest.esc(", ".join(missing))
            + "\nTry the player's full name.",
        )
        return
    if not send_ids or not recv_ids:
        await _send(update, "⚠️ Name at least one player on each side.")
        return

    r = valuation.evaluate_trade(ctx, send_ids, recv_ids)
    lines = [
        "⚖️ <b>Trade check</b>",
        f"\n<b>You send</b> (total {r['send_value']}):",
    ]
    lines += [f"• {digest.esc(x)}" for x in r["send"]]
    lines.append(f"\n<b>You get</b> (total {r['receive_value']}):")
    lines += [f"• {digest.esc(x)}" for x in r["receive"]]
    lines.append(
        f"\n<b>{digest.esc(r['verdict'])}</b>\n"
        f"<i>Value gap: {r['gap_pct']:+.1f}% toward "
        f"{'you' if r['gap_pct'] >= 0 else 'them'}</i>"
    )
    lines.append(
        "\n<i>Value is points above a replacement-level waiver add in your "
        "league's scoring and starters — comparable across positions. This is "
        "the raw price only; ask me about the trade for the live injury, role "
        "and expert read on top.</i>"
    )
    if config.JOURNAL_ENABLED:
        journal.record(
            "tradecheck",
            ctx.week,
            f"{', '.join(r['send'])} → {', '.join(r['receive'])}",
            rationale=r["verdict"],
            expected=f"value gap {r['gap_pct']:+.1f}% ({r['send_value']} vs {r['receive_value']})",
            players=[
                player_name(ctx.players.get(p) or {}) for p in send_ids + recv_ids
            ],
            data={"gap_pct": r["gap_pct"], "verdict": r["verdict"]},
        )
    await _send(update, "\n".join(lines))


@authorized_only
async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What changed in usage: /usage."""
    await _answer(update, context,
        "What changed in USAGE over the last couple of weeks — whose role grew "
        "and whose shrank? Use the usage boards. Call out anyone whose snaps or "
        "targets are climbing while their fantasy points lag, since that is the "
        "buy window, and flag anyone on my roster losing work.", deep=False)


@authorized_only
async def cmd_stash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One-injury-away targets: /stash."""
    await _answer(update, context,
        "Which available players in my league are ONE INJURY AWAY from "
        "immediate starter value? Use the contingent-value figures and the "
        "depth charts. For each, name the starter ahead of him, how much of "
        "that job he would inherit, and whether he is worth a bench spot now — "
        "and tell me which of my own bench players to give up for him.",
        deep=False)


@authorized_only
async def cmd_bench(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bench audit: /bench."""
    await _answer(update, context,
        "Audit my bench. For every bench player, why exactly do I own him — "
        "insurance for a specific starter, a rising role, a known streaming "
        "week, or real upside? Any spot without a clear answer is being wasted: "
        "name it and say what should replace it.", deep=False)


@authorized_only
async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Two-to-four week outlook: /plan."""
    await _answer(update, context,
        "Look two to four weeks ahead, not just this week. Use the bye outlook "
        "and my playoff weeks. Where does my roster break — byes stacking, a "
        "position going thin, a streaming slot with no plan? What should I "
        "acquire NOW while it is cheap rather than the week I need it?",
        deep=True)


@authorized_only
async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record a move you actually made: /log added X, dropped Y, $18."""
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await _send(
            update,
            "Usage: <code>/log added Kaelon Black, dropped Doubs, $18</code>\n"
            "Add <code>| expected: ...</code> to record what you thought would "
            "happen — that's the part that makes a later review about process.",
        )
        return
    expected = ""
    if "| expected:" in text:
        text, expected = (x.strip() for x in text.split("| expected:", 1))
    try:
        ctx = await _ctx()
        week = ctx.week
    except Exception:
        week = 0
    ok = journal.record("manual", week, text, expected=expected)
    note = "" if ok else "\n⚠️ <i>Could not write the journal — check JOURNAL_PATH.</i>"
    await _send(update, f"📓 Logged for Week {week}.{note}")


@authorized_only
async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Review recent decisions: /journal [count]."""
    try:
        limit = int(context.args[0]) if context.args else 8
    except (ValueError, IndexError):
        limit = 8
    entries = journal.recent(max(1, min(limit, 25)))
    if not entries:
        writable, note = journal.available()
        await _send(
            update,
            "📓 No decisions recorded yet."
            + ("" if writable else f"\n⚠️ Journal is {digest.esc(note)}."),
        )
        return
    await _send(update, "📓 <b>Decision journal</b>\n\n" + journal.summarize(entries))


@authorized_only
async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Score past decisions against what actually happened: /review."""
    await _typing(update)
    try:
        ctx = await _ctx()
    except Exception as exc:
        await _send(update, f"⚠️ Couldn't load your league: <code>{digest.esc(exc)}</code>")
        return

    pending = journal.unscored(ctx.week)
    if not pending:
        await _send(
            update,
            "📓 Nothing to review — no decisions from completed weeks are "
            "waiting on an outcome.",
        )
        return

    # Attach what actually happened, then judge the process rather than the
    # result: a sound call that didn't pay is still a sound call.
    lines = []
    for e in pending[-6:]:
        wk = int(e.get("week") or 0)
        scored = []
        for name in e.get("players", [])[:4]:
            pid = valuation.find_player(ctx, name)
            if not pid:
                continue
            try:
                rows = await client.get_stats(ctx.season, wk)
            except Exception:
                rows = []
            pts = next(
                (
                    projected_points_of(r)
                    for r in rows
                    if str(r.get("player_id")) == pid
                ),
                None,
            )
            if pts is not None:
                scored.append(f"{name} {pts}pts in W{wk}")
        outcome = "; ".join(scored) if scored else "no box score found"
        journal.score(e["ts"], outcome)
        lines.append(
            f"<b>W{wk} · {e.get('kind')}</b> — {digest.esc(e.get('summary',''))}\n"
            f"   <i>expected: {digest.esc(e.get('expected') or 'not recorded')}</i>\n"
            f"   <i>actual: {digest.esc(outcome)}</i>"
        )
    await _send(
        update,
        "📓 <b>Review</b>\n\n" + "\n\n".join(lines)
        + "\n\n<i>Judge the process, not the result. A sound call that didn't "
        "pay is still sound; a lucky one is still lucky.</i>",
    )


@authorized_only
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the conversation memory to start a fresh topic."""
    context.chat_data.pop("qa_history", None)
    await _send(update, "🧹 Conversation memory cleared — ask me something fresh.")


@authorized_only
async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what env vars the running process actually sees, and try to load
    the league — for diagnosing deployment/config issues."""
    def yn(v):
        return "✅" if v else "❌ missing"

    lines = [
        "<b>Diagnostics — what the process sees</b>",
        f"TELEGRAM_TOKEN: {yn(config.TELEGRAM_TOKEN)}",
        f"TELEGRAM_CHAT_ID: {yn(config.TELEGRAM_CHAT_ID)}",
        f"LEAGUE_ID: {yn(config.LEAGUE_ID)}"
        + (f" <code>{digest.esc(config.LEAGUE_ID)}</code>" if config.LEAGUE_ID else ""),
        f"LEAGUE_NAME: {yn(config.LEAGUE_NAME)}"
        + (f" <code>{digest.esc(config.LEAGUE_NAME)}</code>" if config.LEAGUE_NAME else "")
        + (" <i>(from env)</i>" if config.LEAGUE_NAME_FROM_ENV else " <i>(code default)</i>"),
        f"SLEEPER_USER_ID: {yn(config.SLEEPER_USER_ID)}"
        + (f" <code>{digest.esc(config.SLEEPER_USER_ID)}</code>" if config.SLEEPER_USER_ID else "")
        + (" <i>(from env)</i>" if config.SLEEPER_USER_ID_FROM_ENV else " <i>(code default)</i>"),
        f"SLEEPER_USERNAME: {yn(config.SLEEPER_USERNAME)}",
        f"SEASON: <code>{digest.esc(config.SEASON)}</code>",
        f"XAI_API_KEY: {yn(config.XAI_API_KEY)}",
    ]
    try:
        ctx = await _ctx(force=True)
        lines.append(
            f"\n✅ League loaded: <b>{digest.esc(ctx.league.get('name'))}</b> — "
            f"your team: <b>{digest.esc(ctx.team_name(ctx.my_user_id))}</b>"
        )
        # The value signals behind every trade/start-sit call. Projections come
        # from an undocumented Sleeper endpoint, so surface when they're absent
        # rather than letting advice quietly fall back to vibes.
        lines.append(
            f"Market ranks: {yn(ctx.market_ranks)} "
            f"({len(ctx.market_ranks)} players)"
        )
        lines.append(
            f"Draft picks: {yn(ctx.draft_picks)} ({len(ctx.draft_picks)} picks)"
        )
        lines.append(
            f"Projections: {yn(ctx.has_projections)} "
            f"({len(ctx.week_projections)} this week, "
            f"{len(ctx.season_projections)} season)"
        )
        levels = valuation.replacement_levels(ctx)
        ranks = valuation.replacement_ranks(ctx)
        lines.append(
            f"Valuation: {yn(ctx.player_values)} ({len(ctx.player_values)} "
            "players priced)"
        )
        lines.append(f"Usage/participation: {yn(ctx.usage)} ({len(ctx.usage)} players)")
        writable, note = journal.available()
        lines.append(
            f"Journal: {yn(writable)} <i>{digest.esc(note)}</i> "
            f"({len(journal.recent(999))} entries)"
        )
        if ranks:
            # Shown as ranks because those are checkable by eye — but the
            # expected value depends entirely on THIS league's starting slots,
            # so show the demand it was derived from rather than a number
            # borrowed from some other league's roster settings.
            n_teams = max(1, len(ctx.rosters))
            demand = valuation.expected_starts(ctx)
            lines.append(
                "  <i>replacement level: "
                + digest.esc(", ".join(
                    f"{pos}{ranks[pos]} ({levels[pos]:.0f}pts)"
                    for pos in sorted(ranks)
                ))
                + "</i>"
            )
            lines.append(
                f"  <i>derived from {n_teams} teams × starters/team ("
                + digest.esc(", ".join(
                    f"{pos} {demand[pos] / n_teams:.1f}"
                    for pos in sorted(demand)
                ))
                + ") + bench padding — check these match your lineup slots</i>"
            )
        if not ctx.has_projections:
            lines.append(
                "  <i>⚠️ No projections — values fall back to a curve fitted "
                "to market rank. Ordering stays sane; point totals are "
                "approximate.</i>"
            )
    except Exception as exc:
        lines.append(f"\n❌ League load failed: <code>{digest.esc(exc)}</code>")
    await _send(update, "\n".join(lines))


@authorized_only
async def cmd_startsit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """On-demand start/sit: optimal lineup for the week."""
    await _typing(update)
    await update.effective_chat.send_message(
        "🔎 Setting your optimal lineup vs this week's matchup…"
    )
    try:
        ctx = await _ctx()
        text = await digest.build_start_sit(ctx, client, final=False)
    except Exception as exc:
        text = f"⚠️ Couldn't build start/sit: {digest.esc(exc)}"
    await _send(update, text)


@authorized_only
async def cmd_gameday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _typing(update)
    try:
        ctx = await _ctx()
        text = await digest.build_gameday_alert(ctx, client)
    except Exception as exc:
        text = f"⚠️ Couldn't run the gameday sweep: {digest.esc(exc)}"
    await _send(update, text)


# --- Scheduled jobs ---------------------------------------------------------
async def _push(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not config.TELEGRAM_CHAT_ID:
        logger.warning("No TELEGRAM_CHAT_ID set; skipping scheduled push.")
        return
    for chunk in digest.split_for_telegram(text):
        await context.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


def _is_day(target_day: int) -> bool:
    """True if today (in the configured timezone) matches target weekday."""
    return datetime.now(config.TIMEZONE).weekday() == target_day


async def job_pre(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not _is_day(config.PRE_DIGEST_DAY):
            return
        ctx = await _ctx(force=True)
        await _push(context, await digest.build_pre_waiver_digest(ctx, client))
    except Exception as exc:
        logger.exception("pre-waiver digest failed")
        await _push(context, "⚠️ Scheduled pre-waiver digest failed: "
                    f"<code>{digest.esc(type(exc).__name__)}: {digest.esc(exc)}</code>")


async def job_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not _is_day(config.POST_DIGEST_DAY):
            return
        ctx = await _ctx(force=True)
        await _push(context, await digest.build_post_waiver_digest(ctx, client))
    except Exception as exc:
        logger.exception("post-waiver digest failed")
        await _push(context, "⚠️ Scheduled post-waiver digest failed: "
                    f"<code>{digest.esc(type(exc).__name__)}: {digest.esc(exc)}</code>")


async def job_startsit(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not _is_day(config.STARTSIT_DAY):
            return
        ctx = await _ctx(force=True)
        await _push(context, await digest.build_start_sit(ctx, client, final=False))
    except Exception as exc:
        logger.exception("start/sit digest failed")
        await _push(context, "⚠️ Scheduled start/sit digest failed: "
                    f"<code>{digest.esc(type(exc).__name__)}: {digest.esc(exc)}</code>")


async def job_gameday(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not _is_day(config.GAMEDAY_DAY):
            return
        ctx = await _ctx(force=True)
        # Sunday is the last-minute check: full start/sit weighted to late news.
        await _push(context, await digest.build_start_sit(ctx, client, final=True))
    except Exception as exc:
        logger.exception("gameday sweep failed")
        await _push(context, "⚠️ Scheduled gameday sweep failed: "
                    f"<code>{digest.esc(type(exc).__name__)}: {digest.esc(exc)}</code>")


async def job_fa_watch(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Frequent, Grok-free scan: alert on newly-hot players available in the
    league. Primes silently on first run so a redeploy doesn't re-announce
    everything; then only surfaces genuinely new pickups. Quiet overnight."""
    hour = datetime.now(config.TIMEZONE).hour
    if hour < config.FA_WATCH_START_HOUR or hour >= config.FA_WATCH_END_HOUR:
        return
    try:
        ctx = await _ctx(force=True)
        recs = await analysis.faab_recommendations(ctx, client, limit=8)
    except Exception as exc:
        logger.warning("FA watch failed: %s", exc)
        return

    alerted: set = context.bot_data.setdefault("alerted_fa", set())
    if not context.bot_data.get("fa_primed"):
        context.bot_data["fa_primed"] = True
        alerted.update(r["player_id"] for r in recs)
        return

    fresh = []
    for r in recs:
        pid = r["player_id"]
        if pid in alerted:
            continue
        if r["fills_need"] or r["adds"] >= config.FA_WATCH_MIN_ADDS:
            fresh.append(r)
            alerted.add(pid)
    if not fresh:
        return

    lines = ["🚨 <b>Hot free agent(s) on your wire</b>"]
    for r in fresh[:5]:
        p = r["player"]
        star = "⭐" if r["fills_need"] else "•"
        lines.append(
            f"{star} <b>{digest.esc(player_name(p))}</b> "
            f"({digest.esc(digest._pos_tag(p))}) — {r['adds']:,} adds, "
            f"bid ~${r['bid']} <i>({digest.esc(r['reason'])})</i>"
        )
    lines.append("\n<i>/player &lt;name&gt; for the live read · /waivers for the full list.</i>")
    await _push(context, "\n".join(lines))


async def _run_news_scan(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Shared body for the scheduled watch and the /news command."""
    ctx = await _ctx(force=True)
    full_ctx = await analysis.full_league_context(ctx, client)
    result = await grok.breaking_news(
        full_ctx, lookback_hours=config.NEWS_WATCH_LOOKBACK_HOURS
    )
    if not result:
        return None
    text = (result.get("text") or "").strip()
    if not text or text.startswith("⚠️"):
        return None
    if "NOTHING ACTIONABLE" in text.upper():
        return ""
    cites = result.get("citations") or []
    if cites:
        text += "\n\nSources:\n" + "\n".join(f"• {c}" for c in cites[:3])
    return text


@authorized_only
async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """On-demand breaking-news sweep: /news."""
    await _typing(update)
    await update.effective_chat.send_message(
        "📡 Scanning X + news for anything actionable on your wire…"
    )
    try:
        text = await _run_news_scan(context)
    except Exception as exc:
        await _send(update, f"⚠️ News scan failed: <code>{digest.esc(exc)}</code>")
        return
    if text is None:
        await _send(update, "⚠️ No response from the news scan.")
    elif text == "":
        await _send(
            update,
            "✅ Nothing actionable on your wire right now — no injury or role "
            "news that frees up someone worth adding in your league.",
        )
    else:
        await _send(update, "📡 <b>Breaking — act on this</b>\n\n" + digest.esc(text))


async def job_news_watch(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Leading-indicator watch: read the news directly rather than waiting for
    add volume to tell us what everyone already knows.

    Silent unless there is something to act on, and it never repeats an alert
    it has already sent — an alert channel that cries wolf gets muted, which
    costs more than the miss it was trying to prevent.
    """
    hour = datetime.now(config.TIMEZONE).hour
    if hour < config.NEWS_WATCH_START_HOUR or hour >= config.NEWS_WATCH_END_HOUR:
        return
    try:
        text = await _run_news_scan(context)
    except Exception as exc:
        logger.warning("News watch failed: %s", exc)
        return
    if not text:
        return

    # Dedupe on the players named, so a story that stays in the news for a few
    # cycles is announced once rather than every three hours.
    seen: set = context.bot_data.setdefault("news_seen", set())
    try:
        ctx = await _ctx()
        named = {
            player_name(p)
            for p in ctx.players.values()
            if (p.get("position") or "") in ("QB", "RB", "WR", "TE")
            and player_name(p) in text
        }
    except Exception:
        named = set()
    fresh = named - seen
    if named and not fresh:
        return
    seen.update(named)

    await _push(context, "📡 <b>Breaking — act on this</b>\n\n" + digest.esc(text))


# --- The weekly briefs ------------------------------------------------------
# Each is the same shape: assemble the full league context, ask the one
# question that day's decision actually turns on, and push the answer. The
# question differs because the job differs — Monday reads usage, Tuesday
# prices claims, Saturday buys optionality.
_BRIEFS = {
    "usage": (
        "📊 <b>Monday — what changed</b>",
        "What changed in USAGE yesterday, not who scored. Using the USAGE "
        "RISING and USAGE FALLING boards plus your search: whose role grew, "
        "whose shrank, and which of those changes are structural rather than "
        "game-script noise. Call out anyone whose snaps or targets climbed "
        "while their fantasy points stayed quiet — those are the buy windows "
        "before the league notices. Flag anything on MY roster that is losing "
        "work. Then name the two or three players I should be targeting on "
        "waivers, before the articles tell everyone.",
    ),
    "waiver": (
        "🎯 <b>Tuesday — waiver claims &amp; FAAB</b>",
        "Waivers process early tomorrow morning, so this is my last chance to "
        "set claims. Give me the ranked list to claim, with a specific FAAB "
        "bid for each and the exact player I drop for him. Use the waiver "
        "board's upgrade figures, and use RIVAL INTEL to say who else is "
        "likely to bid and roughly what it will take to beat them. Separate "
        "genuine season-changers worth spending real budget from contingent "
        "fliers worth a dollar. If nothing is worth a claim, say so plainly.",
    ),
    "tnf": (
        "🌙 <b>Thursday — tonight's lock</b>",
        "Thursday night kicks off shortly. Do I have anyone playing tonight, "
        "and is starting them right? Remember that a Thursday player locks my "
        "FLEX for the week, so prefer them in a fixed slot and keep FLEX free "
        "to absorb a Sunday inactive. Then flag anyone questionable for Sunday "
        "where I should be lining up insurance now rather than Sunday morning.",
    ),
    "bench": (
        "🔍 <b>Saturday — bench audit</b>",
        "Audit my bench. For every bench player, why exactly do I own him — "
        "insurance for a specific starter, a rising role, a streamer for a "
        "known week, or genuine upside? Any spot without an answer is wasted, "
        "so name it and what should replace it. Then the free options: if a "
        "starter anywhere is questionable for tomorrow and his backup is "
        "available in my league, adding that backup costs only a disposable "
        "spot and pays a starter if the inactive lands.",
    ),
    "scout": (
        "🔭 <b>Sunday night — get there first</b>",
        "Today's games just finished. Before anyone builds a waiver board: "
        "which roles visibly changed today — injuries, a backup taking over, "
        "a rookie's snaps jumping, a committee resolving? Who becomes the top "
        "claim on Tuesday, and is he available right now? If I can add him "
        "tonight for a disposable bench spot, I skip the bidding entirely. "
        "Also flag anyone playing tomorrow night whose backup is worth a "
        "speculative add.",
    ),
}


async def _push_brief(context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    """Run one of the weekly briefs and push it."""
    header, question = _BRIEFS[kind]
    try:
        ctx = await _ctx(force=True)
        full_ctx = await analysis.full_league_context(ctx, client)
        result = await grok.answer_question(question, full_ctx)
        if not result:
            return
        text = (result.get("text") or "").strip()
        if not text or text.startswith("⚠️"):
            logger.warning("%s brief unusable: %s", kind, text[:120])
            return
        body = f"{header}\n<i>{digest.esc(ctx.league.get('name',''))} · Week {ctx.week}</i>\n\n"
        await _push(context, body + digest.esc(text))
        # Record what was advised and on what basis, so it can be reviewed
        # later for process rather than judged on the scoreboard.
        if config.JOURNAL_ENABLED:
            movers = [
                player_name(ctx.players.get(pid) or {})
                for pid, u in list(ctx.usage.items())[:200]
                if (u.get("snap_delta") or 0) >= 8
            ][:6]
            journal.record(
                f"brief:{kind}",
                ctx.week,
                text.split(". ")[0][:200],
                rationale=text[:800],
                expected="see recommendation",
                players=movers,
                data={"usage_movers": movers, "has_usage": bool(ctx.usage)},
            )
    except Exception as exc:
        logger.exception("%s brief failed", kind)
        await _push(
            context,
            f"⚠️ {kind} brief failed: <code>{digest.esc(type(exc).__name__)}: "
            f"{digest.esc(exc)}</code>",
        )


def _brief_job(kind: str, day_attr: str):
    """Build a job that runs one brief on its configured weekday."""
    async def job(context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_day(getattr(config, day_attr)):
            return
        await _push_brief(context, kind)

    job.__name__ = f"job_{kind}_brief"
    return job


job_usage_brief = _brief_job("usage", "USAGE_BRIEF_DAY")
job_waiver_brief = _brief_job("waiver", "WAIVER_BRIEF_DAY")
job_tnf_brief = _brief_job("tnf", "TNF_BRIEF_DAY")
job_bench_brief = _brief_job("bench", "BENCH_BRIEF_DAY")
job_scout_brief = _brief_job("scout", "SCOUT_BRIEF_DAY")


def _register_jobs(app: Application) -> None:
    jq = app.job_queue
    # Each job runs daily at its time but guards on the target weekday, so we
    # never depend on any library's day-index convention.
    # Retired by default: it fired Monday evening, before Monday Night Football,
    # so claims were priced on an incomplete week. The Tuesday waiver brief does
    # the same job at the point the decision is actually made.
    if config.PRE_DIGEST_ENABLED:
        jq.run_daily(job_pre, time=config.PRE_DIGEST_TIME, name="pre_waiver")
    jq.run_daily(job_post, time=config.POST_DIGEST_TIME, name="post_waiver")
    jq.run_daily(job_startsit, time=config.STARTSIT_TIME, name="friday_startsit")
    jq.run_daily(job_gameday, time=config.GAMEDAY_TIME, name="sunday_final")
    for kind, enabled, day, tm in (
        ("usage", config.USAGE_BRIEF_ENABLED, config.USAGE_BRIEF_DAY, config.USAGE_BRIEF_TIME),
        ("waiver", config.WAIVER_BRIEF_ENABLED, config.WAIVER_BRIEF_DAY, config.WAIVER_BRIEF_TIME),
        ("tnf", config.TNF_BRIEF_ENABLED, config.TNF_BRIEF_DAY, config.TNF_BRIEF_TIME),
        ("bench", config.BENCH_BRIEF_ENABLED, config.BENCH_BRIEF_DAY, config.BENCH_BRIEF_TIME),
        ("scout", config.SCOUT_BRIEF_ENABLED, config.SCOUT_BRIEF_DAY, config.SCOUT_BRIEF_TIME),
    ):
        if enabled and config.ENABLE_GROK:
            app.job_queue.run_daily(
                globals()[f"job_{kind}_brief"], time=tm, name=f"{kind}_brief"
            )

    if config.NEWS_WATCH_ENABLED and config.ENABLE_GROK:
        app.job_queue.run_repeating(
            job_news_watch,
            interval=config.NEWS_WATCH_HOURS * 3600,
            first=120,
            name="news_watch",
        )
    if config.FA_WATCH_ENABLED:
        jq.run_repeating(
            job_fa_watch,
            interval=config.FA_WATCH_HOURS * 3600,
            first=120,
            name="fa_watch",
        )
    logger.info(
        "Scheduled: pre=%s(d%s) post=%s(d%s) startsit=%s(d%s) sunday=%s(d%s)",
        config.PRE_DIGEST_TIME, config.PRE_DIGEST_DAY,
        config.POST_DIGEST_TIME, config.POST_DIGEST_DAY,
        config.STARTSIT_TIME, config.STARTSIT_DAY,
        config.GAMEDAY_TIME, config.GAMEDAY_DAY,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global safety net: log any unhandled error and tell the user.

    The message names the actual exception. A bare "something went wrong" is
    unactionable — it looks identical whether the cause is a bad API key, a
    message Telegram rejected, or a genuine bug, and this bot has exactly one
    user, who is also the person who can fix it.
    """
    exc = context.error
    logger.exception("Unhandled handler error", exc_info=exc)
    chat_id = config.TELEGRAM_CHAT_ID
    if isinstance(update, Update) and update.effective_chat:
        chat_id = update.effective_chat.id
    if not chat_id:
        return

    detail = f"{type(exc).__name__}: {exc}" if exc else "unknown error"
    where = ""
    tb = getattr(exc, "__traceback__", None)
    while tb:  # innermost frame in our own code is the useful one
        name = tb.tb_frame.f_code.co_filename
        if "/bot/" in name or name.endswith("waiver_bot.py"):
            where = f"{name.rsplit('/', 1)[-1]}:{tb.tb_lineno}"
        tb = tb.tb_next
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ That failed. Details so it can be fixed:\n"
                f"<code>{digest.esc(detail[:400])}</code>"
                + (f"\n<i>at {digest.esc(where)}</i>" if where else "")
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        # Even the error report failed — fall back to plain text.
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=f"⚠️ That failed: {detail[:400]}"
            )
        except Exception:
            pass


def build_application() -> Application:
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_error_handler(on_error)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pre", cmd_pre))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("waivers", cmd_waivers))
    app.add_handler(CommandHandler("drops", cmd_drops))
    app.add_handler(CommandHandler("needs", cmd_needs))
    app.add_handler(CommandHandler("roster", cmd_roster))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("bench", cmd_bench))
    app.add_handler(CommandHandler("stash", cmd_stash))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("player", cmd_player))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("tradecheck", cmd_tradecheck))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CommandHandler("startsit", cmd_startsit))
    app.add_handler(CommandHandler("gameday", cmd_gameday))
    # Any plain text that isn't a command → free-form Q&A. Registered last so
    # it never shadows the command handlers above.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    _register_jobs(app)
    return app
