"""xAI Grok client using the Agent Tools API (Responses endpoint).

For a given player we ask Grok to search *recent* X posts (beat reporters
break snap-count / injury / role news there first) plus the web, and summarize
the fantasy-relevant takeaway with a credibility read. xAI runs the searches
server-side via the `x_search` and `web_search` tools and returns the final
answer with citations.

Note: xAI's older Live Search (`search_parameters`) is retired and now returns
HTTP 410 — this module targets POST /v1/responses instead. We call it directly
with `requests` (no SDK) so xAI-specific tool types pass through untouched.
Docs: https://docs.x.ai/docs/guides/tools/overview
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from . import config, prompting

logger = logging.getLogger("fantasy_bot")

# Matches markdown links: [text](url) and citation forms like [[1]](url).
_MD_LINK = re.compile(r"\[+([^\]]*)\]+\((https?://[^)]+)\)")


def _clean(text: str) -> str:
    """Strip markdown Grok sometimes emits despite instructions, so Telegram
    HTML renders cleanly. Citations are surfaced separately, so drop inline
    numeric markers; keep meaningful link labels as plain text."""
    def repl(m: "re.Match") -> str:
        label = m.group(1).strip()
        return "" if (not label or label.isdigit()) else label

    text = _MD_LINK.sub(repl, text)
    text = re.sub(r"\[+\d+\]+", "", text)   # leftover [[1]] / [1]
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _format_directive() -> str:
    fmt = (config.LEAGUE_FORMAT or "redraft").lower()
    if fmt == "redraft":
        return (
            "LEAGUE FORMAT: REDRAFT (single season). Judge everything by "
            "THIS-season impact. Favor players who help win now — immediate role, "
            "target/touch share, favorable near-term schedule. Actively discount "
            "dynasty-only value: rookie stashes, 'buy for next year', long-term "
            "upside that won't pay off this season. If a source is dynasty-framed, "
            "translate it to redraft — does this help my lineup in the coming weeks?"
        )
    if fmt == "dynasty":
        return (
            "LEAGUE FORMAT: DYNASTY. Weigh multi-year and rookie value alongside "
            "current production."
        )
    return f"LEAGUE FORMAT: {config.LEAGUE_FORMAT}."


def _trusted_directive() -> str:
    if not config.X_TRUSTED_HANDLES:
        return ""
    handles = ", ".join(f"@{h}" for h in config.X_TRUSTED_HANDLES)
    return (
        "TRUSTED VOICES: weight analysis from these accounts heavily when it "
        f"appears in your search — {handles}. Still trust verified NFL beat "
        "reporters and official injury reports for breaking news even if not "
        "listed."
    )


def _instructions() -> str:
    return (
        "You are a sharp NFL fantasy football analyst. Using the X posts and web "
        "results you searched, give a concise, decision-oriented read for the "
        "player asked about. "
        + _format_directive()
        + " "
        + _trusted_directive()
        + " Prioritize credible sources over hype. Cover, only if relevant: "
        "injury/practice status, snap-count or role change, matchup, and whether "
        "they're worth a waiver/FAAB add THIS week. Be direct. If the buzz is thin "
        "or just noise, say so. Keep it under 140 words. Write plain text prose "
        "only — NO markdown: no **bold** or asterisks, no headers, and no inline "
        "links or bracketed citation markers like [[1]]. End with one line: "
        "'Verdict: <ADD / STASH / HOLD / PASS> — <short why>'."
    )


def _tools() -> list[dict]:
    from_date = (
        datetime.now(timezone.utc) - timedelta(days=config.GROK_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")
    x_search: dict = {"type": "x_search", "from_date": from_date}
    if config.X_RESTRICT_TO_HANDLES and config.X_TRUSTED_HANDLES:
        # allowed_x_handles caps at 20 per the API.
        x_search["allowed_x_handles"] = config.X_TRUSTED_HANDLES[:20]
    return [x_search, {"type": "web_search"}]


def _extract_text(data: dict) -> str:
    """Pull the assistant's text out of a Responses API payload."""
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for item in data.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for chunk in item.get("content", []) or []:
            if chunk.get("type") in ("output_text", "text") and chunk.get("text"):
                parts.append(chunk["text"])
    return "\n".join(parts).strip()


def _extract_citations(data: dict) -> list[str]:
    cites = data.get("citations")
    if isinstance(cites, list) and cites:
        return [c for c in cites if c]
    # Fallback: some responses attach source URLs as content annotations.
    urls: list[str] = []
    for item in data.get("output", []) or []:
        for chunk in item.get("content", []) or []:
            for ann in chunk.get("annotations", []) or []:
                url = ann.get("url") or ann.get("source")
                if url and url not in urls:
                    urls.append(url)
    return urls


def _answer_instructions(team_context: str) -> str:
    """System prompt for free-form questions about the user's own team."""
    return (
        "You are a personalized NFL fantasy football assistant for the user's "
        "specific team. Answer their question directly and practically, using the "
        "X posts and web results you searched for current info. "
        + _format_directive()
        + " "
        + _trusted_directive()
        + "\n\nLive league context — the user's team, ALL rosters (who owns "
        "whom), a VALUE BOARD pricing the top players at each position, the "
        "user's TRADE POSTURE (which of their players are untouchable vs. "
        "expendable), each team's remaining FAAB, and notable available free "
        "agents. Every player carries value signals in brackets. The most "
        "important is 'val': a 0-100 score measuring projected points ABOVE "
        "the replacement-level player at that position, computed from this "
        "league's own scoring and starter requirements and discounted for "
        "injury. Because it is measured against replacement, it is comparable "
        "ACROSS positions — a QB and a RB with equal scores are equally "
        "valuable to trade, even though the QB scores more raw points, and a "
        "val of 0 means freely replaceable off waivers. The brackets also "
        "carry market rank (lower = better), raw projected points, and the "
        "round this league drafted the player. Use all of it as ground "
        "truth:\n"
        + (team_context or "(league context unavailable)")
        + "\n\nRules: Always answer in FANTASY terms — not general NFL talk. "
        "ALWAYS apply THIS league's exact scoring, roster settings (FLEX count, "
        "no kicker, etc.) and playoff schedule to every answer automatically — "
        "the user should never have to say 'in my scoring.' Never give generic, "
        "league-agnostic advice; tailor everything to the settings above. "
        "When a specific player comes up, always cover: (1) their fantasy value "
        "THIS season and how soon they help (this week vs. later / rest-of-"
        "season), (2) whether they're rostered in THIS league or a free agent "
        "(check the rosters above), and (3) if available, whether it's a free "
        "pickup or a waiver claim, with a suggested FAAB bid sized to win vs. "
        "rivals' budgets; if rostered, name who holds them and whether they're a "
        "trade target. Recommend free-agent adds only from players NOT on any "
        "roster above. For waiver/FAAB bids, size the bid to win given rivals' "
        "remaining FAAB, and call out which opponents are likely to compete "
        "(they have budget AND a hole at that position). Flag injury-driven "
        "opportunities (e.g. a hurt starter making a handcuff a must-add) and "
        "how long the window lasts. For trades, only propose players actually on "
        "another team's roster, and target managers whose needs complement "
        "yours. PRICE EVERY TRADE before proposing it: total the val scores on "
        "each side, adjust with what your search actually found (role, injury, "
        "snap counts, expert rankings, the trusted voices' takes), and state "
        "both sides' value in the answer. Never propose sending a clearly "
        "more valuable player for a less valuable one without naming the "
        "specific, sourced reason the market is wrong; build offers from the "
        "user's DEPTH and EXPENDABLE players, never their CORE, unless the "
        "return is clearly larger and you show the math. "
        "Be concise and decision-oriented; "
        "if you make a start/sit, add/drop, bid, or trade call, state it clearly "
        "with a one-line why. Write plain text prose only — no markdown, "
        "asterisks, headers, or bracketed citations. Keep it under ~180 words "
        "unless the question truly needs more — a trade call that has to show "
        "both sides' value earns the extra room."
    )


def _post(instructions: str, input_messages: list, model: str) -> dict:
    """Blocking POST to the Responses API. Runs in a worker thread."""
    body = {
        "model": model,
        "instructions": instructions,
        "input": input_messages,
        "tools": _tools(),
        "temperature": config.GROK_TEMPERATURE,
    }
    resp = requests.post(
        f"{config.XAI_BASE_URL}/responses",
        headers={
            "Authorization": f"Bearer {config.XAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=config.GROK_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"xAI {resp.status_code}: {resp.text[:300]}")
    return resp.json()


async def _run(
    instructions: str, input_messages: list, deep: bool = False
) -> Optional[dict]:
    """Shared entry: returns {'text', 'citations'} or None if Grok is off."""
    if not config.ENABLE_GROK:
        return None
    model = config.GROK_MODEL_DEEP if deep else config.GROK_MODEL
    try:
        data = await asyncio.to_thread(_post, instructions, input_messages, model)
    except requests.exceptions.Timeout:
        return {
            "text": "⚠️ That took too long to research. Try a more specific "
            "question (one position or one decision) and ask again.",
            "citations": [],
        }
    except Exception as exc:  # network / auth / quota — surface, don't crash
        low = str(exc).lower()
        if "incorrect api key" in low or "401" in low or (
            "400" in low and "api key" in low
        ):
            return {
                "text": "⚠️ xAI rejected the API key. Check that XAI_API_KEY in "
                "Railway → Variables matches your key exactly — no quotes, no "
                "spaces, and the full string.",
                "citations": [],
            }
        return {"text": f"⚠️ Grok lookup failed: {exc}", "citations": []}
    # Parsing runs outside the request try/except above, so guard it too: an
    # unexpected payload shape must not surface as an unexplained failure.
    try:
        text = _clean(_extract_text(data)) or "No usable response from Grok."
        citations = _extract_citations(data)
    except Exception as exc:
        logger.warning("Could not parse xAI response: %s", exc)
        return {
            "text": "⚠️ Grok returned a response I couldn't read. Try asking "
            "again, or narrow the question.",
            "citations": [],
        }
    return {"text": text, "citations": citations}


async def analyze_player(
    player_name: str, extra_context: str = "", deep: bool = False
) -> Optional[dict]:
    """Live buzz on a single player. {'text', 'citations'} or None if off."""
    ctx_line = f" Context: {extra_context}." if extra_context else ""
    user_msg = (
        f"What's the latest fantasy-relevant buzz on {player_name} (NFL)?"
        f"{ctx_line} Focus on the last few days."
    )
    return await _run(_instructions(), [{"role": "user", "content": user_msg}], deep=deep)


async def answer_question(
    question: str,
    team_context: str = "",
    deep: Optional[bool] = None,
    history: Optional[list] = None,
) -> Optional[dict]:
    """Answer a free-form question about the user's team, with optional prior
    conversation turns so follow-ups keep context. {'text', 'citations'}.

    Every caller routes through here, and the analysis directives are attached
    here rather than by the caller, so a chat question, a scheduled digest and
    a start/sit call all get the same process.

    `deep` left as None picks the model from the question — high-stakes topics
    escalate on their own. A caller that passes True or False means it: the
    scheduled digests run several times a week and choose the cheaper model
    deliberately, so auto-escalation must not override them.
    """
    directed = question + prompting.directives_for(question)
    # History keeps the user's original wording — directives are per-call
    # scaffolding, not part of the conversation.
    messages = list(history or []) + [{"role": "user", "content": directed}]
    if deep is None:
        deep = prompting.wants_deep(question)
    return await _run(_answer_instructions(team_context), messages, deep=deep)


async def buzz_line(player_name: str, extra_context: str = "") -> str:
    """Compact one-block buzz suitable for embedding in a digest."""
    result = await analyze_player(player_name, extra_context)
    if result is None:
        return ""
    return result["text"]


def _news_instructions(team_context: str) -> str:
    """System prompt for the breaking-news sweep.

    Deliberately narrow. The value of this alert is being early and being
    right about MY league — a general NFL news roundup is worse than silence,
    because it trains you to ignore the next one.
    """
    return (
        "You are an NFL fantasy news scout for one specific team. Your job is "
        "to find news from the last few hours that creates an ACTIONABLE roster "
        "move in THIS league, before the other managers act on it. "
        + _format_directive()
        + " "
        + _trusted_directive()
        + "\n\nLive league context — rosters, who owns whom, the waiver board "
        "with each free agent's value and the upgrade he'd give this team, and "
        "the league's rules:\n"
        + (team_context or "(league context unavailable)")
        + "\n\nWhat counts as actionable, in priority order: (1) an injury, "
        "inactive, or IR move to ANY starter in this league whose backup or "
        "handcuff is a FREE AGENT here — name the beneficiary and say to grab "
        "him now; (2) a role, snap-count or depth-chart change that makes a "
        "FREE AGENT here startable; (3) news affecting a player on MY roster "
        "that changes whether I start, stash or drop him; (4) a suspension, "
        "holdout or trade that opens a job. "
        "\n\nRules: only report a pickup if the player is genuinely NOT on any "
        "roster in the context above. Say whether he is a free add or needs a "
        "waiver claim, and how urgent it is — before waivers process, before "
        "kickoff, or whenever. Cite what broke and roughly when. Do NOT report "
        "general injury news with no free-agent consequence here, do NOT repeat "
        "well-known season-long situations, and do NOT pad. Two or three real "
        "items beat ten filler ones. "
        "\n\nIf nothing in the window is genuinely actionable for this team, "
        "reply with exactly: NOTHING ACTIONABLE. Say that rather than "
        "manufacturing an alert — a false alarm costs more than a miss. "
        "\n\nWrite plain text prose only — no markdown, asterisks, headers or "
        "bracketed citations. Lead each item with the player name. Keep the "
        "whole thing under 200 words."
    )


async def breaking_news(team_context: str, lookback_hours: int = 5) -> Optional[dict]:
    """Scan X and the news for roster moves this league can act on right now."""
    question = (
        f"Scan X and the web for NFL news from the LAST {lookback_hours} HOURS: "
        "injuries, inactives, IR moves, practice reports, snap-count and "
        "depth-chart changes, suspensions, and backfield or target-share news. "
        "Report only what creates an actionable add, start/sit or stash "
        "decision for my team in my league right now."
    )
    return await _run(
        _news_instructions(team_context),
        [{"role": "user", "content": question}],
        deep=False,
    )
