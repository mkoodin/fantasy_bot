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

    async def _get(self, path: str) -> Any:
        def do() -> Any:
            resp = self._session.get(f"{BASE}{path}", timeout=25)
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

    # --- Player dictionary (cached) ----------------------------------------
    async def get_players(self, max_age: int = 86_400) -> dict[str, Any]:
        now = time.time()
        if self._players is not None and now - self._players_ts < max_age:
            return self._players
        data = await self._get("/players/nfl")
        self._players = data
        self._players_ts = now
        return data


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
