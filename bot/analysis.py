"""Strategy layer: turn raw Sleeper data into decisions.

This is where roster construction, positional needs, FAAB bids and drop
candidates are computed. The heuristics are intentionally simple and
explainable — every recommendation carries a human-readable reason. Sleeper
doesn't expose projections, so signals used are: market velocity (trending
adds/drops), injury status, and positional depth vs. required starters.
"""

import time
from dataclasses import dataclass, field
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
    if config.LEAGUE_ID:
        return config.LEAGUE_ID
    if user_id:
        leagues = await client.get_user_leagues(user_id, config.SEASON)
        if leagues:
            return leagues[0].get("league_id")
    return None


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
        season=config.SEASON,
        faab_total=faab_total,
        faab_used=faab_used,
        rostered_ids=rostered_ids,
    )
    _cache["ctx"] = ctx
    _cache["ts"] = now
    return ctx


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
