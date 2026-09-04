"""Thin async wrapper around Sleeper's free, read-only public API.

Sleeper needs no auth key for read access. The one heavy call is the full
player dictionary (~5MB); we fetch it at most once a day and keep it in
memory for the life of the process.

Docs: https://docs.sleeper.com/
"""

import asyncio
import time
from typing import Any, Optional

import requests

BASE = "https://api.sleeper.app/v1"

# Projections live outside the documented /v1 API and Sleeper has served them
# from both hosts over time. We try each in turn and treat the whole feature
# as best-effort: if none answer, callers simply get no projections.
_PROJECTION_HOSTS = ("https://api.sleeper.com", "https://api.sleeper.app")

# Positions we treat as fantasy-relevant when scanning free agents.
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}

# injury_status values that mean a player probably can't help you this week.
OUT_STATUSES = {"Out", "IR", "PUP", "Sus", "Suspended", "DNR", "NA"}


class SleeperClient:
    """One shared instance per process. Methods are async so they play nice
    with the Telegram event loop; the actual HTTP happens in a thread."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._players: Optional[dict[str, Any]] = None
        self._players_ts: float = 0.0
        # Projections are keyed by (season, week) — week None means season-long.
        self._projections: dict[tuple[str, Optional[int]], Any] = {}
        self._projections_ts: dict[tuple[str, Optional[int]], float] = {}

    async def _get(self, path: str) -> Any:
        def do() -> Any:
            resp = self._session.get(f"{BASE}{path}", timeout=25)
            resp.raise_for_status()
            return resp.json()

        return await asyncio.to_thread(do)

    async def _get_url(self, url: str) -> Any:
        """Fetch an absolute URL (for endpoints outside the /v1 base)."""
        def do() -> Any:
            resp = self._session.get(url, timeout=25)
            resp.raise_for_status()
            return resp.json()

        return await asyncio.to_thread(do)

    # --- Simple endpoints ---------------------------------------------------
    async def get_nfl_state(self) -> dict:
        """Current season/week metadata: {'week', 'season', 'season_type', ...}."""
        return await self._get("/state/nfl")

    async def get_user(self, username_or_id: str) -> Optional[dict]:
        return await self._get(f"/user/{username_or_id}")

    async def get_user_leagues(self, user_id: str, season: str) -> list[dict]:
        return await self._get(f"/user/{user_id}/leagues/nfl/{season}")

    async def get_league(self, league_id: str) -> dict:
        return await self._get(f"/league/{league_id}")

    async def get_rosters(self, league_id: str) -> list[dict]:
        return await self._get(f"/league/{league_id}/rosters")

    async def get_league_users(self, league_id: str) -> list[dict]:
        return await self._get(f"/league/{league_id}/users")

    async def get_matchups(self, league_id: str, week: int) -> list[dict]:
        return await self._get(f"/league/{league_id}/matchups/{week}")

    async def get_transactions(self, league_id: str, week: int) -> list[dict]:
        return await self._get(f"/league/{league_id}/transactions/{week}")

    async def get_trending(
        self, kind: str = "add", lookback_hours: int = 24, limit: int = 25
    ) -> list[dict]:
        """kind is 'add' or 'drop'. Returns [{'player_id', 'count'}, ...]."""
        return await self._get(
            f"/players/nfl/trending/{kind}?lookback_hours={lookback_hours}&limit={limit}"
        )

    # --- Draft ---------------------------------------------------------------
    async def get_league_drafts(self, league_id: str) -> list[dict]:
        """All drafts for a league, newest first per Sleeper's ordering."""
        return await self._get(f"/league/{league_id}/drafts")

    async def get_draft_picks(self, draft_id: str) -> list[dict]:
        """Every pick in a draft: round, pick_no, roster_id, player_id, metadata."""
        return await self._get(f"/draft/{draft_id}/picks")

    # --- Actual stats (undocumented, best-effort) ---------------------------
    async def get_stats(
        self, season: str, week: Optional[int] = None, max_age: int = 1_800
    ) -> list[dict]:
        """Real box-score and usage stats. Week None = season totals.

        Same undocumented endpoint family as projections, and treated the same
        way: any failure returns [] and callers carry on. This is where the
        participation data lives — offensive snaps, targets, carries, red-zone
        looks — which is what actually predicts next week.
        """
        return await self._stat_feed("stats", season, week, max_age)

    # --- Projections (undocumented, best-effort) -----------------------------
    async def get_projections(
        self,
        season: str,
        week: Optional[int] = None,
        max_age: int = 3_600,
    ) -> list[dict]:
        """Sleeper's own player projections. Week None = season-long totals.

        This endpoint isn't part of the documented API, so it is treated as a
        bonus signal: any failure returns [] and the caller carries on without
        projections rather than breaking the whole answer.
        """
        return await self._stat_feed("projections", season, week, max_age)

    async def _stat_feed(
        self, kind: str, season: str, week: Optional[int], max_age: int
    ) -> list[dict]:
        """Shared fetch for the projections and stats feeds."""
        key = (kind, str(season), week)
        now = time.time()
        cached = self._projections.get(key)
        if cached is not None and now - self._projections_ts.get(key, 0.0) < max_age:
            return cached

        positions = "".join(f"&position[]={p}" for p in sorted(FANTASY_POSITIONS))
        path = f"/{kind}/nfl/{season}"
        if week is not None:
            path += f"/{week}"
        query = f"?season_type=regular&order_by=ppr{positions}"

        rows: list[dict] = []
        for host in _PROJECTION_HOSTS:
            try:
                data = await self._get_url(f"{host}{path}{query}")
            except Exception:
                continue
            # Sleeper has returned both a bare list and a player_id-keyed dict.
            if isinstance(data, dict):
                data = [
                    {**v, "player_id": v.get("player_id", k)}
                    for k, v in data.items()
                    if isinstance(v, dict)
                ]
            if isinstance(data, list) and data:
                rows = data
                break

        self._projections[key] = rows
        self._projections_ts[key] = now
        return rows

    # --- Player dictionary (cached) ----------------------------------------
    async def get_players(self, max_age: int = 86_400) -> dict[str, Any]:
        now = time.time()
        if self._players is not None and now - self._players_ts < max_age:
            return self._players
        data = await self._get("/players/nfl")
        self._players = data
        self._players_ts = now
        return data


def projected_points(row: dict, scoring_key: str = "pts_ppr") -> Optional[float]:
    """Pull the league-appropriate projected points out of a projection row."""
    stats = row.get("stats") if isinstance(row.get("stats"), dict) else row
    if not isinstance(stats, dict):
        return None
    for key in (scoring_key, "pts_ppr", "pts_half_ppr", "pts_std"):
        val = stats.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def player_name(player: Optional[dict]) -> str:
    """Best-effort display name from a Sleeper player record."""
    if not player:
        return "Unknown"
    if player.get("full_name"):
        return player["full_name"]
    first, last = player.get("first_name"), player.get("last_name")
    if first or last:
        return f"{(first or '').strip()} {(last or '').strip()}".strip()
    # Team defenses are keyed by team abbreviation with no name fields.
    return player.get("player_id", "Unknown")


def is_out(player: Optional[dict]) -> bool:
    """True if the player is unlikely to be usable (IR/Out/etc.)."""
    if not player:
        return False
    return (player.get("injury_status") or "") in OUT_STATUSES
