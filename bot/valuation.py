"""Player valuation: turn raw Sleeper data into comparable prices.

The problem this solves: a roster listing tells you who owns whom, but nothing
about what anyone is worth, so trade advice ends up anchored on a model's
memory of last season. Everything here produces *comparable numbers* instead.

The core idea is value over replacement (VORP). Raw projected points can't be
compared across positions — a QB outscores every running back and is still the
cheapest starter to replace, because the 12th-best QB is nearly as good as the
5th. What matters is the gap between a player and the freely-available player
who would take his slot. That baseline depends on the league: how many teams,
how many starters at each position, how many flex slots and who can fill them.
All of that is read from the league's own settings rather than assumed.

Layered on top:
  * an injury discount, so a player who can't play isn't priced as if he can;
  * a market-rank prior, which carries the valuation when projections are
    missing and tempers it when they disagree with consensus;
  * roster fit, which is what makes a player expendable — the third running
    back on a two-RB roster is worth less to you than his market price.
"""

import math
from typing import Any, Optional

from .sleeper import FANTASY_POSITIONS, is_out, player_name

# Positions we can meaningfully value. K and DEF are streamed, not traded.
VALUED_POSITIONS = ("QB", "RB", "WR", "TE")

# How a flex slot splits across the positions eligible to fill it. Flex is
# overwhelmingly RB/WR in practice, with a minority of TEs; these priors are
# renormalized over whichever positions a given slot actually allows.
_FLEX_PRIOR = {"QB": 0.05, "RB": 0.45, "WR": 0.45, "TE": 0.10}

# Teams carry backups beyond their starters, so the true replacement player
# sits slightly deeper than pure starter demand implies.
_BENCH_PADDING = {"QB": 2, "RB": 6, "WR": 6, "TE": 2}

# Value multipliers for injury designations. An Out player still has trade
# value (he comes back), but he is not worth his healthy price today.
_INJURY_DISCOUNT = {
    "IR": 0.45,
    "PUP": 0.45,
    "NA": 0.5,
    "Sus": 0.5,
    "Suspended": 0.5,
    "DNR": 0.4,
    "Out": 0.6,
    "Doubtful": 0.7,
    "Questionable": 0.92,
}

# Shape-only fallback when projections are unavailable: typical season points
# for the #1 player at a position, and how fast that decays down the ranks.
# These produce a sane *ordering and spacing*, not real projections.
_SYNTHETIC_TOP = {"QB": 380.0, "RB": 300.0, "WR": 300.0, "TE": 230.0}
_SYNTHETIC_DECAY = {"QB": 22.0, "RB": 14.0, "WR": 16.0, "TE": 9.0}


def _synthetic_points(position: str, pos_rank: int) -> float:
    """Approximate season points from a positional rank.

    Used only when real projections are missing. Exponential decay matches the
    actual shape of fantasy scoring far better than a straight line: the gap
    between RB1 and RB6 dwarfs the gap between RB30 and RB35.
    """
    top = _SYNTHETIC_TOP.get(position, 250.0)
    decay = _SYNTHETIC_DECAY.get(position, 14.0)
    return top * math.exp(-(max(1, pos_rank) - 1) / decay)


def expected_starts(ctx: Any) -> dict[str, float]:
    """How many players at each position the whole league starts each week.

    This is the demand side of replacement level, read from the league's real
    roster_positions: fixed slots plus each position's expected share of the
    flex slots it's eligible for.
    """
    from .analysis import BENCH_SLOTS, FLEX_ELIGIBILITY

    n_teams = max(1, len(ctx.rosters))
    slots = [s for s in ctx.league.get("roster_positions", []) if s not in BENCH_SLOTS]

    per_team: dict[str, float] = {p: 0.0 for p in VALUED_POSITIONS}
    for slot in slots:
        if slot in per_team:
            per_team[slot] += 1.0
            continue
        eligible = FLEX_ELIGIBILITY.get(slot)
        if not eligible:
            continue
        weights = {p: _FLEX_PRIOR.get(p, 0.0) for p in eligible if p in per_team}
        total = sum(weights.values())
        if total <= 0:
            continue
        for pos, w in weights.items():
            per_team[pos] += w / total

    return {pos: cnt * n_teams for pos, cnt in per_team.items()}


def _pool(ctx: Any, position: str) -> list[tuple[float, str]]:
    """Every valuable player at a position, as (season points, player_id).

    Projections are used where Sleeper served them; anyone missing falls back
    to the synthetic curve off their market rank, so the pool stays complete
    and the replacement baseline doesn't drift just because a projection is
    absent.
    """
    out: list[tuple[float, str]] = []
    for pid, ranks in ctx.market_ranks.items():
        p = ctx.players.get(pid) or {}
        if (p.get("position") or "") != position:
            continue
        pts = ctx.season_projections.get(pid)
        if pts is None:
            pts = _synthetic_points(position, ranks[0])
        out.append((float(pts), pid))
    out.sort(reverse=True)
    return out


def replacement_levels(ctx: Any) -> dict[str, float]:
    """Projected points of the replacement-level player at each position.

    That's the player sitting just past league-wide starter demand plus a
    bench allowance — effectively the best guy on waivers. Value above this
    line is what a trade is actually negotiating over.
    """
    demand = expected_starts(ctx)
    levels: dict[str, float] = {}
    for pos in VALUED_POSITIONS:
        pool = _pool(ctx, pos)
        if not pool:
            continue
        idx = int(round(demand.get(pos, 0.0))) + _BENCH_PADDING.get(pos, 4)
        idx = min(max(idx, 1), len(pool))
        levels[pos] = pool[idx - 1][0]
    return levels


def injury_multiplier(player: Optional[dict]) -> float:
    if not player:
        return 1.0
    return _INJURY_DISCOUNT.get(player.get("injury_status") or "", 1.0)


def compute_values(ctx: Any) -> dict[str, dict]:
    """Price every valuable player in the league.

    Returns player_id -> {vorp, score, points, replacement, injury_mult}, where
    `score` is VORP normalized to a 0-100 scale across the whole league so that
    a quarterback and a wide receiver can be compared directly. Scores are the
    number the trade logic and the model both reason over.
    """
    levels = replacement_levels(ctx)
    raw: dict[str, dict] = {}
    for pos in VALUED_POSITIONS:
        replacement = levels.get(pos)
        if replacement is None:
            continue
        for pts, pid in _pool(ctx, pos):
            mult = injury_multiplier(ctx.players.get(pid))
            vorp = (pts - replacement) * mult
            raw[pid] = {
                "points": round(pts, 1),
                "replacement": round(replacement, 1),
                "vorp": round(vorp, 1),
                "injury_mult": mult,
            }

    best = max((v["vorp"] for v in raw.values()), default=0.0)
    for entry in raw.values():
        # Below-replacement players score 0: they're waiver fodder, and letting
        # them go negative would make throw-ins look like they cost something.
        entry["score"] = (
            round(100.0 * max(0.0, entry["vorp"]) / best, 1) if best > 0 else 0.0
        )
    return raw


# --- Roster fit -------------------------------------------------------------
def starter_depth(ctx: Any) -> dict[str, int]:
    """How many of each position one team actually starts, flex included."""
    demand = expected_starts(ctx)
    n_teams = max(1, len(ctx.rosters))
    return {pos: max(1, int(round(cnt / n_teams))) for pos, cnt in demand.items()}


def roster_tiers(ctx: Any, roster: Optional[dict] = None) -> dict[str, str]:
    """Label each player on a roster CORE / STARTER / DEPTH / EXPENDABLE.

    Expendability is the missing half of trade advice: knowing James Cook is
    valuable isn't enough if the model doesn't also know he's your RB1 and the
    guy behind your third receiver is the one to trade. Ranked within position
    against how many that slot actually starts.
    """
    roster = roster or ctx.my_roster
    by_pos: dict[str, list[tuple[float, str]]] = {}
    for pid in roster.get("players") or []:
        p = ctx.players.get(pid) or {}
        pos = p.get("position") or ""
        if pos not in VALUED_POSITIONS:
            continue
        score = (ctx.player_values.get(pid) or {}).get("score", 0.0)
        by_pos.setdefault(pos, []).append((score, pid))

    depth = starter_depth(ctx)
    tiers: dict[str, str] = {}
    for pos, players in by_pos.items():
        players.sort(reverse=True)
        starters = depth.get(pos, 1)
        for i, (score, pid) in enumerate(players):
            if i < starters and score >= 60:
                tiers[pid] = "CORE"
            elif i < starters:
                tiers[pid] = "STARTER"
            elif score <= 0:
                tiers[pid] = "EXPENDABLE"
            else:
                tiers[pid] = "DEPTH"
    return tiers


# --- Trade evaluation -------------------------------------------------------
def find_player(ctx: Any, name: str) -> Optional[str]:
    """Resolve a typed name to a player_id, preferring rostered players.

    Names arrive from chat, so matching is loose: exact first, then substring,
    with rostered and higher-valued players winning ties over the long tail of
    practice-squad namesakes.
    """
    want = " ".join(name.lower().split())
    if not want:
        return None

    def rank(pid: str) -> tuple:
        score = (ctx.player_values.get(pid) or {}).get("score", 0.0)
        return (pid in ctx.rostered_ids, score)

    exact: list[str] = []
    partial: list[str] = []
    for pid, p in ctx.players.items():
        if (p.get("position") or "") not in FANTASY_POSITIONS:
            continue
        full = player_name(p).lower()
        if full == want:
            exact.append(pid)
        elif want and want in full:
            partial.append(pid)
    pool = exact or partial
    return max(pool, key=rank) if pool else None


def evaluate_trade(
    ctx: Any, send_ids: list[str], receive_ids: list[str]
) -> dict:
    """Score both sides of a proposed trade.

    Two numbers matter and they answer different questions. Market value is
    whether the other manager would ever accept. Roster value is whether you
    should want it: a player only helps you as much as the player he displaces
    in your lineup, so acquiring a fourth good receiver is worth less to you
    than his market price, and trading from surplus is close to free.
    """
    def market(pids: list[str]) -> float:
        return round(sum((ctx.player_values.get(p) or {}).get("score", 0.0) for p in pids), 1)

    def describe(pids: list[str]) -> list[str]:
        out = []
        for pid in pids:
            p = ctx.players.get(pid) or {}
            v = ctx.player_values.get(pid) or {}
            tier = ctx.roster_tiers.get(pid)
            out.append(
                f"{player_name(p)} ({p.get('position', '?')}) "
                f"value {v.get('score', 0.0)}"
                + (f", {tier} on your roster" if tier else "")
            )
        return out

    send_val, recv_val = market(send_ids), market(receive_ids)
    total = send_val + recv_val
    # Gap as a share of the whole deal, so a 20-point edge reads differently on
    # a blockbuster than on two bench pieces.
    gap_pct = round(100.0 * (recv_val - send_val) / total, 1) if total > 0 else 0.0

    if total < 10:
        # Two throw-ins. The percentage gap between 6 and 0 is enormous and
        # meaningless — neither side is giving up anything that matters.
        verdict = "IMMATERIAL — both sides are waiver-level; this changes nothing"
    elif abs(gap_pct) <= 7:
        verdict = "FAIR — close enough that either side could reasonably accept"
    elif gap_pct > 25:
        verdict = "LOPSIDED IN YOUR FAVOR — they will almost certainly decline"
    elif gap_pct > 7:
        verdict = "FAVORS YOU — a realistic opening offer"
    elif gap_pct < -25:
        verdict = "LOPSIDED AGAINST YOU — do not send this"
    else:
        verdict = "FAVORS THEM — ask for more back"

    return {
        "send": describe(send_ids),
        "receive": describe(receive_ids),
        "send_value": send_val,
        "receive_value": recv_val,
        "gap_pct": gap_pct,
        "verdict": verdict,
    }


def trade_posture_context(ctx: Any) -> str:
    """Which of your players are actually available to trade, and which aren't.

    Written into the model's context so an offer gets built out of surplus
    instead of out of the roster's best player.
    """
    if not ctx.roster_tiers:
        return ""
    groups: dict[str, list[str]] = {}
    for pid, tier in ctx.roster_tiers.items():
        p = ctx.players.get(pid) or {}
        score = (ctx.player_values.get(pid) or {}).get("score", 0.0)
        groups.setdefault(tier, []).append(f"{player_name(p)} ({score})")

    lines = [
        "YOUR TRADE POSTURE — computed from this league's starter requirements "
        "and each player's value over a replacement-level waiver add. The "
        "number in parentheses is that value on a 0-100 league-wide scale:"
    ]
    labels = {
        "CORE": "UNTOUCHABLE — league-winners and the spine of your lineup. Do "
                "not offer these unless the return is clearly larger, and say "
                "why if you do",
        "STARTER": "STARTING but not untouchable — tradeable in a deal that "
                   "upgrades the same slot or fills a real hole",
        "DEPTH": "REAL DEPTH — your natural trade chips, worth something to "
                 "another roster but not to your starting lineup",
        "EXPENDABLE": "EXPENDABLE — below replacement level; throw-ins and "
                      "drop candidates, not the centerpiece of any offer",
    }
    for tier in ("CORE", "STARTER", "DEPTH", "EXPENDABLE"):
        if groups.get(tier):
            lines.append(f"  {labels[tier]}: {', '.join(sorted(groups[tier]))}")
    lines.append(
        "Build offers from DEPTH and EXPENDABLE first. Sending a CORE player "
        "for anything less than a clear upgrade is a mistake — if you propose "
        "one, state the value on both sides and justify it explicitly."
    )
    return "\n".join(lines)
