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
    "<b>/player &lt;name&gt;</b> — outlook + availability + FAAB bid for any player\n"
    "<b>/startsit</b> — optimal lineup + start/sit calls for the week\n"
    "<b>/trade</b> — find an ideal trade: fair deal + opening offer + how to pitch\n"
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
    # Route through the full-context brain so the answer covers this-season
    # value, availability in YOUR league, and a free-pickup vs. FAAB-bid call.
    question = (
        f"{name}: give me the fantasy outlook for this season and how "
        "immediately they help, whether they're available in my league (free "
        "pickup or waiver claim with a suggested winning FAAB bid), or if "
        "rostered, who holds them."
    )
    await _answer(update, context, question, deep=False)


# High-stakes questions worth the flagship model (multi-factor decisions).
_DEEP_PATTERNS = re.compile(
    r"\b(trade|trading|optimal lineup|best lineup|set (?:my )?lineup|optimi[sz]e|"
    r"rest[- ]of[- ]season|\bros\b|playoff|keeper|who (?:do|should) i keep)\b",
    re.IGNORECASE,
)


def _wants_deep(text: str) -> bool:
    return bool(_DEEP_PATTERNS.search(text))


# Draft/ADP questions — ungrounded without a live ADP/rankings search.
_DRAFT_PATTERNS = re.compile(
    r"\b(draft|adp|mock|what round|which round|round \d|draft board|"
    r"draft position|draft rank|draft strateg|snake draft|auction value|"
    r"sleepers? to draft|early pick)\b",
    re.IGNORECASE,
)


def _is_draft(text: str) -> bool:
    return bool(_DRAFT_PATTERNS.search(text))


_DRAFT_DIRECTIVE = (
    "\n\n[DRAFT/ADP QUESTION — ground this in DATA, not memory. FIRST live-search "
    "the CURRENT consensus ADP and industry rankings for the upcoming season "
    "(e.g. FantasyPros, Sleeper ADP, respected analysts), then tier and rank "
    "players strictly from that current consensus. Anchor every round/tier claim "
    "to it, and call out players whose value has recently risen or fallen. It's "
    "redraft — value THIS season only.]"
)


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
    # Draft/ADP questions are ungrounded from memory — force the flagship and
    # make it pull current consensus ADP/rankings first. Keep the user's
    # original wording in history; only the Grok call sees the directive.
    grok_question = question
    if _is_draft(question):
        deep = True
        grok_question = question + _DRAFT_DIRECTIVE
    await _typing(update)
    note = "🔎 On it — searching X + news…"
    if deep:
        note += " (deep analysis with grok-4.5 — this can take a minute or two)"
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
        grok_question, full_ctx, deep=deep, history=list(history)
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
        f"SLEEPER_USER_ID: {yn(config.SLEEPER_USER_ID)}"
        + (f" <code>{digest.esc(config.SLEEPER_USER_ID)}</code>" if config.SLEEPER_USER_ID else ""),
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
    except Exception as exc:
        lines.append(f"\n❌ League load failed: <code>{digest.esc(exc)}</code>")
    await _send(update, "\n".join(lines))


@authorized_only
async def cmd_startsit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """On-demand start/sit: optimal lineup for the week."""
    await _typing(update)
    await update.effective_chat.send_message("🔎 Setting your optimal lineup…")
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
    if not _is_day(config.PRE_DIGEST_DAY):
        return
    ctx = await _ctx(force=True)
    await _push(context, await digest.build_pre_waiver_digest(ctx, client))


async def job_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_day(config.POST_DIGEST_DAY):
        return
    ctx = await _ctx(force=True)
    await _push(context, await digest.build_post_waiver_digest(ctx, client))


async def job_startsit(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_day(config.STARTSIT_DAY):
        return
    ctx = await _ctx(force=True)
    await _push(context, await digest.build_start_sit(ctx, client, final=False))


async def job_gameday(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_day(config.GAMEDAY_DAY):
        return
    ctx = await _ctx(force=True)
    # Sunday is the last-minute check: full start/sit weighted to late news.
    await _push(context, await digest.build_start_sit(ctx, client, final=True))


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


def _register_jobs(app: Application) -> None:
    jq = app.job_queue
    # Each job runs daily at its time but guards on the target weekday, so we
    # never depend on any library's day-index convention.
    jq.run_daily(job_pre, time=config.PRE_DIGEST_TIME, name="pre_waiver")
    jq.run_daily(job_post, time=config.POST_DIGEST_TIME, name="post_waiver")
    jq.run_daily(job_startsit, time=config.STARTSIT_TIME, name="friday_startsit")
    jq.run_daily(job_gameday, time=config.GAMEDAY_TIME, name="sunday_final")
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
    """Global safety net: log any unhandled error and tell the user, so a
    failure (or a mid-request restart) never leaves them hanging silently."""
    logger.exception("Unhandled handler error", exc_info=context.error)
    chat_id = config.TELEGRAM_CHAT_ID
    if isinstance(update, Update) and update.effective_chat:
        chat_id = update.effective_chat.id
    if not chat_id:
        return
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Something interrupted that — please try again in a moment.",
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
    app.add_handler(CommandHandler("player", cmd_player))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CommandHandler("startsit", cmd_startsit))
    app.add_handler(CommandHandler("gameday", cmd_gameday))
    # Any plain text that isn't a command → free-form Q&A. Registered last so
    # it never shadows the command handlers above.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    _register_jobs(app)
    return app
