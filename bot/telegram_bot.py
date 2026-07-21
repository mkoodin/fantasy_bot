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

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import analysis, config, digest, grok
from .sleeper import SleeperClient, is_out, player_name

logger = logging.getLogger("fantasy_bot")

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
    "<b>/trending</b> — most-added players across Sleeper right now\n"
    "<b>/player &lt;name&gt;</b> — live X + news buzz on any player (Grok)\n"
    "<b>/deep &lt;question&gt;</b> — force the flagship model for a big call\n"
    "<b>/gameday</b> — injury sweep of your starters\n"
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
        await update.effective_chat.send_message(
            chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
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
    if not config.ENABLE_GROK:
        await _send(
            update,
            "Grok isn't configured — set <code>XAI_API_KEY</code> to enable "
            "live X + news lookups.",
        )
        return
    await _typing(update)
    result = await grok.analyze_player(name)
    if not result:
        await _send(update, "No response from Grok.")
        return
    text = f"🔎 <b>{digest.esc(name)}</b> — live X + news\n\n{digest.esc(result['text'])}"
    cites = result.get("citations") or []
    if cites:
        text += "\n\n<b>Sources:</b>\n" + "\n".join(
            f"• {digest.esc(c)}" for c in cites[:5]
        )
    await _send(update, text)


# High-stakes questions worth the flagship model (multi-factor decisions).
_DEEP_PATTERNS = re.compile(
    r"\b(trade|trading|optimal lineup|best lineup|set (?:my )?lineup|optimi[sz]e|"
    r"rest[- ]of[- ]season|\bros\b|playoff|keeper|who (?:do|should) i keep)\b",
    re.IGNORECASE,
)


def _wants_deep(text: str) -> bool:
    return bool(_DEEP_PATTERNS.search(text))


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
    await _typing(update)
    note = "🔎 On it — searching X + news…"
    if deep:
        note += " (deep analysis with grok-4.5)"
    await update.effective_chat.send_message(note)

    try:
        ctx = await _ctx()
        full_ctx = analysis.team_context_summary(ctx)
        full_ctx += "\n\n" + analysis.league_rosters_context(ctx)
        full_ctx += "\n\n" + analysis.league_faab_context(ctx)
        fa_ctx = await analysis.available_fa_context(ctx, client)
        if fa_ctx:
            full_ctx += "\n\n" + fa_ctx
    except Exception:
        full_ctx = ""  # still answer, just without personalization

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
    await _answer(update, context, question, deep=_wants_deep(question))


@authorized_only
async def cmd_deep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force the flagship model for a big decision: /deep <question>."""
    question = " ".join(context.args).strip() if context.args else ""
    await _answer(update, context, question, deep=True)


@authorized_only
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the conversation memory to start a fresh topic."""
    context.chat_data.pop("qa_history", None)
    await _send(update, "🧹 Conversation memory cleared — ask me something fresh.")


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
    if not _is_day(config.PRE_DIGEST_DAY):
        return
    ctx = await _ctx(force=True)
    await _push(context, await digest.build_pre_waiver_digest(ctx, client))


async def job_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_day(config.POST_DIGEST_DAY):
        return
    ctx = await _ctx(force=True)
    await _push(context, await digest.build_post_waiver_digest(ctx, client))


async def job_gameday(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_day(config.GAMEDAY_DAY):
        return
    ctx = await _ctx(force=True)
    await _push(context, await digest.build_gameday_alert(ctx, client))


def _register_jobs(app: Application) -> None:
    jq = app.job_queue
    # Each job runs daily at its time but guards on the target weekday, so we
    # never depend on any library's day-index convention.
    jq.run_daily(job_pre, time=config.PRE_DIGEST_TIME, name="pre_waiver")
    jq.run_daily(job_post, time=config.POST_DIGEST_TIME, name="post_waiver")
    jq.run_daily(job_gameday, time=config.GAMEDAY_TIME, name="gameday")
    logger.info(
        "Scheduled jobs: pre=%s(day %s) post=%s(day %s) gameday=%s(day %s)",
        config.PRE_DIGEST_TIME, config.PRE_DIGEST_DAY,
        config.POST_DIGEST_TIME, config.POST_DIGEST_DAY,
        config.GAMEDAY_TIME, config.GAMEDAY_DAY,
    )


def build_application() -> Application:
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pre", cmd_pre))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("waivers", cmd_waivers))
    app.add_handler(CommandHandler("drops", cmd_drops))
    app.add_handler(CommandHandler("needs", cmd_needs))
    app.add_handler(CommandHandler("roster", cmd_roster))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("player", cmd_player))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("gameday", cmd_gameday))
    # Any plain text that isn't a command → free-form Q&A. Registered last so
    # it never shadows the command handlers above.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    _register_jobs(app)
    return app
