"""Strategy layer: turn raw Sleeper data into decisions.

This is where roster construction, positional needs, FAAB bids and drop
candidates are computed. The heuristics are intentionally simple and
explainable — every recommendation carries a human-readable reason. Sleeper
serves projections only through an undocumented endpoint, so they are a
best-effort bonus signal layered on top of the reliable ones: market rank,
market velocity (trending adds/drops), draft capital, injury status, and
positional depth vs. required starters.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from . import config, valuation
from .sleeper import (
    FANTASY_POSITIONS,
    SleeperClient,
    is_out,
    player_name,
    projected_points,
)

# Roster slots that don't count as "starters needed" at a fixed position.
BENCH_SLOTS = {"BN", "IR", "TAXI"}
# Flex-type slots and which positions can fill them.
FLEX_ELIGIBILITY = {
    "FLEX": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
    "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "IDP_FLEX": set(),
}


@dataclass
class LeagueContext:
    """Everything the strategy functions need, resolved once and cached."""

    league: dict
    rosters: list[dict]
    users: list[dict]
    players: dict[str, Any]
    my_user_id: str
    my_roster: dict
    week: int
    season: str
    faab_total: int
    faab_used: int
    rostered_ids: set[str] = field(default_factory=set)
    season_type: str = ""  # "regular" | "post" | "off" | "pre" from NFL state
    # player_id -> {"round", "pick", "overall"} from this league's own draft.
    draft_picks: dict[str, dict] = field(default_factory=dict)
    # player_id -> projected points for the current week, in league scoring.
    week_projections: dict[str, float] = field(default_factory=dict)
    # player_id -> projected points for the full season, in league scoring.
    season_projections: dict[str, float] = field(default_factory=dict)
    # player_id -> (positional rank, overall rank) by Sleeper's market rank.
    market_ranks: dict[str, tuple[int, int]] = field(default_factory=dict)
    # player_id -> {"score", "vorp", "points", "replacement"} from valuation.
    player_values: dict[str, dict] = field(default_factory=dict)
    # player_id -> CORE / STARTER / DEPTH / EXPENDABLE, for your roster only.
    roster_tiers: dict[str, str] = field(default_factory=dict)

    @property
    def has_projections(self) -> bool:
        return bool(self.week_projections or self.season_projections)

    @property
    def faab_remaining(self) -> int:
        return max(0, self.faab_total - self.faab_used)

    def team_name(self, user_id: str) -> str:
        for u in self.users:
            if u.get("user_id") == user_id:
                meta = u.get("metadata") or {}
                return meta.get("team_name") or u.get("display_name") or "Team"
        return "Team"


# --- Context building -------------------------------------------------------
_cache: dict[str, Any] = {"ctx": None, "ts": 0.0}


async def resolve_user_id(client: SleeperClient) -> Optional[str]:
    if config.SLEEPER_USER_ID:
        return config.SLEEPER_USER_ID
    if config.SLEEPER_USERNAME:
        user = await client.get_user(config.SLEEPER_USERNAME)
        if user:
            return user.get("user_id")
    return None


async def resolve_league_id(client: SleeperClient, user_id: Optional[str]) -> Optional[str]:
    """Find the league to use. An explicit LEAGUE_ID always wins. Otherwise
    auto-detect from the user's leagues, matching LEAGUE_NAME, trying the
    current NFL season first then recent past seasons — so the bot follows the
    manager into a new league each year and still works in the offseason."""
    if config.LEAGUE_ID:
        return config.LEAGUE_ID
    if not user_id:
        return None

    try:
        current = int((await client.get_nfl_state()).get("season") or 0)
    except Exception:
        current = 0
    if not current:
        current = datetime.now().year
    seasons = [str(current), str(current - 1), str(current - 2)]

    target = (config.LEAGUE_NAME or "").strip().lower()
    fallback = None
    for season in seasons:
        try:
            leagues = await client.get_user_leagues(user_id, season)
        except Exception:
            continue
        if not leagues:
            continue
        if fallback is None:
            fallback = leagues[0].get("league_id")
        if target:
            for lg in leagues:
                if (lg.get("name") or "").strip().lower() == target:
                    return lg.get("league_id")
        else:
            return leagues[0].get("league_id")
    # Name didn't match anywhere, but the manager has leagues — use the newest.
    return fallback


async def build_context(client: SleeperClient, force: bool = False) -> LeagueContext:
    """Build (or return cached) LeagueContext. Cached for CONTEXT_TTL so a
    burst of commands doesn't re-hit Sleeper repeatedly."""
    now = time.time()
    if not force and _cache["ctx"] and now - _cache["ts"] < config.CONTEXT_TTL:
        return _cache["ctx"]

    valuation.clear_memo()
    user_id = await resolve_user_id(client)
    league_id = await resolve_league_id(client, user_id)
    if not league_id:
        raise RuntimeError(
            "Could not determine your league. Set LEAGUE_ID, or set "
            "SLEEPER_USERNAME/SLEEPER_USER_ID so the bot can look it up."
        )

    league = await client.get_league(league_id)
    rosters = await client.get_rosters(league_id)
    users = await client.get_league_users(league_id)
    players = await client.get_players()
    state = await client.get_nfl_state()
    week = int(state.get("week") or 1) or 1
    season_type = state.get("season_type") or ""

    # Locate my roster. If we only had a LEAGUE_ID, fall back to the first.
    my_roster = None
    if user_id:
        my_roster = next((r for r in rosters if r.get("owner_id") == user_id), None)
    if my_roster is None:
        my_roster = rosters[0] if rosters else {}
        user_id = my_roster.get("owner_id", user_id or "")

    faab_total = int((league.get("settings") or {}).get("waiver_budget") or 100)
    faab_used = int((my_roster.get("settings") or {}).get("waiver_budget_used") or 0)

    rostered_ids: set[str] = set()
    for r in rosters:
        rostered_ids.update(r.get("players") or [])

    ctx = LeagueContext(
        league=league,
        rosters=rosters,
        users=users,
        players=players,
        my_user_id=user_id or "",
        my_roster=my_roster,
        week=week,
        season=str(league.get("season") or config.SEASON),
        faab_total=faab_total,
        faab_used=faab_used,
        rostered_ids=rostered_ids,
        season_type=season_type,
    )

    # Value signals. Each is optional on its own — a missing one degrades the
    # answer, but a failure here must never cost us the league data above.
    ctx.market_ranks = compute_market_ranks(ctx)
    ctx.draft_picks = await load_draft_picks(ctx, client)
    try:
        ctx.week_projections, ctx.season_projections = await load_projections(
            ctx, client
        )
    except Exception:
        ctx.week_projections, ctx.season_projections = {}, {}

    # Derived pricing. Runs on whatever signals arrived: with projections it's
    # real points over replacement, without them it degrades to a curve fitted
    # to market rank, so trade logic keeps working either way.
    ctx.player_values = valuation.compute_values(ctx)
    ctx.roster_tiers = valuation.roster_tiers(ctx)

    _cache["ctx"] = ctx
    _cache["ts"] = now
    return ctx


def is_offseason(ctx: LeagueContext) -> bool:
    """True when there's no live fantasy action — the league's season is
    complete, or the NFL is between seasons (offseason/preseason)."""
    if (ctx.league.get("status") or "").lower() == "complete":
        return True
    return ctx.season_type in ("off", "pre")


# --- Roster / needs analysis ------------------------------------------------
def required_starters(ctx: LeagueContext) -> dict[str, int]:
    """Count dedicated starting slots per position, plus flex pools.

    Returns a dict of position -> count for fixed slots and a special
    'FLEX' -> count aggregating all flex-type slots (RB/WR/TE eligible).
    """
    counts: dict[str, int] = {}
    flex = 0
    for slot in ctx.league.get("roster_positions", []):
        if slot in BENCH_SLOTS:
            continue
        if slot in FLEX_ELIGIBILITY:
            flex += 1
            continue
        counts[slot] = counts.get(slot, 0) + 1
    counts["FLEX"] = flex
    return counts


def my_players_by_position(ctx: LeagueContext) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for pid in ctx.my_roster.get("players") or []:
        p = ctx.players.get(pid)
        if not p:
            continue
        pos = p.get("position")
        if pos:
            grouped.setdefault(pos, []).append(p)
    return grouped


def positional_needs(ctx: LeagueContext) -> list[dict]:
    """Identify thin/injured spots. Returns a list of need descriptors,
    most urgent first."""
    required = required_starters(ctx)
    grouped = my_players_by_position(ctx)
    needs: list[dict] = []

    for pos in ("QB", "RB", "WR", "TE"):
        players = grouped.get(pos, [])
        healthy = [p for p in players if not is_out(p)]
        # Flex demand adds pressure on RB/WR/TE depth.
        flex_demand = required.get("FLEX", 0) if pos in {"RB", "WR", "TE"} else 0
        need_count = required.get(pos, 0) + (1 if flex_demand else 0)
        depth = len(healthy)
        injured = [p for p in players if is_out(p)]

        severity = 0
        reasons = []
        if depth < need_count:
            severity += (need_count - depth) * 2
            reasons.append(f"only {depth} healthy for ~{need_count} slots")
        elif depth == need_count:
            severity += 1
            reasons.append("no healthy depth behind starters")
        if injured:
            severity += len(injured)
            reasons.append(
                f"{len(injured)} out/IR ({', '.join(player_name(p) for p in injured[:3])})"
            )
        if severity:
            needs.append(
                {
                    "position": pos,
                    "severity": severity,
                    "depth": depth,
                    "required": need_count,
                    "reason": "; ".join(reasons),
                }
            )

    needs.sort(key=lambda n: n["severity"], reverse=True)
    return needs


def need_positions(ctx: LeagueContext, threshold: int = 1) -> set[str]:
    return {n["position"] for n in positional_needs(ctx) if n["severity"] >= threshold}


# --- Valuation layer --------------------------------------------------------
# Rosters alone tell Grok who owns whom but nothing about what anyone is WORTH,
# which is how you end up recommending a league-winning back for a mid-round
# flier. These helpers attach a market rank, a projection and the league's own
# draft capital to every player so trade and start/sit calls are anchored to
# numbers instead of the model's memory of last season.

# Sleeper marks irrelevant players with a sentinel search_rank instead of null.
_RANK_SENTINEL = 9_999_990


def _scoring_key(ctx: "LeagueContext") -> str:
    """Which projected-points field matches this league's scoring."""
    ppr = (ctx.league.get("scoring_settings") or {}).get("rec")
    if ppr == 1:
        return "pts_ppr"
    if ppr == 0.5:
        return "pts_half_ppr"
    return "pts_std"


def _market_rank(player: Optional[dict]) -> Optional[int]:
    """Sleeper's own market rank for a player (lower = more valuable)."""
    if not player:
        return None
    rank = player.get("search_rank")
    if not isinstance(rank, (int, float)) or rank >= _RANK_SENTINEL:
        return None
    return int(rank)


def compute_market_ranks(ctx: "LeagueContext") -> dict[str, tuple[int, int]]:
    """Turn Sleeper's raw search_rank into readable RB8 / overall-24 ranks.

    Ranked over every fantasy-relevant player on an NFL roster, so the numbers
    mean the same thing for a rostered star and a free agent.
    """
    ranked = []
    for pid, p in ctx.players.items():
        if (p.get("position") or "") not in FANTASY_POSITIONS:
            continue
        rank = _market_rank(p)
        if rank is None or not p.get("team"):
            continue
        ranked.append((rank, pid, p.get("position")))
    ranked.sort()

    out: dict[str, tuple[int, int]] = {}
    per_pos: dict[str, int] = {}
    for overall, (_, pid, pos) in enumerate(ranked, start=1):
        per_pos[pos] = per_pos.get(pos, 0) + 1
        out[pid] = (per_pos[pos], overall)
    return out


async def load_draft_picks(ctx: "LeagueContext", client: SleeperClient) -> dict[str, dict]:
    """Where each rostered player went in THIS league's draft.

    Draft slot is the league's own consensus price for a player, which is the
    cleanest available read on who was expensive and who was a late flier.
    """
    try:
        drafts = await client.get_league_drafts(ctx.league.get("league_id", ""))
    except Exception:
        return {}
    if not drafts:
        return {}
    # Sleeper returns newest first; prefer a completed draft for this season.
    draft = next(
        (
            d
            for d in drafts
            if str(d.get("season")) == str(ctx.season)
            and (d.get("status") or "") == "complete"
        ),
        drafts[0],
    )
    try:
        picks = await client.get_draft_picks(draft.get("draft_id", ""))
    except Exception:
        return {}

    out: dict[str, dict] = {}
    for pick in picks or []:
        pid = pick.get("player_id")
        if not pid:
            continue
        out[str(pid)] = {
            "round": pick.get("round"),
            "pick": pick.get("draft_slot") or pick.get("pick_no"),
            "overall": pick.get("pick_no"),
        }
    return out


async def load_projections(
    ctx: "LeagueContext", client: SleeperClient
) -> tuple[dict[str, float], dict[str, float]]:
    """This week's and the full season's projected points, in league scoring.

    Best-effort: the endpoint is undocumented, so an empty result just means
    the other value signals carry the answer.
    """
    key = _scoring_key(ctx)

    async def fetch(week: Optional[int]) -> dict[str, float]:
        try:
            rows = await client.get_projections(ctx.season, week)
        except Exception:
            return {}
        out: dict[str, float] = {}
        for row in rows or []:
            pid = row.get("player_id")
            pts = projected_points(row, key)
            if pid and pts is not None:
                out[str(pid)] = round(pts, 1)
        return out

    week_proj = {} if is_offseason(ctx) else await fetch(ctx.week)
    season_proj = await fetch(None)
    return week_proj, season_proj


def value_tag(ctx: "LeagueContext", pid: str) -> str:
    """Compact value annotation for one player: rank, projections, draft cost.

    Rendered inline next to every name so the model can't discuss a trade
    without seeing both sides' prices.
    """
    bits = []
    value = ctx.player_values.get(pid)
    if value:
        bits.append(f"val {value['score']}/100")
    ranks = ctx.market_ranks.get(pid)
    if ranks:
        pos_rank, overall = ranks
        pos = (ctx.players.get(pid) or {}).get("position") or "?"
        bits.append(f"mkt {pos}{pos_rank}/ovr{overall}")
    season = ctx.season_projections.get(pid)
    if season is not None:
        bits.append(f"proj {season}pts/season")
    week = ctx.week_projections.get(pid)
    if week is not None:
        bits.append(f"{week} this wk")
    dc = valuation.depth_chart(ctx, pid)
    if dc:
        bits.append(f"depth chart #{dc[0]} at {dc[1]}")
    if valuation.is_unavailable_this_week(ctx, pid):
        bits.append("NO GAME THIS WEEK (bye or inactive)")
    drafted = ctx.draft_picks.get(pid)
    if drafted and drafted.get("round"):
        bits.append(f"drafted R{drafted['round']}")
    elif ctx.draft_picks:
        bits.append("undrafted")
    return ", ".join(bits)


def value_board_context(ctx: "LeagueContext", per_pos: int = 18) -> str:
    """A ranked, league-wide price sheet: the top players at each position with
    their value signals and current owner.

    This is the tiering that makes an unbalanced trade obvious — a top-5 back
    and a fringe starter sit visibly far apart on the same list.
    """
    if not ctx.market_ranks:
        return ""
    owner_by_pid: dict[str, str] = {}
    for r in ctx.rosters:
        owner = ctx.team_name(r.get("owner_id", ""))
        if r.get("owner_id") == ctx.my_user_id:
            owner += " (YOU)"
        for pid in r.get("players") or []:
            owner_by_pid[pid] = owner

    levels = valuation.replacement_levels(ctx)
    level_note = ", ".join(
        f"{pos} {pts:.0f}pts" for pos, pts in sorted(levels.items())
    )
    lines = [
        "VALUE BOARD — the price sheet for this league, sorted by computed "
        "value. 'val' is a 0-100 score built from projected points ABOVE the "
        "replacement-level player at that position, using this league's own "
        "starter requirements, then discounted for injury. It is directly "
        "comparable ACROSS positions — a QB and a RB with the same score are "
        "worth the same in a trade, even though the QB scores more raw points. "
        "A score of 0 means waiver-level: freely replaceable. Replacement "
        f"baselines this league: {level_note or 'unavailable'}. "
        "Market rank is Sleeper's consensus ranking (lower = better); draft "
        "round is what this league actually paid. Treat a large gap in value "
        "as a real gap: never propose sending a clearly higher-valued player "
        "for a lower one unless you explicitly justify why the market is wrong."
    ]
    for pos in ("QB", "RB", "WR", "TE"):
        entries = [
            (-(ctx.player_values.get(pid) or {}).get("score", 0.0), ranks[0], pid)
            for pid, ranks in ctx.market_ranks.items()
            if (ctx.players.get(pid) or {}).get("position") == pos
        ]
        entries.sort()
        rows = []
        for _, _, pid in entries[:per_pos]:
            p = ctx.players.get(pid) or {}
            owner = owner_by_pid.get(pid, "FREE AGENT")
            tag = value_tag(ctx, pid)
            name = player_name(p)
            if p.get("injury_status"):
                name += f" ({p['injury_status']})"
            rows.append(f"    {name} [{tag}] — {owner}")
        if rows:
            lines.append(f"  {pos}:")
            lines.extend(rows)
    return "\n".join(lines)


def team_context_summary(ctx: LeagueContext) -> str:
    """Compact, model-friendly summary of the user's team for personalized Q&A."""
    scoring = ctx.league.get("scoring_settings") or {}
    ppr = scoring.get("rec")
    fmt = "PPR" if ppr == 1 else ("Half-PPR" if ppr == 0.5 else "Standard")
    starters = [
        s for s in ctx.league.get("roster_positions", []) if s not in BENCH_SLOTS
    ]

    off = (
        " | OFFSEASON: this season is complete, so there are no live waivers "
        "yet — frame advice as outlook/planning, not this-week moves"
        if is_offseason(ctx)
        else ""
    )
    flex_ct = sum(1 for s in starters if s in FLEX_ELIGIBILITY)
    flex_elig: set[str] = set()
    for s in starters:
        flex_elig |= FLEX_ELIGIBILITY.get(s, set())
    superflex = "QB" in flex_elig
    slot_extras = []
    if flex_ct:
        elig = "/".join(p for p in ("QB", "RB", "WR", "TE") if p in flex_elig)
        slot_extras.append(f"{flex_ct} FLEX = {elig} only")
    slot_extras.append("no kicker" if "K" not in starters else "starts a kicker")
    slot_extras.append(
        f"start exactly {starters.count('QB')} QB and {starters.count('DEF')} DEF"
        + ("" if superflex else " (single-QB league, NOT superflex)")
    )

    settings = ctx.league.get("settings") or {}
    p_start = settings.get("playoff_week_start")
    p_teams = settings.get("playoff_teams")
    playoff_line = None
    if p_start:
        rounds = max(1, (int(p_teams) - 1).bit_length()) if p_teams else 3
        champ = int(p_start) + rounds - 1
        playoff_line = (
            f"Playoffs: {p_teams} of {len(ctx.rosters)} teams make it, Weeks "
            f"{p_start}-{champ} (championship Week {champ}) — weigh the Weeks "
            f"{p_start}-{champ} schedule for rest-of-season and keeper value"
        )

    lines = [
        f"League: {ctx.league.get('name', 'League')} | {len(ctx.rosters)}-team | "
        f"{fmt} | Week {ctx.week} {ctx.season}{off}",
        f"Starting slots: {', '.join(starters)} — {'; '.join(slot_extras)}",
    ]
    if playoff_line:
        lines.append(playoff_line)
    lines += [
        f"FAAB remaining: ${ctx.faab_remaining} of ${ctx.faab_total}",
        "Your roster:",
    ]
    grouped = my_players_by_position(ctx)
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        players = grouped.get(pos, [])
        if not players:
            continue
        names = []
        for p in players:
            nm = player_name(p)
            if p.get("injury_status"):
                nm += f" ({p.get('injury_status')})"
            tag = value_tag(ctx, p.get("player_id", ""))
            if tag:
                nm += f" [{tag}]"
            names.append(nm)
        lines.append(f"  {pos}: {', '.join(names)}")

    needs = positional_needs(ctx)
    if needs:
        lines.append(
            "Needs: " + "; ".join(f"{n['position']} ({n['reason']})" for n in needs)
        )
    return "\n".join(lines)


def league_rosters_context(ctx: LeagueContext) -> str:
    """Every team's full roster (who owns whom). Anyone not listed here is a
    free agent — this gives Grok the authoritative live league picture for
    availability and trade questions."""
    lines = [
        "Full league rosters — who owns whom. Any fantasy-relevant player NOT "
        "listed below is a FREE AGENT available to add. Each player carries "
        "their value signals in brackets: mkt = consensus market rank (lower "
        "is better), proj = projected points in this league's scoring, and the "
        "round this league drafted them in:"
    ]
    for r in sorted(ctx.rosters, key=lambda x: x.get("roster_id", 0)):
        owner = ctx.team_name(r.get("owner_id", ""))
        mine = " (YOUR TEAM)" if r.get("owner_id") == ctx.my_user_id else ""
        names = []
        for pid in r.get("players") or []:
            p = ctx.players.get(pid)
            if not p:
                continue
            pos = p.get("position") or "?"
            team = p.get("team") or "FA"
            tag = f"{player_name(p)} ({pos}-{team})"
            if is_out(p):
                tag += f"[{p.get('injury_status')}]"
            value = value_tag(ctx, pid)
            if value:
                tag += f" [{value}]"
            names.append(tag)
        lines.append(f"{owner}{mine}: {', '.join(names) if names else '(empty)'}")
    return "\n".join(lines)


async def opponent_context(
    ctx: LeagueContext, client: SleeperClient, final: bool = False
) -> str:
    """This week's head-to-head opponent and their starting lineup, so start/sit
    can weigh floor vs. ceiling: protect a lead with safe plays when favored,
    chase upside with boom/bust when you're the underdog.

    Robust to an opponent who hasn't set their lineup yet: their full roster is
    already in the league context, so Grok is told to judge their strength by
    their BEST likely lineup rather than whatever placeholder starters show."""
    my_rid = ctx.my_roster.get("roster_id")
    if my_rid is None:
        return ""
    try:
        matchups = await client.get_matchups(ctx.league.get("league_id"), ctx.week)
    except Exception:
        return ""
    if not matchups:
        return ""
    mine = next((m for m in matchups if m.get("roster_id") == my_rid), None)
    if not mine or mine.get("matchup_id") is None:
        return ""
    mid = mine.get("matchup_id")
    opp = next(
        (m for m in matchups
         if m.get("matchup_id") == mid and m.get("roster_id") != my_rid),
        None,
    )
    if not opp:
        return ""
    opp_roster = next(
        (r for r in ctx.rosters if r.get("roster_id") == opp.get("roster_id")), {}
    )
    opp_name = ctx.team_name(opp_roster.get("owner_id", ""))
    starter_ids = [pid for pid in (opp.get("starters") or []) if pid and pid != "0"]
    names = []
    for pid in starter_ids or (opp.get("players") or []):
        p = ctx.players.get(pid)
        if p:
            names.append(f"{player_name(p)} ({p.get('position', '?')}-{p.get('team', 'FA')})")
    if not names:
        return ""

    # A set lineup that still contains an out/injured player is a tell that the
    # opponent hasn't actually set it this week.
    looks_unset = any(is_out(ctx.players.get(pid, {})) for pid in starter_ids)
    if final:
        caveat = (
            " These starters are near lineup-lock; if any get ruled out, assume "
            "they swap to their next-best bench option."
        )
    else:
        caveat = (
            " Their lineup likely isn't finalized this early in the week — judge "
            "their strength by their BEST likely lineup from their full roster "
            "(in the rosters above), not just these currently-set starters."
        )
    if looks_unset:
        caveat += " (Their current lineup looks unset — it still has an out/injured player in it.)"

    return (
        f"This week's H2H opponent — {opp_name}. Their current starters: "
        + ", ".join(names)
        + "."
        + caveat
        + " MATCHUP STRATEGY: compare our best lineups; if I project clearly "
        "ahead, lean to safe FLOOR plays to lock the win; if I'm the underdog, "
        "favor high-CEILING boom/bust options to raise my win probability. Flag "
        "any floor-vs-ceiling swaps this matchup calls for."
    )


def scoring_context(ctx: LeagueContext) -> str:
    """The league's exact scoring rules so Grok values players for THIS league
    (e.g. 4-pt vs 6-pt pass TDs, PPR, TE premium, kicker/DEF quirks)."""
    s = ctx.league.get("scoring_settings") or {}
    if not s:
        return ""
    ppr = s.get("rec", 0)
    fmt = (
        "Full PPR" if ppr == 1 else "Half-PPR" if ppr == 0.5
        else "Standard (no PPR)" if not ppr else f"{ppr}/reception"
    )
    notes = [fmt, f"pass TD {s.get('pass_td', 4)}pt"]
    if s.get("bonus_rec_te"):
        notes.append(f"TE premium +{s['bonus_rec_te']}/rec")
    if any(s.get(k) for k in ("bonus_rec_fd", "bonus_rush_fd", "fd", "bonus_fd")):
        notes.append("first-down bonus")
    raw = ", ".join(f"{k}={v}" for k, v in sorted(s.items()) if v)
    return (
        "League scoring — weigh these in every valuation (PPR lifts pass-"
        "catchers; low pass-TD points cool QBs; kicker/DEF rules affect "
        "streaming): "
        + ", ".join(notes)
        + ".\n  Exact per-action points: "
        + raw
    )


def league_rules_context(ctx: LeagueContext) -> str:
    """The league's operating rules: deadlines, waiver timing, IR, vetoes.

    Advice that ignores these is wrong however good the player analysis is —
    proposing a trade after the deadline, or suggesting a drop when the player
    could be stashed on IR for free. Every field is read defensively: Sleeper
    omits settings that don't apply, and a missing one just drops its line.
    """
    st = ctx.league.get("settings") or {}
    lines: list[str] = ["LEAGUE RULES — these constrain what you can advise:"]

    deadline = st.get("trade_deadline")
    if deadline:
        deadline = int(deadline)
        if ctx.week > deadline:
            lines.append(
                f"  TRADE DEADLINE WAS WEEK {deadline} AND IT HAS PASSED "
                f"(now Week {ctx.week}) — trades are CLOSED. Do not propose "
                "any trade; the only roster moves left are waivers, free "
                "agents and lineup changes."
            )
        else:
            left = deadline - ctx.week
            lines.append(
                f"  Trade deadline: end of Week {deadline} — {left} week(s) "
                "left to trade" + (", act now" if left <= 2 else "")
            )

    ir_slots = st.get("reserve_slots")
    if ir_slots:
        on_ir = len(ctx.my_roster.get("reserve") or [])
        allowed = [
            label
            for key, label in (
                ("reserve_allow_out", "Out"),
                ("reserve_allow_doubtful", "Doubtful"),
                ("reserve_allow_sus", "Suspended"),
                ("reserve_allow_na", "NA"),
                ("reserve_allow_dnr", "DNR/holdout"),
            )
            if st.get(key)
        ]
        lines.append(
            f"  IR slots: {on_ir} of {int(ir_slots)} used"
            + (f" — only {', '.join(allowed)} players are IR-eligible" if allowed else "")
            + ". An IR-eligible player should be stashed on IR, NOT dropped: it "
            "frees the bench spot at no cost. Only recommend dropping him if "
            "every IR slot is already full."
        )

    clear_days = st.get("waiver_clear_days")
    if clear_days:
        lines.append(
            f"  Dropped players sit on waivers {int(clear_days)} day(s) before "
            "becoming free agents — a player dropped today needs a FAAB claim, "
            "not a free add."
        )

    veto = st.get("veto_votes_needed")
    if veto:
        lines.append(
            f"  Trade vetoes: {int(veto)} of {len(ctx.rosters)} managers can "
            "void a trade — a deal that looks lopsided to the league may not "
            "survive review."
        )

    return "\n".join(lines) if len(lines) > 1 else ""


async def full_league_context(ctx: LeagueContext, client: SleeperClient) -> str:
    """The whole live picture for Grok: your team, all rosters, everyone's FAAB,
    the league's exact scoring, and notable available free agents. Shared by
    Q&A and the digests."""
    parts = [
        team_context_summary(ctx),
        scoring_context(ctx),
        league_rules_context(ctx),
        valuation.lineup_context(ctx),
        league_rosters_context(ctx),
        value_board_context(ctx),
        valuation.trade_posture_context(ctx),
        waiver_board_context(ctx),
        league_faab_context(ctx),
    ]
    fa = await available_fa_context(ctx, client)
    if fa:
        parts.append(fa)
    return "\n\n".join(p for p in parts if p)


def league_faab_context(ctx: LeagueContext) -> str:
    """Each team's remaining FAAB, so bid advice accounts for who can outbid you
    (waivers go to the highest bidder)."""
    rows = []
    for r in ctx.rosters:
        used = int((r.get("settings") or {}).get("waiver_budget_used") or 0)
        rem = max(0, ctx.faab_total - used)
        mine = r.get("owner_id") == ctx.my_user_id
        rows.append((ctx.team_name(r.get("owner_id", "")), rem, mine))
    rows.sort(key=lambda x: x[1], reverse=True)
    lines = [
        f"FAAB remaining by team (of ${ctx.faab_total}; highest bid wins a "
        "waiver — use this to size a winning bid and spot who can outbid you):"
    ]
    for name, rem, mine in rows:
        lines.append(f"  {name}{' (YOU)' if mine else ''}: ${rem}")
    return "\n".join(lines)


def waiver_board_context(ctx: LeagueContext, per_pos: int = 8) -> str:
    """The best players available in this league, ranked by value.

    The trending list alone can't answer a waiver question: it surfaces who the
    market is chasing, which misses a genuinely valuable player nobody has
    gotten to yet and over-rates a hot name who would not crack the lineup.
    This ranks every unowned player by the same value score used everywhere
    else, and states what each would actually upgrade.
    """
    if not ctx.player_values:
        return ""
    lines = [
        "WAIVER WIRE — every unowned player in this league worth a claim, "
        "ranked by value, NOT by how many people are adding them. 'upgrade' is "
        "how much they would improve YOUR starting lineup: the difference "
        "between their value and the player they would displace. A negative or "
        "zero upgrade means they would sit on your bench — only worth a claim "
        "as insurance or a stash, and say so if you recommend one:"
    ]
    any_rows = False
    for pos in ("QB", "RB", "WR", "TE"):
        rows = []
        candidates = [
            pid
            for pid in ctx.player_values
            if pid not in ctx.rostered_ids
            and (ctx.players.get(pid) or {}).get("position") == pos
        ]
        candidates.sort(
            key=lambda pid: ctx.player_values[pid]["score"], reverse=True
        )
        for pid in candidates[:per_pos]:
            p = ctx.players.get(pid) or {}
            score = ctx.player_values[pid]["score"]
            if score <= 0 and rows:
                break  # Below replacement and we already have real options.
            upgrade = valuation.upgrade_over_roster(ctx, pid)
            week = ctx.week_projections.get(pid)
            bits = [f"val {score}"]
            if week is not None:
                bits.append(f"{week}pts this wk")
            bits.append(f"upgrade {upgrade:+}")
            flag = f" ⚠{p['injury_status']}" if p.get("injury_status") else ""
            rows.append(
                f"    {player_name(p)} ({p.get('team', 'FA')}) "
                f"[{', '.join(bits)}]{flag}"
            )
        if rows:
            any_rows = True
            lines.append(f"  {pos}:")
            lines.extend(rows)
    return "\n".join(lines) if any_rows else ""


async def available_fa_context(
    ctx: LeagueContext, client: SleeperClient, per_pos: int = 8
) -> str:
    """Players the market is actively chasing who are available here.

    Complements the waiver board: velocity is a leading indicator of news the
    value scores haven't caught up to yet, so both signals go to the model.
    """
    try:
        trending = await client.get_trending("add", lookback_hours=72, limit=100)
    except Exception:
        # Velocity is a supporting signal; the waiver board above already
        # carries the ranked list. Degrade rather than lose the whole answer.
        return ""
    by_pos: dict[str, list[str]] = {}
    for entry in trending:
        pid = entry.get("player_id")
        if not pid or pid in ctx.rostered_ids:
            continue
        p = ctx.players.get(pid)
        if not p:
            continue
        pos = p.get("position")
        if pos not in FANTASY_POSITIONS:
            continue
        bucket = by_pos.setdefault(pos, [])
        if len(bucket) < per_pos:
            team = p.get("team")
            entry_name = player_name(p) + (f" ({team})" if team else "")
            tag = value_tag(ctx, pid)
            bucket.append(entry_name + (f" [{tag}]" if tag else ""))

    if not by_pos:
        return ""
    lines = [
        "TRENDING ADDS available in your league (not on any roster, ranked by "
        "recent add volume). Velocity often front-runs the value scores — a "
        "surge usually means news broke. Cross-check anyone hot here against "
        "the waiver board above, and if they're being added everywhere but "
        "score low, say which one you believe and why:"
    ]
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        if by_pos.get(pos):
            lines.append(f"  {pos}: {', '.join(by_pos[pos])}")
    lines.append(
        "When asked about free agents/waivers, recommend from THIS list. If you "
        "mention someone not on it, flag that they may already be rostered."
    )
    return "\n".join(lines)


# --- Free agents & FAAB -----------------------------------------------------
async def hot_free_agents(
    ctx: LeagueContext,
    client: SleeperClient,
    limit: int = 10,
    lookback_hours: int = 48,
    positions: Optional[set[str]] = None,
) -> list[dict]:
    """Trending-add players that are actually available in your league.

    Each entry: {player, player_id, adds, position, fills_need}.
    """
    try:
        trending = await client.get_trending(
            "add", lookback_hours=lookback_hours, limit=100
        )
    except Exception:
        return []
    needs = need_positions(ctx)
    results: list[dict] = []
    for entry in trending:
        pid = entry.get("player_id")
        if not pid or pid in ctx.rostered_ids:
            continue
        p = ctx.players.get(pid)
        if not p:
            continue
        pos = p.get("position")
        if pos not in FANTASY_POSITIONS:
            continue
        if positions and pos not in positions:
            continue
        results.append(
            {
                "player": p,
                "player_id": pid,
                "adds": int(entry.get("count") or 0),
                "position": pos,
                "fills_need": pos in needs,
            }
        )
        if len(results) >= limit * 3:  # gather extra before need-sorting
            break

    # Prioritize players that fill a roster need, then raw add velocity.
    results.sort(key=lambda r: (r["fills_need"], r["adds"]), reverse=True)
    return results[:limit]


def suggest_faab_bid(ctx: LeagueContext, candidate: dict, max_adds: int) -> dict:
    """Turn a free-agent candidate into a concrete bid.

    Three inputs, each answering a different question:

      upgrade    = how much he improves your STARTING lineup over the player
                   he'd displace. Sets what he is worth, and so the ceiling.
      rivals     = how many other teams have a hole at his position AND budget
                   left. Sets how much of that ceiling you actually have to
                   spend; with nobody bidding, the right price is the minimum.
      add volume = league-wide market interest, used only as a tiebreak.

    Bids are computed in dollars against the real budget. An earlier version
    worked in percentages with a flat additive spread, which produced a $19-79
    range on a $1000 budget while claiming "2%".
    """
    remaining = ctx.faab_remaining
    pid = candidate["player_id"]
    upgrade = valuation.upgrade_over_roster(ctx, pid)
    priced = bool(ctx.player_values.get(pid))
    add_value_for_rivals = (ctx.player_values.get(pid) or {}).get("score", 0.0)
    rivals = valuation.competing_teams(
        ctx, candidate["position"], add_value_for_rivals
    )
    comp = (candidate["adds"] / max_adds) if max_adds else 0.0

    # What he is worth to this roster, as a share of the budget.
    worth = min(1.0, max(0.0, upgrade / 40.0))
    ceiling_pct = 0.02 + 0.45 * worth

    # How much of that you must actually pay. Nobody competing means minimum.
    pressure = min(1.0, len(rivals) / 4.0) * 0.75 + 0.25 * comp
    bid_pct = ceiling_pct * (0.25 + 0.75 * pressure)

    if not priced:
        # K/DEF and anyone unpriced: streaming fodder, never a budget item.
        bid_pct = min(bid_pct, 0.01)

    bid = int(round(remaining * bid_pct))
    if upgrade <= 0 or not priced:
        # He would not crack your lineup. Undrafted players are usually free
        # adds; if he is on waivers a token claim wins him. Either way this is
        # not a budget decision.
        bid, bid_high = 0, 1
    else:
        bid = max(1, bid)
        # Range scales with the bid itself, so it stays sane on any budget.
        bid_high = max(bid + 1, int(round(bid * 1.6)))
    bid = min(bid, remaining)
    bid_high = min(bid_high, remaining)

    drop = valuation.worst_rosterable(ctx)
    drop_pid, drop_value = drop if drop else (None, 0.0)
    add_value = (ctx.player_values.get(pid) or {}).get("base_score", 0.0)

    # Below the waiver line every player scores 0, so comparing those scores
    # is a non-comparison — "worse than X (0.0 vs 0.0)" says nothing. When both
    # sides are replacement-level the decision is about role and upside instead:
    # is he a handcuff, is he young with a path, am I thin at the position, and
    # is the man he'd replace doing anything at all?
    add_flags = valuation.upside_flags(ctx, pid)
    drop_flags = valuation.dead_weight_flags(ctx, drop_pid) if drop_pid else []
    both_replacement = add_value <= 0 and drop_value <= 0

    if drop_pid is None:
        beats_drop, verdict = False, "no droppable player — an add costs you a starter"
    elif add_value > drop_value + 3:
        beats_drop = True
        verdict = f"clear upgrade on {{drop}} ({add_value} vs {drop_value})"
    elif both_replacement and add_flags:
        # Neither is worth points today, but one of them has a reason to exist.
        beats_drop = True
        verdict = "lottery ticket worth the bench spot — " + "; ".join(add_flags[:2])
        if drop_flags:
            verdict += f" · {{drop}} is dead weight: {drop_flags[0]}"
    elif both_replacement:
        beats_drop = False
        verdict = (
            "no edge either way — both are replacement-level with no role or "
            "upside case; stand pat"
        )
    else:
        beats_drop = False
        verdict = f"worse than {{drop}} ({add_value} vs {drop_value}) — skip"

    reason_bits: list[str] = []
    if upgrade > 0:
        reason_bits.append(f"upgrades your lineup by {upgrade:+}")
    elif priced:
        reason_bits.append("wouldn't crack your lineup — stash only")
    else:
        reason_bits.append("streaming option, not a budget item")
    if rivals:
        reason_bits.append(
            f"{len(rivals)} rival(s) need {candidate['position']} and can bid"
            + (f" ({', '.join(rivals[:3])})" if len(rivals) <= 3 else "")
        )
    else:
        reason_bits.append("no rival needs this position — minimum bid wins")
    reason_bits.append(f"{candidate['adds']:,} adds/48h")

    return {
        "bid": bid,
        "bid_high": bid_high,
        "pct": round(100.0 * bid / remaining) if remaining else 0,
        "upgrade": upgrade,
        "add_value": add_value,
        "drop_player_id": drop_pid,
        "drop_value": drop_value,
        "beats_drop": beats_drop,
        "verdict": verdict,
        "add_flags": add_flags,
        "drop_flags": drop_flags,
        "rivals": rivals,
        "reason": "; ".join(reason_bits),
    }


async def faab_recommendations(
    ctx: LeagueContext, client: SleeperClient, limit: int = 6
) -> list[dict]:
    """Ranked pickup list with concrete bids. Each entry merges the hot-FA
    candidate with its suggested bid."""
    candidates = await hot_free_agents(ctx, client, limit=limit * 3)
    if not candidates:
        return []
    max_adds = max(c["adds"] for c in candidates)
    recs = []
    for c in candidates:
        bid = suggest_faab_bid(ctx, c, max_adds)
        recs.append({**c, **bid})
    # Lead with what actually improves the lineup. A hot name who is worse
    # than the player you'd cut for him is not a waiver target, so he sorts
    # last and carries the comparison that says why.
    # Clamp upgrade at zero before sorting: below the waiver line the number is
    # a meaningless negative that differs by position, which would otherwise
    # rank a no-hope receiver above a genuine handcuff. Once tied at zero, the
    # verdict decides.
    recs.sort(
        key=lambda r: (max(r["upgrade"], 0.0), r["beats_drop"], r["adds"]),
        reverse=True,
    )
    return recs[:limit]


# --- Drop candidates --------------------------------------------------------
async def drop_candidates(
    ctx: LeagueContext, client: SleeperClient, limit: int = 4
) -> list[dict]:
    """Rank your own players by how droppable they are.

    Signals (higher = more droppable): out/IR status, appearing in the
    league-wide trending-drop list, and being buried depth at a position.
    """
    try:
        trending_drop = await client.get_trending("drop", lookback_hours=48, limit=100)
    except Exception:
        trending_drop = []
    drop_velocity = {
        e["player_id"]: int(e.get("count") or 0)
        for e in trending_drop
        if e.get("player_id")
    }

    grouped = my_players_by_position(ctx)
    # Rank within each position so we know who's buried.
    depth_rank: dict[str, int] = {}
    for pos, players in grouped.items():
        for i, p in enumerate(players):
            key = p.get("player_id")
            if key:
                depth_rank[key] = i

    required = required_starters(ctx)
    starters, _ = valuation.optimal_lineup(ctx)
    starting_ids = {e["player_id"] for e in starters if e.get("player_id")}
    st = ctx.league.get("settings") or {}
    ir_total = int(st.get("reserve_slots") or 0)
    on_ir = set(ctx.my_roster.get("reserve") or [])
    ir_free = max(0, ir_total - len(on_ir))
    ir_eligible = {
        label
        for key, label in (
            ("reserve_allow_out", "Out"),
            ("reserve_allow_doubtful", "Doubtful"),
            ("reserve_allow_sus", "Sus"),
            ("reserve_allow_na", "NA"),
            ("reserve_allow_dnr", "DNR"),
        )
        if st.get(key)
    } | {"IR"}

    scored: list[dict] = []
    for pid in ctx.my_roster.get("players") or []:
        p = ctx.players.get(pid)
        if not p:
            continue
        if pid in on_ir:
            continue  # Already stashed — not occupying a bench spot.
        if valuation.is_unavailable_this_week(ctx, pid):
            # On bye or inactive this week. That is temporary, and cutting a
            # useful player because he happens to be off this week is the
            # inverse of the mistake of starting him.
            continue
        if pid in starting_ids:
            # Season-long value can rate a weekly starter as replaceable.
            # Telling you to start a man and cut him in the same breath is
            # incoherent, so this week's lineup wins.
            continue
        pos = p.get("position") or "?"
        score = 0.0
        reasons = []

        if is_out(p):
            status = p.get("injury_status") or ""
            # Dropping a player you could stash for free is a pure loss, so an
            # IR-eligible injury is a reason to IR him, not to cut him.
            if ir_free and status in ir_eligible:
                continue
            score += 5
            reasons.append(status)
        if pid in drop_velocity:
            score += 2
            reasons.append("being dropped league-wide")

        rank = depth_rank.get(pid, 0)
        starters = required.get(pos, 0) + (required.get("FLEX", 0) if pos in {"RB", "WR", "TE"} else 0)
        if rank >= max(starters, 1) + 1:
            score += 1.5
            reasons.append(f"buried #{rank + 1} at {pos}")

        # Value is the strongest drop signal available: a player scoring 0 is
        # replaceable by the best free agent on the wire, whatever his name.
        # A CORE player is never a drop candidate — an injured star is someone
        # you stash, and suggesting otherwise is how you lose a season.
        tier = ctx.roster_tiers.get(pid)
        if tier == "CORE":
            continue
        if tier == "EXPENDABLE":
            score += 2.5
            reasons.append("below replacement level — waiver-grade")
        elif tier == "STARTER":
            score -= 4
        # Kickers/defenses are the classic streaming drops.
        if pos in {"K", "DEF"} and rank >= 1:
            score += 1
            reasons.append(f"backup {pos}")

        if score > 0:
            scored.append(
                {
                    "player": p,
                    "player_id": pid,
                    "position": pos,
                    "score": score,
                    "reason": ", ".join(reasons) or "low priority",
                }
            )

    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored[:limit]
