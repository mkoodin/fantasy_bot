"""Compose human-facing digests from the analysis layer.

All messages use Telegram HTML parse mode. Dynamic text is escaped via
`esc()` so player names with '&' or '<' don't break rendering.
"""

import html
from typing import Optional

from . import analysis, config, grok
from .sleeper import SleeperClient, is_out, player_name

TELEGRAM_LIMIT = 4096


def esc(value) -> str:
    return html.escape(str(value))


def _pos_tag(p: dict) -> str:
    pos = p.get("position") or "?"
    team = p.get("team") or "FA"
    tag = f"{pos}·{team}"
    if is_out(p):
        tag += f" ⛔{p.get('injury_status')}"
    return tag


def _split_long_line(line: str) -> list[str]:
    """Break a single over-long line into pieces that each fit the cap.

    Grok is instructed to write unbroken prose with no headers or markdown, so
    a long answer routinely arrives as one paragraph with no newline to split
    on. Splits on spaces where possible; a single token longer than the cap
    (a pathological URL) is cut mid-word because nothing else can be done.
    """
    out: list[str] = []
    current = ""
    for word in line.split(" "):
        while len(word) > TELEGRAM_LIMIT:
            if current:
                out.append(current)
                current = ""
            out.append(word[:TELEGRAM_LIMIT])
            word = word[TELEGRAM_LIMIT:]
        candidate = f"{current} {word}" if current else word
        if len(candidate) > TELEGRAM_LIMIT:
            out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def split_for_telegram(text: str) -> list[str]:
    """Split a message into parts that each fit Telegram's 4096-char cap.

    Splits on line boundaries where it can and inside a line where it must.
    Never emits an empty part: Telegram rejects an empty message outright,
    which reaches the user as an unexplained failure rather than a long answer.
    """
    if not text.strip():
        return []
    if len(text) <= TELEGRAM_LIMIT:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        pieces = _split_long_line(line) if len(line) > TELEGRAM_LIMIT else [line]
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) > TELEGRAM_LIMIT:
                if current:
                    chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def _offseason_note(ctx: analysis.LeagueContext) -> str:
    """Banner clarifying that digests are placeholder data in the offseason."""
    if not analysis.is_offseason(ctx):
        return ""
    try:
        nxt = int(ctx.season) + 1
    except (TypeError, ValueError):
        nxt = ""
    return (
        f"\n⚠️ <i>Offseason — this reflects your completed {esc(ctx.season)} "
        f"league. Live waivers &amp; start/sit resume when the {nxt} season "
        "starts.</i>\n"
    )


def _header(ctx: analysis.LeagueContext, title: str) -> str:
    team = esc(ctx.team_name(ctx.my_user_id))
    return (
        f"🏈 <b>{esc(title)}</b>\n"
        f"<i>{esc(ctx.league.get('name', 'League'))} · Week {ctx.week} · "
        f"{team}</i>\n"
        f"💰 FAAB left: <b>${ctx.faab_remaining}</b> of ${ctx.faab_total}\n"
        + _offseason_note(ctx)
    )


def _needs_block(ctx: analysis.LeagueContext) -> str:
    needs = analysis.positional_needs(ctx)
    if not needs:
        return "\n<b>Roster health:</b> no glaring holes — you're set at starters.\n"
    lines = ["\n<b>Roster needs:</b>"]
    for n in needs[:4]:
        lines.append(f"• <b>{esc(n['position'])}</b> — {esc(n['reason'])}")
    return "\n".join(lines) + "\n"


async def _faab_block(ctx: analysis.LeagueContext, client: SleeperClient) -> str:
    recs = await analysis.faab_recommendations(ctx, client, limit=6)
    if not recs:
        return "\n<b>Waiver targets:</b> nothing trending that you don't already have.\n"
    lines = ["\n<b>🎯 Waiver targets & FAAB bids:</b>"]
    for r in recs:
        p = r["player"]
        # The star marks a real starting upgrade, not merely a thin position:
        # flagging a "need" the player wouldn't actually start for is noise.
        star = "⭐" if r["upgrade"] > 0 else "•"
        if r["bid"] == 0:
            price = "<b>free add / $1 claim</b>"
        else:
            price = f"bid <b>${r['bid']}–${r['bid_high']}</b> (~{r['pct']}% budget)"
        line = (
            f"{star} <b>{esc(player_name(p))}</b> ({esc(_pos_tag(p))}) — {price}\n"
            f"   <i>{esc(r['reason'])}</i>"
        )
        drop_pid = r.get("drop_player_id")
        if drop_pid:
            dropped = ctx.players.get(drop_pid) or {}
            if r.get("beats_drop"):
                line += (
                    f"\n   ↳ <i>drop {esc(player_name(dropped))} "
                    f"({r['drop_value']}) for him ({r['add_value']})</i>"
                )
            else:
                line += (
                    f"\n   ↳ <i>SKIP — worse than {esc(player_name(dropped))} "
                    f"({r['add_value']} vs {r['drop_value']}), your own worst "
                    "rosterable player</i>"
                )
        lines.append(line)
    return "\n".join(lines) + "\n"


async def _drop_block(ctx: analysis.LeagueContext, client: SleeperClient) -> str:
    drops = await analysis.drop_candidates(ctx, client, limit=4)
    if not drops:
        return ""
    lines = ["\n<b>✂️ Drop candidates (to make room):</b>"]
    for d in drops:
        p = d["player"]
        lines.append(
            f"• <b>{esc(player_name(p))}</b> ({esc(_pos_tag(p))}) — "
            f"<i>{esc(d['reason'])}</i>"
        )
    return "\n".join(lines) + "\n"


async def _grok_pickup_analysis(
    ctx: analysis.LeagueContext, client: SleeperClient
) -> str:
    """League-tuned analysis of the top available targets: which to prioritize
    for THIS team's scoring/construction/playoff picture, refined bids, live
    buzz, and who to drop. Replaces generic per-player buzz so the scheduled
    digest reasons in your exact rules, not the raw market signal."""
    if not config.ENABLE_GROK:
        return ""
    recs = await analysis.faab_recommendations(ctx, client, limit=6)
    if not recs:
        return ""
    targets = "; ".join(
        f"{player_name(r['player'])} ({r['position']}, {r['adds']:,} adds/48h, "
        f"market bid ${r['bid']}-{r['bid_high']})"
        for r in recs
    )
    full_ctx = await analysis.full_league_context(ctx, client)
    question = (
        "These are this week's top trending-available pickups for my team: "
        f"{targets}. Using MY exact scoring, roster construction (FLEX/QB/DEF "
        "rules), current needs, remaining FAAB, and playoff picture, tell me "
        "which 2-3 to actually prioritize and why they fit MY league (not "
        "generic), refine the FAAB bid if the market number looks off, note any "
        "to skip, and who on my roster to drop to make room. Add a quick live "
        "buzz check on the top target. Keep it tight — a couple sentences per "
        "priority pickup."
    )
    result = await grok.answer_question(question, full_ctx, deep=False)
    if not result or result["text"].startswith("⚠️"):
        return ""
    return (
        "\n<b>🔎 Tuned pickup analysis (your scoring &amp; build):</b>\n"
        + esc(result["text"])
        + "\n"
    )


async def build_pre_waiver_digest(
    ctx: analysis.LeagueContext, client: SleeperClient, with_buzz: bool = True
) -> str:
    parts = [
        _header(ctx, "Pre-Waiver Digest"),
        _needs_block(ctx),
        await _faab_block(ctx, client),
        await _drop_block(ctx, client),
    ]
    if with_buzz:
        parts.append(await _grok_pickup_analysis(ctx, client))
    parts.append(
        "\n<i>Bids are % of your remaining FAAB, weighted for needs. "
        "Adjust for how badly you want the player.</i>"
    )
    return "".join(parts)


async def build_post_waiver_digest(
    ctx: analysis.LeagueContext, client: SleeperClient
) -> str:
    """After waivers process: recap league activity + what to target next on
    the free-agent wire."""
    parts = [_header(ctx, "Post-Waiver Digest")]

    # Recap this week's completed waiver/free-agent moves across the league.
    try:
        txns = await client.get_transactions(ctx.league["league_id"], ctx.week)
    except Exception:
        txns = []
    moves = [
        t for t in txns
        if t.get("status") == "complete" and t.get("type") in {"waiver", "free_agent"}
    ]
    if moves:
        lines = ["\n<b>📋 Waivers processed:</b>"]
        for t in moves[:10]:
            adds = t.get("adds") or {}
            drops = t.get("drops") or {}
            bid = (t.get("settings") or {}).get("waiver_bid")
            roster_id = (t.get("roster_ids") or [None])[0]
            owner = _roster_owner_name(ctx, roster_id)
            for pid in adds:
                p = ctx.players.get(pid, {})
                bid_txt = f" (${bid})" if bid is not None else ""
                lines.append(
                    f"• {esc(owner)} added <b>{esc(player_name(p))}</b>{bid_txt}"
                )
            for pid in drops:
                p = ctx.players.get(pid, {})
                lines.append(f"   ↳ dropped {esc(player_name(p))}")
        parts.append("\n".join(lines) + "\n")
    else:
        parts.append("\n<i>No completed waiver claims found for this week yet.</i>\n")

    # What's now available and worth chasing on the open wire.
    parts.append(_needs_block(ctx))
    parts.append(await _faab_block(ctx, client))
    parts.append(await _grok_pickup_analysis(ctx, client))
    parts.append(
        "\n<i>Still-available free agents you can grab now (FAAB or first-come, "
        "per your league's waiver settings).</i>"
    )
    return "".join(parts)


def _roster_owner_name(ctx: analysis.LeagueContext, roster_id: Optional[int]) -> str:
    if roster_id is None:
        return "Someone"
    for r in ctx.rosters:
        if r.get("roster_id") == roster_id:
            return ctx.team_name(r.get("owner_id", ""))
    return "Someone"


async def build_start_sit(
    ctx: analysis.LeagueContext, client: SleeperClient, final: bool = False
) -> str:
    """Start/sit recommendations for the week. Friday version sets the optimal
    lineup with close calls; the Sunday 'final' version is a last-minute check
    focused on confirmed inactives and late injury news."""
    title = "Sunday Final Lineup Check" if final else "Friday Start/Sit"
    header = _header(ctx, title)
    if not config.ENABLE_GROK:
        # Fall back to a plain injury sweep when Grok isn't configured.
        return await build_gameday_alert(ctx, client)

    full_ctx = await analysis.full_league_context(ctx, client)
    opp = await analysis.opponent_context(ctx, client, final=final)
    if opp:
        full_ctx += "\n\n" + opp
    if final:
        extra = (
            " This is a final pre-kickoff check: prioritize confirmed inactives, "
            "late injury designations, and weather, and give me any swaps to make "
            "before the early games."
        )
    else:
        extra = (
            " Include close start/sit calls, favorable/tough matchups, and any "
            "injuries or workload notes to monitor into the weekend."
        )
    matchup = (
        " Factor in this week's opponent (in the context): if I'm favored, lean "
        "to safe floor plays; if I'm the underdog, favor higher-ceiling boom/bust "
        "options, and flag any floor-vs-ceiling swaps the matchup calls for."
        if opp
        else ""
    )
    question = (
        f"Set my optimal starting lineup for Week {ctx.week}. Go slot by slot: "
        "name who to start with a one-line why, then list the tough bench/close "
        "calls I should double-check." + matchup + extra
    )
    result = await grok.answer_question(question, full_ctx, deep=True)
    if not result:
        return header + "\nNo response.\n"
    return header + "\n" + esc(result["text"])


async def build_gameday_alert(
    ctx: analysis.LeagueContext, client: SleeperClient
) -> str:
    """Sunday sweep: flag your starters who are Out/Doubtful/IR so you can
    swap them before kickoff."""
    grouped = analysis.my_players_by_position(ctx)
    flagged = []
    for players in grouped.values():
        for p in players:
            status = p.get("injury_status")
            if status and status not in {"", "Probable"}:
                flagged.append((p, status))
    header = _header(ctx, "Gameday Injury Sweep")
    if not flagged:
        return header + "\n✅ No injury flags on your roster. Good to go.\n"
    lines = ["\n<b>⚠️ Check these before kickoff:</b>"]
    for p, status in flagged:
        lines.append(
            f"• <b>{esc(player_name(p))}</b> ({esc(_pos_tag(p))}) — {esc(status)}"
        )
    return header + "\n".join(lines) + "\n"
