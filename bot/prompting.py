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
    "a market rank, projections, his depth-chart position, and the round this "
    "league drafted him. Those are your baseline for any claim about who is "
    "better or what something is worth. (2) LAYER THE LIVE READ ON TOP. Search "
    "recent X discussion from the trusted voices and current expert rankings, "
    "and move a player off his baseline only where the reporting justifies it. "
    "Name what moved him and why. (3) DECIDE. Give the call, with the number "
    "that drives it. Never rank or compare players from memory when the "
    "context already prices them; if you contradict the value score, say so "
    "out loud and justify it.]"
    "\n\n[WHAT ACTUALLY PREDICTS — reason like an analyst, not a box-score "
    "reader. OPPORTUNITY BEATS EFFICIENCY. Snap share, routes run, target "
    "share, carries, and especially red-zone and goal-line work are the "
    "foundation; yards per carry and catch rate swing wildly and regress. A "
    "player whose usage is rising has a stronger case than one whose points "
    "came from a broken play. "
    "USAGE LEADS PRODUCTION BY ONE TO TWO WEEKS — this is the edge. When snaps, "
    "routes or targets climb before the fantasy points arrive, that is the "
    "buying window, and it closes once the box score catches up and everyone "
    "else sees it. Hunt for it explicitly. "
    "ROLE IS THE QUESTION, NOT TALENT. Ask what job he has and whether "
    "anything changed it: a depth-chart move, an injury ahead of him, a "
    "coaching-staff comment, a committee resolving. A first-string listing "
    "the market has not repriced is more actionable than a famous name in a "
    "timeshare. "
    "GAME SCRIPT SHAPES VOLUME. A team favored by two touchdowns runs; a team "
    "trailing throws. Weigh the implied team total and spread when they push a "
    "player's role one way. "
    "SAMPLE SIZE AND REGRESSION. One big game is noise. Touchdown rates and "
    "yards per touch regress hard; volume and route participation persist. "
    "Discount a hot week built on scores that will not repeat, and do not "
    "abandon a player whose role is intact after a quiet week. "
    "AVAILABILITY IS A PREREQUISITE. A player on bye or ruled out scores zero "
    "regardless of talent — the context flags this, never start him. "
    "BENCH SPOTS ARE NOT SMALL STARTING SPOTS. Judge a starter on expected "
    "points and reliability; judge a bench player on the chance he becomes "
    "something. A backup who inherits a job the moment one thing goes wrong is "
    "worth more than a veteran you will never start, even though the veteran "
    "outprojects him every week — the context gives this as contingent value. "
    "The last roster spots are working capital: churn them. "
    "GET THERE FIRST. You are predicting the league's reaction as much as the "
    "player. Ask what the other managers will want next week and whether you "
    "can hold it now, and prefer the move nobody has priced yet over the one "
    "everyone can see. "
    "THINK IN DISTRIBUTIONS, NOT VERDICTS. Say what is likely and how likely, "
    "not what will happen. Update on structural news fast — a starter lost for "
    "the season resolves in one snap — and on statistical noise slowly. "
    "DOING NOTHING IS A MOVE. Activity is not skill. If nothing improves "
    "expected points, upside, risk or flexibility, say so and stand pat. "
    "PLAY THE SEASON YOU ARE IN. The context states the current objective from "
    "the standings — grow value early, protect a bubble spot, take variance "
    "when eliminated is the likelier outcome, weight the playoff weeks when "
    "qualifying is safe. Answer to THAT objective, not to a generic 'most "
    "points this week'.]"
    "\n\n[EVIDENCE — weigh sources by what they are worth, and do not average "
    "conflicting ones into a false middle. In descending order: actual game "
    "participation (snaps, routes, targets, carries, red-zone work); official "
    "injury and practice status; team transactions and depth-chart changes; "
    "credible beat-reporter observation; coach comments; projection systems; "
    "expert rankings; social sentiment. A coach saying he wants someone more "
    "involved does not outweigh three weeks of 21% route share. "
    "Five accounts repeating one injury report is ONE piece of evidence, not "
    "five — do not count correlated sources twice. Structural news updates "
    "belief immediately (a starter lost for the season resolves in one snap); "
    "rate stats like yards per carry and touchdown rate move belief slowly. "
    "SEPARATE WHAT YOU KNOW FROM WHAT YOU INFER. State the fact, then the "
    "inference drawn from it, then the recommendation — 'he ran 74% of routes' "
    "is a fact, 'his passing-down role expanded' is an inference, and they "
    "should not be presented as the same thing. Where the context flags a "
    "SIGNAL CONFLICT, name which side you believe and why, or say role "
    "certainty is low. Where DATA CONFIDENCE reports a missing input, treat it "
    "as unknown rather than zero and say what you could not see.]"
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
    "is worth to MY lineup, and check the rival demand given: if no other team "
    "would start him, the minimum bid wins and saying otherwise wastes my "
    "budget. Many unrostered players are free adds costing nothing — say so "
    "rather than inventing a price. EVERY add must name the specific player "
    "it displaces and why that trade-up is worth it; if the best available "
    "free agent is worse than my own worst bench player, the correct answer "
    "is to stand pat, and say that plainly instead of manufacturing a target. "
    "Never suggest dropping a CORE player, a starter in this week's lineup, "
    "or an injured starter worth stashing on IR. "
    "When every option is replacement-level — which is normal — do NOT argue "
    "from the value scores, because below the waiver line they are all zero "
    "and comparing them says nothing. Argue from fit instead: is he the "
    "handcuff to a back I depend on, is he young with a path to work, am I "
    "thin at that position, is my own bench player doing anything at all. "
    "A flier with a real role case beats a hot name with none, and if neither "
    "has one, say so and tell me to stand pat.]"
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


# --- The weekly operating rhythm -------------------------------------------
# Each day of the week has a different job. A Monday answer that reads like a
# Sunday answer is doing the wrong work: Monday is for reading what changed in
# usage, Tuesday for pricing waivers, Saturday for optionality. Stating the
# day's job keeps the process disciplined instead of re-deriving it each time.
DAY_JOBS = {
    0: (  # Monday
        "MONDAY — postmortem, and do not chase points. The job is to find what "
        "CHANGED in usage, not who scored. For every relevant offense ask whose "
        "role became more valuable: snap share, route participation, target "
        "share, carries, third-down and two-minute work, goal-line and inside-5 "
        "carries, red-zone and end-zone targets. Classify every notable "
        "performance as role-driven (a real structural change — buy "
        "aggressively), skill-driven, situation-driven (game script inflated "
        "the volume), efficiency-driven, or touchdown-driven (the most "
        "dangerous to chase). Then build the waiver board before the articles "
        "tell everyone what to do."
    ),
    1: (  # Tuesday
        "TUESDAY — injury forensics and waiver pricing. For each injury map the "
        "tree: who replaces him, what share do they inherit, whose role changes "
        "downstream, does the passing game or goal-line work move. Identify the "
        "players who are ONE INJURY AWAY from immediate starter value and are "
        "still available — those are options worth buying before the event. "
        "Price claims by tier: potential season-changer, probable starter, "
        "ascending role, contingent upside, one-week streamer, and empty "
        "calories that will never be worth more than they are today."
    ),
    2: (  # Wednesday
        "WEDNESDAY — attack what waivers left behind. The easy edge is here: "
        "everyone watches who WON a claim, almost nobody inspects who went "
        "unclaimed, and what other managers DROPPED to make room. Look for the "
        "targets ranked 4th through 8th, backup backs, ascending rookies, "
        "injured players nearing return, and future streamers."
    ),
    3: (  # Thursday
        "THURSDAY — start/sit work begins, and Thursday-night lineup mechanics. "
        "Anyone playing tonight locks first, so keep them out of FLEX where the "
        "roster allows: FLEX is the slot you want free to absorb a Sunday "
        "inactive. Flag anyone questionable for Sunday who needs an insurance "
        "plan now."
    ),
    4: (  # Friday
        "FRIDAY — practice reports and the multi-week plan. Trajectory is what "
        "matters: DNP to limited to full is a different player from full to "
        "limited to DNP. Do not stop at whether he plays — ask whether he plays "
        "in his NORMAL ROLE, because a decoy at 45% of snaps is worse than his "
        "healthy backup. Then look two to four weeks out: byes, streaming "
        "positions, and anything worth acquiring a week early while it is cheap."
    ),
    5: (  # Saturday
        "SATURDAY — bench audit and free optionality. For every bench player "
        "answer why exactly you own him; 'he's decent' means that spot is being "
        "wasted. Then take the free options: if a starter is questionable for "
        "Sunday and his backup is available, adding the backup costs a "
        "disposable roster spot and pays a starter if the inactive lands."
    ),
    6: (  # Sunday
        "SUNDAY — information war. Inactives, beat reporters, warmups, weather, "
        "and offensive-line absences, in that order of urgency. When something "
        "breaks, run it through: injury, replacement, workload, projection, "
        "lineup, waiver consequence. Early games lock first, so preserve "
        "flexibility in later slots. Anything you learn today is also the start "
        "of next week's waiver board."
    ),
}


def day_directive(weekday: int) -> str:
    """The operating job for a given weekday (Monday = 0)."""
    job = DAY_JOBS.get(weekday)
    return f"\n\n[TODAY'S JOB — {job}]" if job else ""
