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
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from . import config

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
        "whom), each team's remaining FAAB, and notable available free agents. "
        "Use it as ground truth:\n"
        + (team_context or "(league context unavailable)")
        + "\n\nRules: Recommend free-agent adds only from players NOT on any "
        "roster above. For waiver/FAAB bids, size the bid to win given rivals' "
        "remaining FAAB, and call out which opponents are likely to compete "
        "(they have budget AND a hole at that position). For trades, only "
        "propose players actually on another team's roster, and target managers "
        "whose roster needs complement yours. Be concise and decision-oriented; "
        "if you make a start/sit, add/drop, bid, or trade call, state it clearly "
        "with a one-line why. Write plain text prose only — no markdown, "
        "asterisks, headers, or bracketed citations. Keep it under ~180 words "
        "unless the question truly needs more."
    )


def _post(instructions: str, input_messages: list, model: str) -> dict:
    """Blocking POST to the Responses API. Runs in a worker thread."""
    body = {
        "model": model,
        "instructions": instructions,
        "input": input_messages,
        "tools": _tools(),
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
    text = _clean(_extract_text(data)) or "No usable response from Grok."
    return {"text": text, "citations": _extract_citations(data)}


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
    deep: bool = False,
    history: Optional[list] = None,
) -> Optional[dict]:
    """Answer a free-form question about the user's team, with optional prior
    conversation turns so follow-ups keep context. {'text', 'citations'}."""
    messages = list(history or []) + [{"role": "user", "content": question}]
    return await _run(_answer_instructions(team_context), messages, deep=deep)


async def buzz_line(player_name: str, extra_context: str = "") -> str:
    """Compact one-block buzz suitable for embedding in a digest."""
    result = await analyze_player(player_name, extra_context)
    if result is None:
        return ""
    return result["text"]
