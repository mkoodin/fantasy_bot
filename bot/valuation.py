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

# Several of these helpers are called repeatedly while assembling one answer —
# the optimal lineup alone was recomputed five times per request, and the
# positional pools once per caller. They are pure functions of a LeagueContext
# that is itself rebuilt on a TTL, so memoizing per context is safe and removes
# the repeat work. Bounded so a long-running process cannot accumulate entries.
_MEMO: dict[tuple, Any] = {}
_MEMO_MAX = 64


def clear_memo() -> None:
    """Drop all cached derivations. Called whenever a context is rebuilt.

    Keys include id(ctx), and CPython reuses object ids after collection — so
    without this, a freshly built context could be served values derived from
    the one it replaced.
    """
    _MEMO.clear()


def _memo(ctx: Any, key: tuple, build):
    """Cache a derived value for the lifetime of one LeagueContext."""
    full = (id(ctx), *key)
    if full not in _MEMO:
        if len(_MEMO) >= _MEMO_MAX:
            _MEMO.clear()
        _MEMO[full] = build()
    return _MEMO[full]

from .sleeper import FANTASY_POSITIONS, player_name

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
    def build() -> list[tuple[float, str]]:
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

    return _memo(ctx, ("pool", position), build)


def replacement_levels(ctx: Any) -> dict[str, float]:
    """Projected points of the replacement-level player at each position.

    That's the player sitting just past league-wide starter demand plus a
    bench allowance — effectively the best guy on waivers. Value above this
    line is what a trade is actually negotiating over.
    """
    def build() -> dict[str, float]:
        return _replacement_levels(ctx)

    return _memo(ctx, ("levels",), build)


def _replacement_levels(ctx: Any) -> dict[str, float]:
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

    Returns player_id -> {vorp, score, base_score, points, replacement,
    injury_mult}. `score` is VORP normalized to 0-100 across the league and
    discounted for injury — what the player is worth to a lineup right now.
    `base_score` is the same number undiscounted: what he is worth when
    healthy.

    Keeping both matters. Pricing a start/sit call needs the discounted score,
    but deciding whether someone is expendable must not: an elite back landing
    on IR would otherwise be demoted out of your core and show up as a drop
    candidate, which is how you lose a season on a player you should stash.
    """
    levels = replacement_levels(ctx)
    raw: dict[str, dict] = {}
    for pos in VALUED_POSITIONS:
        replacement = levels.get(pos)
        if replacement is None:
            continue
        for pts, pid in _pool(ctx, pos):
            mult = injury_multiplier(ctx.players.get(pid))
            healthy_vorp = pts - replacement
            raw[pid] = {
                "points": round(pts, 1),
                "replacement": round(replacement, 1),
                "vorp": round(healthy_vorp * mult, 1),
                "healthy_vorp": round(healthy_vorp, 1),
                "injury_mult": mult,
            }

    # Normalize both scales against the same healthy top player, so an injury
    # moves a player down the board rather than rescaling everyone.
    best = max((v["healthy_vorp"] for v in raw.values()), default=0.0)
    for entry in raw.values():
        # Below-replacement players score 0: they're waiver fodder, and letting
        # them go negative would make throw-ins look like they cost something.
        def norm(v: float) -> float:
            return round(100.0 * max(0.0, v) / best, 1) if best > 0 else 0.0

        entry["score"] = norm(entry["vorp"])
        entry["base_score"] = norm(entry["healthy_vorp"])
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

    Deliberately uses healthy value, not this week's injury-discounted value —
    a hurt starter is someone you stash, not someone you cut.
    """
    roster = roster or ctx.my_roster
    by_pos: dict[str, list[tuple[float, str]]] = {}
    for pid in roster.get("players") or []:
        p = ctx.players.get(pid) or {}
        pos = p.get("position") or ""
        if pos not in VALUED_POSITIONS:
            continue
        score = (ctx.player_values.get(pid) or {}).get("base_score", 0.0)
        by_pos.setdefault(pos, []).append((score, pid))

    depth = starter_depth(ctx)
    # Anyone this week's lineup actually starts is a starter, whatever his
    # season-long value says. A flex-worthy player can project as replaceable
    # over a full season and still be the best option in a slot right now;
    # calling him expendable is how you end up recommending a cut and a start
    # for the same man in the same breath.
    lineup, _ = optimal_lineup(ctx, roster)
    starting = {e["player_id"] for e in lineup if e.get("player_id")}

    tiers: dict[str, str] = {}
    for pos, players in by_pos.items():
        players.sort(reverse=True)
        starters = depth.get(pos, 1)
        for i, (score, pid) in enumerate(players):
            if i < starters and score >= 60:
                tiers[pid] = "CORE"
            elif i < starters or pid in starting:
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


# --- Weekly decisions -------------------------------------------------------
# Season-long value answers "who is worth more". It is the wrong number for the
# decisions this bot actually makes most weeks: a lineup is set on THIS week's
# projection and matchup, and a waiver add is only worth making if it beats the
# player it displaces. Both are computed here so the model starts from a real
# baseline and spends its search budget on news instead of arithmetic.


def week_points(ctx: Any, pid: str) -> float:
    """This week's projected points, discounted for injury.

    The per-game fallback applies only when the weekly feed is missing
    ENTIRELY. When the feed loaded and this player simply isn't in it, he has
    no game — bye, or inactive — and is worth zero. Falling back to his season
    average there would rank a player on bye as a top starter, which is the one
    lineup mistake that costs a guaranteed zero.
    """
    pts = ctx.week_projections.get(pid)
    if pts is None:
        if ctx.week_projections:
            return 0.0
        season = ctx.season_projections.get(pid)
        pts = (season / 17.0) if season is not None else 0.0
    return pts * injury_multiplier(ctx.players.get(pid))


def optimal_lineup(ctx: Any, roster: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    """Fill every starting slot with the highest-projected eligible player.

    Fixed slots are filled first from the best players at that position, then
    flex slots from whoever is left — which is optimal here, not merely greedy:
    a player eligible for a fixed slot can only ever fill that slot or a flex,
    so taking the best at each position first never costs a better flex.

    Returns (starters, bench), each entry carrying the slot and projection.
    """
    roster = roster or ctx.my_roster
    if roster is ctx.my_roster:
        return _memo(ctx, ("lineup",), lambda: _optimal_lineup(ctx, roster))
    return _optimal_lineup(ctx, roster)


def _optimal_lineup(ctx: Any, roster: dict) -> tuple[list[dict], list[dict]]:
    from .analysis import BENCH_SLOTS, FLEX_ELIGIBILITY

    slots = [s for s in ctx.league.get("roster_positions", []) if s not in BENCH_SLOTS]
    available = sorted(
        (roster.get("players") or []),
        key=lambda pid: week_points(ctx, pid),
        reverse=True,
    )
    used: set[str] = set()
    starters: list[dict] = []

    def take(eligible: set[str], slot: str) -> None:
        for pid in available:
            if pid in used:
                continue
            pos = (ctx.players.get(pid) or {}).get("position")
            if pos in eligible:
                used.add(pid)
                starters.append(
                    {"slot": slot, "player_id": pid, "points": round(week_points(ctx, pid), 1)}
                )
                return
        starters.append({"slot": slot, "player_id": None, "points": 0.0})

    for slot in slots:
        if slot in FLEX_ELIGIBILITY:
            continue
        take({slot}, slot)
    for slot in slots:
        if slot in FLEX_ELIGIBILITY:
            take(FLEX_ELIGIBILITY[slot], slot)

    bench = [
        {"player_id": pid, "points": round(week_points(ctx, pid), 1)}
        for pid in available
        if pid not in used
    ]
    return starters, bench


def lineup_context(ctx: Any) -> str:
    """The projection-optimal lineup for this week, plus the calls to check.

    Given to the model as a starting point, not a verdict: it holds the
    arithmetic steady so the live search is spent on what the numbers can't
    see — snap counts, weather, a beat reporter's Friday practice note.
    """
    starters, bench = optimal_lineup(ctx)
    if not starters:
        return ""

    basis = "this week's projections" if ctx.week_projections else (
        "season projections scaled per game (no weekly feed available)"
    )
    lines = [
        f"PROJECTION-OPTIMAL LINEUP for Week {ctx.week}, computed from {basis} "
        "and discounted for injury status. This is the arithmetic baseline "
        "ONLY — it knows nothing about matchup, weather, snap trends or news. "
        "Start here, then move players based on what your search actually "
        "finds, and say what made you deviate:"
    ]
    total = 0.0
    for entry in starters:
        pid = entry["player_id"]
        if not pid:
            lines.append(f"  {entry['slot']}: (empty — no eligible player)")
            continue
        p = ctx.players.get(pid) or {}
        flag = f" ⚠{p['injury_status']}" if p.get("injury_status") else ""
        if is_unavailable_this_week(ctx, pid):
            # Starting a player on bye is a self-inflicted zero, and it is the
            # one lineup mistake no amount of matchup analysis recovers from.
            flag += " ⛔ NO GAME THIS WEEK — BYE OR INACTIVE, DO NOT START"
        total += entry["points"]
        lines.append(
            f"  {entry['slot']}: {player_name(p)} "
            f"({p.get('position', '?')}-{p.get('team', 'FA')}) "
            f"{entry['points']}pts{flag}"
        )
    lines.append(f"  Projected total: {round(total, 1)}pts")

    if bench:
        lines.append("  Bench: " + ", ".join(
            f"{player_name(ctx.players.get(b['player_id']) or {})} {b['points']}pts"
            for b in bench[:8]
        ))

    # A bench player within a couple of points of a starter is the decision
    # worth surfacing — that's where news actually changes the answer.
    close: list[str] = []
    for entry in starters:
        if not entry["player_id"]:
            continue
        slot_pos = (ctx.players.get(entry["player_id"]) or {}).get("position")
        for b in bench:
            bp = ctx.players.get(b["player_id"]) or {}
            if bp.get("position") != slot_pos:
                continue
            gap = entry["points"] - b["points"]
            if 0 <= gap <= 3:
                close.append(
                    f"{player_name(ctx.players.get(entry['player_id']) or {})} "
                    f"({entry['points']}) over {player_name(bp)} ({b['points']}) "
                    f"— only {round(gap, 1)}pts apart"
                )
    if close:
        lines.append(
            "  CLOSE CALLS worth resolving with news rather than projections: "
            + "; ".join(close[:5])
        )
    return "\n".join(lines)


def roster_hole_value(ctx: Any, position: str) -> float:
    """The value of the player a new add at this position would displace.

    Adding is only an upgrade relative to whoever currently occupies the slot,
    so this is the bar a waiver claim has to clear.
    """
    depth = starter_depth(ctx).get(position, 1)
    owned = sorted(
        (
            (ctx.player_values.get(pid) or {}).get("score", 0.0)
            for pid in (ctx.my_roster.get("players") or [])
            if (ctx.players.get(pid) or {}).get("position") == position
        ),
        reverse=True,
    )
    if len(owned) < depth:
        return 0.0  # An empty starting slot — anyone is an upgrade.
    return owned[depth - 1]


def upgrade_over_roster(ctx: Any, pid: str) -> float:
    """How much a free agent would improve your starting lineup, in value."""
    pos = (ctx.players.get(pid) or {}).get("position") or ""
    if pos not in VALUED_POSITIONS:
        return 0.0
    score = (ctx.player_values.get(pid) or {}).get("score", 0.0)
    return round(score - roster_hole_value(ctx, pos), 1)


def replacement_ranks(ctx: Any) -> dict[str, int]:
    """Which rank at each position the replacement line falls on (e.g. RB35).

    Points alone can't be eyeballed for correctness, but ranks can: a 12-team
    league should land near RB35 / WR47 / TE15 / QB14, and anything wildly off
    means an input is wrong — most likely that market rank was read backwards.
    """
    out: dict[str, int] = {}
    for pos, level in replacement_levels(ctx).items():
        pool = _pool(ctx, pos)
        idx = next((i for i, (pts, _) in enumerate(pool, 1) if pts <= level), None)
        if idx:
            out[pos] = idx
    return out


def worst_rosterable(ctx: Any) -> Optional[tuple[str, float]]:
    """The player you would actually drop to make room, and his value.

    A waiver add is only worth making if it beats the man it displaces on the
    roster — not the abstract replacement level. Returns None when there is
    nobody droppable, which means any add costs you a real player.
    """
    lineup, _ = optimal_lineup(ctx)
    starting = {e["player_id"] for e in lineup if e.get("player_id")}
    candidates = []
    for pid in ctx.my_roster.get("players") or []:
        if pid in set(ctx.my_roster.get("reserve") or []):
            continue
        if pid in starting:
            # Includes the K and DEF this league requires: they carry no value
            # score, so without this they look like free real estate.
            continue
        tier = ctx.roster_tiers.get(pid)
        if tier in ("CORE", "STARTER"):
            continue
        score = (ctx.player_values.get(pid) or {}).get("base_score", 0.0)
        candidates.append((score, pid))
    if not candidates:
        return None
    score, pid = min(candidates)
    return pid, score


def competing_teams(ctx: Any, position: str, candidate_value: float) -> list[str]:
    """Rival teams this player would actually upgrade, who can still bid.

    Bid pressure is not "who is thin at the position" — every team is thin
    somewhere. It is who would put this specific player in their starting
    lineup, which is the same upgrade test applied to their roster instead of
    yours. A team already starting better players will not spend on him.
    """
    depth = starter_depth(ctx).get(position, 1)
    out: list[str] = []
    for r in ctx.rosters:
        if r.get("owner_id") == ctx.my_user_id:
            continue
        used = int((r.get("settings") or {}).get("waiver_budget_used") or 0)
        if ctx.faab_total - used <= 0:
            continue
        owned = sorted(
            (
                (ctx.player_values.get(pid) or {}).get("score", 0.0)
                for pid in (r.get("players") or [])
                if (ctx.players.get(pid) or {}).get("position") == position
            ),
            reverse=True,
        )
        # The player he would displace in their lineup; 0 if they can't fill it.
        theirs = owned[depth - 1] if len(owned) >= depth else 0.0
        if candidate_value > theirs:
            out.append(ctx.team_name(r.get("owner_id", "")))
    return out


# --- Why a replacement-level player might still be worth a spot -------------
# Value over replacement is the right tool for comparing starters and pricing
# trades, but it deliberately floors everyone below the waiver line at zero.
# That makes it useless for the most common waiver question there is: both
# players score 0, so which one do I actually want on my bench? That question
# is not about current points at all — it is about role, upside and what the
# roster already has.


def upside_flags(ctx: Any, pid: str) -> list[str]:
    """Reasons a below-replacement free agent may still deserve a roster spot."""
    p = ctx.players.get(pid) or {}
    pos, team = p.get("position"), p.get("team")
    flags: list[str] = []

    # The classic lottery ticket: the backup to a back you are relying on.
    # If your starter goes down, his handcuff inherits a starting job outright.
    if pos in ("RB", "WR", "TE"):
        for mine in ctx.my_roster.get("players") or []:
            if mine == pid:
                continue
            mp = ctx.players.get(mine) or {}
            if mp.get("team") != team or mp.get("position") != pos:
                continue
            if ctx.roster_tiers.get(mine) in ("CORE", "STARTER"):
                flags.append(f"handcuff to your {player_name(mp)}")
                break

    # The same backup on someone else's stud is insurance you can hold against
    # them, and it costs a bench spot rather than a bid.
    if not flags and pos == "RB":
        for r in ctx.rosters:
            if r.get("owner_id") == ctx.my_user_id:
                continue
            for their in r.get("players") or []:
                tp = ctx.players.get(their) or {}
                if tp.get("team") != team or tp.get("position") != pos:
                    continue
                if (ctx.player_values.get(their) or {}).get("base_score", 0.0) >= 55:
                    flags.append(f"handcuff to {player_name(tp)} ({ctx.team_name(r.get('owner_id',''))})")
                    break
            if flags:
                break

    dc = depth_chart(ctx, pid)
    if dc:
        order, slot = dc
        if order == 1:
            # First string but priced as a free agent: the market has not
            # repriced him yet, which is precisely the window worth taking.
            flags.append(f"listed FIRST STRING at {slot} — market hasn't caught up")
        elif order == 2:
            flags.append(f"next man up at {slot} (depth chart #2)")

    exp = p.get("years_exp")
    if isinstance(exp, int) and exp <= 1:
        flags.append("rookie/2nd-year — role can still grow")

    # A position you are genuinely thin at is worth a flier; one you are deep
    # at is not, however appealing the player.
    if pos in VALUED_POSITIONS:
        depth = starter_depth(ctx).get(pos, 1)
        owned = sum(
            1
            for x in (ctx.my_roster.get("players") or [])
            if (ctx.players.get(x) or {}).get("position") == pos
        )
        if owned <= depth:
            flags.append(f"you carry no spare {pos}")

    week = ctx.week_projections.get(pid)
    if week is not None and week >= 8:
        flags.append(f"already projected {week} this week")
    return flags


def dead_weight_flags(ctx: Any, pid: str) -> list[str]:
    """Reasons a player you roster may be holding a spot for nothing."""
    p = ctx.players.get(pid) or {}
    pos = p.get("position")
    flags: list[str] = []

    if pos in ("K", "DEF"):
        same = [
            x
            for x in (ctx.my_roster.get("players") or [])
            if (ctx.players.get(x) or {}).get("position") == pos
        ]
        if len(same) > 1:
            flags.append(f"you roster {len(same)} {pos}s and start one")

    season = ctx.season_projections.get(pid)
    if season is not None and season <= 20:
        flags.append(f"projected just {season} points all season")

    if p.get("injury_status") and not is_out_eligible(ctx, pid):
        flags.append(f"{p['injury_status']} with no IR slot to hide him")

    if pos in VALUED_POSITIONS:
        ranked = sorted(
            (
                ((ctx.player_values.get(x) or {}).get("base_score", 0.0), x)
                for x in (ctx.my_roster.get("players") or [])
                if (ctx.players.get(x) or {}).get("position") == pos
            ),
            reverse=True,
        )
        spot = next((i for i, (_, x) in enumerate(ranked, 1) if x == pid), None)
        depth = starter_depth(ctx).get(pos, 1)
        if spot and spot > depth + 1:
            flags.append(f"your #{spot} {pos} behind {depth} starters")
    return flags


def is_out_eligible(ctx: Any, pid: str) -> bool:
    """True if this player could be stashed on IR rather than cut."""
    st = ctx.league.get("settings") or {}
    if not st.get("reserve_slots"):
        return False
    if len(ctx.my_roster.get("reserve") or []) >= int(st["reserve_slots"]):
        return False
    status = (ctx.players.get(pid) or {}).get("injury_status") or ""
    allowed = {"IR"}
    for key, label in (
        ("reserve_allow_out", "Out"),
        ("reserve_allow_doubtful", "Doubtful"),
        ("reserve_allow_sus", "Sus"),
        ("reserve_allow_na", "NA"),
        ("reserve_allow_dnr", "DNR"),
    ):
        if st.get(key):
            allowed.add(label)
    return status in allowed


# --- Depth chart and availability ------------------------------------------
def depth_chart(ctx: Any, pid: str) -> Optional[tuple[int, str]]:
    """(order, slot) from Sleeper's depth chart, e.g. (1, 'RB') or (2, 'LWR').

    Order 1 is the starter. This is the field that tells you a rookie has
    climbed to first string before the projections or the market catch up,
    which is the whole reason to watch it.
    """
    p = ctx.players.get(pid) or {}
    order = p.get("depth_chart_order")
    slot = p.get("depth_chart_position")
    if not isinstance(order, int) or not slot:
        return None
    return order, str(slot)


def is_unavailable_this_week(ctx: Any, pid: str) -> bool:
    """True when a rostered player has no game projected — bye, or inactive.

    Sleeper's player feed carries no bye week, but the weekly projection feed
    simply omits a player who isn't playing. Only meaningful when weekly
    projections loaded at all, otherwise everyone would look benched.
    """
    if not ctx.week_projections:
        return False
    if (ctx.season_projections.get(pid) or 0) < 20:
        return False  # Never projected for anything; absence says nothing.
    return not ctx.week_projections.get(pid)
