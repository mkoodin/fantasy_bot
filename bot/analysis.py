"""Strategy layer: turn raw Sleeper data into decisions.

This is where roster construction, positional needs, FAAB bids and drop
candidates are computed. The heuristics are intentionally simple and
explainable — every recommendation carries a human-readable reason. Sleeper
doesn't expose projections, so signals used are: market velocity (trending
adds/drops), injury status, and positional depth vs. required starters.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from . import config
from .sleeper import (
    FANTASY_POSITIONS,
    SleeperClient,
    is_out,
    player_name,
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
        "listed below is a FREE AGENT available to add:"
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


async def full_league_context(ctx: LeagueContext, client: SleeperClient) -> str:
    """The whole live picture for Grok: your team, all rosters, everyone's FAAB,
    the league's exact scoring, and notable available free agents. Shared by
    Q&A and the digests."""
    parts = [
        team_context_summary(ctx),
        scoring_context(ctx),
        league_rosters_context(ctx),
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


async def available_fa_context(
    ctx: LeagueContext, client: SleeperClient, per_pos: int = 8
) -> str:
    """List notable players actually AVAILABLE in this league (trending adds not
    on any roster), grouped by position — so Grok recommends real waiver options
    instead of generic names that may already be rostered."""
    trending = await client.get_trending("add", lookback_hours=72, limit=100)
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
            bucket.append(player_name(p) + (f" ({team})" if team else ""))

    if not by_pos:
        return ""
    lines = [
        "Notable AVAILABLE free agents in your league right now "
        "(not on any roster, by recent add volume):"
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
    trending = await client.get_trending(
        "add", lookback_hours=lookback_hours, limit=100
    )
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
    """Turn a hot-FA candidate into a concrete bid, as % of remaining budget.

    Explainable heuristic:
      base       = market interest, scaled by add velocity vs. the hottest FA
      need_mult  = 1.5x if the player fills a positional need, else 1.0x
      bid_pct    = clamp(base * need_mult, 1%, 55% of remaining FAAB)
    """
    remaining = ctx.faab_remaining
    norm = (candidate["adds"] / max_adds) if max_adds else 0.0
    base_pct = 0.03 + 0.42 * norm
    need_mult = 1.5 if candidate["fills_need"] else 1.0
    bid_pct = min(0.55, max(0.01, base_pct * need_mult))

    bid = max(1, round(remaining * bid_pct))
    bid_high = max(bid, round(remaining * min(0.6, bid_pct + 0.06)))

    reason_bits = [f"{candidate['adds']:,} adds/48h"]
    if candidate["fills_need"]:
        reason_bits.append(f"fills {candidate['position']} need")
    else:
        reason_bits.append("depth/upside")

    return {
        "bid": bid,
        "bid_high": bid_high,
        "pct": round(bid_pct * 100),
        "reason": ", ".join(reason_bits),
    }


async def faab_recommendations(
    ctx: LeagueContext, client: SleeperClient, limit: int = 6
) -> list[dict]:
    """Ranked pickup list with concrete bids. Each entry merges the hot-FA
    candidate with its suggested bid."""
    candidates = await hot_free_agents(ctx, client, limit=limit)
    if not candidates:
        return []
    max_adds = max(c["adds"] for c in candidates)
    recs = []
    for c in candidates:
        bid = suggest_faab_bid(ctx, c, max_adds)
        recs.append({**c, **bid})
    return recs


# --- Drop candidates --------------------------------------------------------
async def drop_candidates(
    ctx: LeagueContext, client: SleeperClient, limit: int = 4
) -> list[dict]:
    """Rank your own players by how droppable they are.

    Signals (higher = more droppable): out/IR status, appearing in the
    league-wide trending-drop list, and being buried depth at a position.
    """
    trending_drop = await client.get_trending("drop", lookback_hours=48, limit=100)
    drop_velocity = {e["player_id"]: int(e.get("count") or 0) for e in trending_drop}

    grouped = my_players_by_position(ctx)
    # Rank within each position so we know who's buried.
    depth_rank: dict[str, int] = {}
    for pos, players in grouped.items():
        for i, p in enumerate(players):
            depth_rank[p["player_id"]] = i

    required = required_starters(ctx)
    scored: list[dict] = []
    for pid in ctx.my_roster.get("players") or []:
        p = ctx.players.get(pid)
        if not p:
            continue
        pos = p.get("position") or "?"
        score = 0.0
        reasons = []

        if is_out(p):
            score += 5
            reasons.append(f"{p.get('injury_status')}")
        if pid in drop_velocity:
            score += 2
            reasons.append("being dropped league-wide")

        rank = depth_rank.get(pid, 0)
        starters = required.get(pos, 0) + (required.get("FLEX", 0) if pos in {"RB", "WR", "TE"} else 0)
        if rank >= max(starters, 1) + 1:
            score += 1.5
            reasons.append(f"buried #{rank + 1} at {pos}")
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
