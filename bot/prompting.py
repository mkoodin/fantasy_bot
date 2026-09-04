"""Question classification and the analysis directives every answer follows.

These used to live in the Telegram layer, which meant only chat questions got
them — the scheduled digests and start/sit calls went to Grok with a bare
prompt and reasoned from memory. They live here now, and `grok.answer_question`
applies them to every call, so there is exactly one path into the model and no
way for an entry point to skip the process.

The process itself is the same every time: price the decision from the league's
own data first, then let the live X and expert layer adjust that baseline, and
say what moved.
"""

import re

# The house rule. Prepended to every question regardless of topic.
CORE_DIRECTIVE = (
    "\n\n[HOW TO ANSWER — follow this every time, without being asked. "
    "(1) START FROM THE DATA IN CONTEXT. Every player carries a computed value "
    "score (0-100, points above a replacement-level waiver add in THIS "
    "league's scoring and starter requirements — comparable across positions), "
    "a market rank, projections, and the round this league drafted them. Those "
    "numbers are your baseline for any claim about who is better or what "
    "something is worth. (2) LAYER THE LIVE READ ON TOP. Search recent X "
    "discussion from the trusted voices and current expert rankings, and move a "
    "player off his baseline only where the reporting justifies it — role or "
    "snap-count change, injury, depth-chart shift, matchup or schedule swing. "
    "Name what moved him and why. (3) DECIDE. Give the call, with the number "
    "that drives it. Never rank or compare players from memory when the "
    "context already prices them; if you contradict the value score, say so "
    "out loud and justify it with what you found.]"
)

# --- Topic classifiers ------------------------------------------------------
# High-stakes, multi-factor decisions worth the flagship model.
_DEEP = re.compile(
    r"\b(trade|trading|optimal lineup|best lineup|set (?:my )?lineup|optimi[sz]e|"
    r"rest[- ]of[- ]season|\bros\b|playoff|keeper|who (?:do|should) i keep)\b",
    re.IGNORECASE,
)

_TRADE = re.compile(
    r"\b(trade|trading|traded|buy low|sell high|package|two[- ]for[- ]one|"
    r"swap|offer for|deal for|give up|worth it for)\b",
    re.IGNORECASE,
)

_DRAFT = re.compile(
    r"\b(draft|adp|mock|what round|which round|round \d|draft board|"
    r"draft position|draft rank|draft strateg|snake draft|auction value|"
    r"sleepers? to draft|early pick)\b",
    re.IGNORECASE,
)

# "streamer" and "weeks 15-17" both have to match here, which a trailing \b
# after the alternation would block — the boundary goes only where it's needed.
_MATCHUP = re.compile(
    r"\b(stream(?:s|ed|er|ers|ing)?|strength of schedule|sos\b|schedule|"
    r"matchup|easy run|favorable run|good run|tough stretch|soft stretch|"
    r"weeks? \d+|next few weeks|rest of (?:the )?season|playoff schedule)",
    re.IGNORECASE,
)

_START_SIT = re.compile(
    r"\b(start|sit|bench|flex|lineup|who do i play|play him|start him|"
    r"start or sit)\b",
    re.IGNORECASE,
)

_WAIVER = re.compile(
    r"\b(waivers?|faab|bids?|pick ?up|adds?|drops?|free agents?|fa\b|claims?|"
    r"stream(?:s|ed|er|ers|ing)?)",
    re.IGNORECASE,
)


def is_trade(text: str) -> bool:
    return bool(_TRADE.search(text))


def is_draft(text: str) -> bool:
    return bool(_DRAFT.search(text))


def is_matchup(text: str) -> bool:
    return bool(_MATCHUP.search(text))


def is_start_sit(text: str) -> bool:
    return bool(_START_SIT.search(text))


def is_waiver(text: str) -> bool:
    return bool(_WAIVER.search(text))


def wants_deep(text: str) -> bool:
    """True for questions that earn the flagship model."""
    return bool(_DEEP.search(text)) or is_trade(text) or is_draft(text)


# --- Topic directives -------------------------------------------------------
TRADE_DIRECTIVE = (
    "\n\n[TRADE — price both sides before proposing anything. Read the VALUE "
    "BOARD and YOUR TRADE POSTURE in the context: they already tell you what "
    "every player is worth and which of mine are untouchable versus "
    "expendable. Build offers from DEPTH and EXPENDABLE players first; a CORE "
    "player leaves my roster only for a clearly larger return, stated as "
    "numbers. Show the ledger explicitly — 'I send X (value N) for Y (value "
    "M)' — and total both sides. If the totals are far apart, fix the package "
    "by adding pieces rather than hand-waving the gap; the only way to justify "
    "a lopsided deal is a specific, sourced reason the market is wrong, which "
    "you must state. Then check the deal from the OTHER manager's side using "
    "their roster and needs: if they would obviously decline, say so and "
    "adjust. Finish with the opening offer to actually send and how to pitch "
    "it.]"
)

DRAFT_DIRECTIVE = (
    "\n\n[DRAFT/ADP — ground this in DATA, not memory. Live-search the CURRENT "
    "consensus ADP and industry rankings for the upcoming season (FantasyPros, "
    "Sleeper ADP, respected analysts) and anchor every round and tier claim to "
    "that consensus. Then add edge: flag MISPRICED ADP — undervalued players "
    "whose role, situation or buzz beats their cost, and reaches whose ADP "
    "exceeds their outlook — plus notable risers and fallers, one line of why "
    "each. Value THIS season only unless the league format says otherwise.]"
)

MATCHUP_DIRECTIVE = (
    "\n\n[MATCHUP/SCHEDULE — ground it in the real NFL schedule. Live-search "
    "the relevant weeks' matchups and each defense's rank against the position "
    "in question, including my playoff weeks when rest-of-season or playoff "
    "streaming is implied. Separate who is the best play THIS week from who "
    "has the best upcoming run, and name concrete streamers with the specific "
    "weeks they're best.]"
)

START_SIT_DIRECTIVE = (
    "\n\n[START/SIT — the context already contains a PROJECTION-OPTIMAL "
    "LINEUP: every slot filled with the highest-projected eligible player for "
    "THIS week, injury-discounted. Start from it rather than re-deriving it, "
    "and spend your search on what it cannot know — snap counts, practice "
    "reports, weather, defensive matchup, role changes. Go slot by slot; for "
    "each one either confirm the projected starter or name the swap and the "
    "specific finding that justifies overriding the number. Resolve the CLOSE "
    "CALLS the context flags, since those are where news actually changes the "
    "answer. Distinguish floor from ceiling: if I'm favored this week lean to "
    "floor, if I'm the underdog lean to ceiling.]"
)

WAIVER_DIRECTIVE = (
    "\n\n[WAIVERS/FAAB — recommend only players NOT on any roster in the "
    "context. Two lists are provided and they answer different questions: the "
    "WAIVER WIRE board ranks everyone available by value and states the "
    "'upgrade' each would give MY starting lineup, while TRENDING ADDS shows "
    "what the market is chasing. Lead with upgrade, not add volume — a hot "
    "name with a negative upgrade would sit on my bench, and a quiet player "
    "with a positive upgrade starts immediately. When the two lists disagree, "
    "say which you believe and why; heavy adds usually mean news broke, so "
    "check what it was before dismissing it. Size FAAB bids to what the player "
    "is worth to MY lineup, then adjust for who can outbid me — name the "
    "rivals with both budget and a hole at that position. Say who to drop, and "
    "confirm the drop is DEPTH or EXPENDABLE; never a CORE player, and never "
    "an injured starter worth stashing.]"
)


def directives_for(question: str) -> str:
    """The full instruction block for one question: house rule plus topics.

    Topic directives stack — 'should I trade for a streaming QB before the
    playoffs' is legitimately a trade, matchup and waiver question at once.
    """
    parts = [CORE_DIRECTIVE]
    if is_trade(question):
        parts.append(TRADE_DIRECTIVE)
    if is_draft(question):
        parts.append(DRAFT_DIRECTIVE)
    if is_matchup(question):
        parts.append(MATCHUP_DIRECTIVE)
    if is_start_sit(question):
        parts.append(START_SIT_DIRECTIVE)
    if is_waiver(question):
        parts.append(WAIVER_DIRECTIVE)
    return "".join(parts)
