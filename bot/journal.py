"""Durable decision journal.

The point is to separate decision quality from outcome quality. Adding a
backup back before the starter got hurt was a good decision even if the
starter stayed healthy; starting a receiver who caught a 75-yard touchdown
after you projected him two targets was a bad one that happened to pay. A
system that learns only from fantasy points becomes results-oriented, and
results-oriented gets steadily worse.

So every meaningful call is written down with the information that was
available when it was made, and scored later against what actually happened —
with the two kept visibly separate.

Storage is a single JSON file, written atomically. On Railway that path should
be a mounted volume; without one the container is ephemeral and the journal
resets on redeploy, so `available()` reports which of those is true rather
than pretending the writes are durable.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Optional

from . import config

# Bounded so a season of entries can't grow without limit; oldest are dropped.
_MAX_ENTRIES = 500
_lock = threading.Lock()


def _path() -> str:
    return config.JOURNAL_PATH


def _load() -> list[dict]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(entries: list[dict]) -> bool:
    """Write atomically: a crash mid-write must not corrupt the journal."""
    path = _path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entries[-_MAX_ENTRIES:], fh, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def available() -> tuple[bool, str]:
    """(writable, human-readable note about durability)."""
    path = _path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        return False, f"not writable ({exc.__class__.__name__})"
    # A path inside the app directory disappears on redeploy; a mounted volume
    # does not. Worth saying which one this is.
    durable = not os.path.abspath(path).startswith(os.path.abspath(os.getcwd()))
    return True, ("durable volume" if durable else "EPHEMERAL — resets on redeploy")


def record(
    kind: str,
    week: int,
    summary: str,
    *,
    rationale: str = "",
    alternatives: Optional[list[str]] = None,
    expected: str = "",
    players: Optional[list[str]] = None,
    data: Optional[dict] = None,
) -> bool:
    """Write one decision, with the reasoning available at the time.

    `expected` is what we thought would happen — the field that makes a later
    review about process rather than hindsight.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "week": week,
        "kind": kind,
        "summary": summary[:600],
        "rationale": rationale[:800],
        "alternatives": (alternatives or [])[:6],
        "expected": expected[:400],
        "players": (players or [])[:12],
        "data": data or {},
        "outcome": None,
    }
    with _lock:
        entries = _load()
        entries.append(entry)
        return _save(entries)


def recent(limit: int = 10, kind: Optional[str] = None) -> list[dict]:
    entries = _load()
    if kind:
        entries = [e for e in entries if e.get("kind") == kind]
    return entries[-limit:][::-1]


def unscored(before_week: int) -> list[dict]:
    """Entries from completed weeks that have not been scored yet."""
    return [
        e
        for e in _load()
        if e.get("outcome") is None and int(e.get("week") or 0) < before_week
    ]


def score(ts: str, outcome: str) -> bool:
    """Attach what actually happened to one entry."""
    with _lock:
        entries = _load()
        for e in entries:
            if e.get("ts") == ts:
                e["outcome"] = outcome[:600]
                return _save(entries)
    return False


def summarize(entries: list[dict]) -> str:
    """Render entries for a chat message."""
    out = []
    for e in entries:
        line = f"<b>W{e.get('week')} · {e.get('kind')}</b> — {e.get('summary','')}"
        if e.get("expected"):
            line += f"\n   <i>expected: {e['expected']}</i>"
        if e.get("outcome"):
            line += f"\n   <i>actual: {e['outcome']}</i>"
        out.append(line)
    return "\n\n".join(out)


def recent_since(hours: float, kind: str) -> list[dict]:
    """Entries of one kind written within the last `hours`."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for e in _load():
        if e.get("kind") != kind:
            continue
        try:
            when = datetime.fromisoformat(e["ts"])
        except (KeyError, ValueError):
            continue
        if when >= cutoff:
            out.append(e)
    return out


def recent_alerts_context(hours: float = 12.0) -> str:
    """What the bot has already told the user recently.

    Without this each answer is written from scratch, so a news alert and a
    lineup call made minutes apart could contradict each other and neither
    would know. Feeding recent alerts back in gives every later answer the
    chance to either agree or say plainly what changed.
    """
    alerts = recent_since(hours, "alert")
    if not alerts:
        return ""
    lines = [
        "ALREADY TOLD THE USER, in the last few hours — these came from a news "
        "sweep, not from the full projections. If your answer now disagrees "
        "with one, say so explicitly and explain what you are weighing that "
        "the alert was not; do NOT silently contradict it:"
    ]
    for e in alerts[-4:]:
        lines.append(f"  [{e.get('ts','')[:16]}] {e.get('summary','')}")
    return "\n".join(lines)


# --- Small durable state, alongside the journal ------------------------------
# Detecting that something CHANGED requires remembering what it was. This is
# deliberately separate from the decision log: it is machine state, rewritten
# constantly, and it should not push decisions out of a bounded history.


def _state_path() -> str:
    base = _path()
    return base[:-5] + "_state.json" if base.endswith(".json") else base + ".state"


def load_state(key: str) -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get(key) or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        return {}


def save_state(key: str, value: dict) -> bool:
    path = _state_path()
    with _lock:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            if not isinstance(blob, dict):
                blob = {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            blob = {}
        blob[key] = value
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)
            os.replace(tmp, path)
            return True
        except OSError:
            return False
