"""Central configuration, loaded from environment variables.

Everything the bot needs is read here once so the rest of the code never
touches os.getenv directly. On Railway these are set in the service's
Variables tab; locally you can use a .env file (see .env.example).
"""

import os
from datetime import datetime, time
from zoneinfo import ZoneInfo


def _load_dotenv() -> None:
    """Minimal .env loader so local runs don't need python-dotenv.

    Only sets vars that aren't already in the environment. Silently does
    nothing if there's no .env file (the normal case on Railway).
    """
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()


def _parse_time(value: str, default: str) -> time:
    """Parse 'HH:MM' into a tz-aware time in the configured timezone."""
    hh, mm = (value or default).split(":")
    return time(hour=int(hh), minute=int(mm), tzinfo=TIMEZONE)


# --- Telegram ---------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
# The single chat the bot is allowed to talk to / pushes digests to.
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Sleeper ----------------------------------------------------------------
# Provide either your username (bot resolves the id) and/or an explicit id.
SLEEPER_USERNAME = os.getenv("SLEEPER_USERNAME")
# These are public identifiers (not secrets), so they're baked in as defaults
# for this league — the bot works even if the host's env vars aren't set.
# Override via env vars to point at a different team/league.
SLEEPER_USER_ID = os.getenv("SLEEPER_USER_ID", "1267645505593147392")  # Koodini
# Leave LEAGUE_ID unset to AUTO-DETECT: the bot finds your league by
# SLEEPER_USER_ID + LEAGUE_NAME for the current NFL season, falling back to
# recent past seasons in the offseason — so it follows you into a new league
# each year with zero edits. Set LEAGUE_ID to pin a specific league instead.
LEAGUE_ID = os.getenv("LEAGUE_ID")
LEAGUE_NAME = os.getenv("LEAGUE_NAME", "Show me your TDs")
# Season is derived from the resolved league; this is only a display fallback.
SEASON = os.getenv("SEASON", str(datetime.now().year))

# --- League strategy --------------------------------------------------------
# redraft | dynasty | keeper. Steers Grok toward this-season vs. long-term
# value. Redraft => discount dynasty/rookie-stash takes, favor win-now.
LEAGUE_FORMAT = os.getenv("LEAGUE_FORMAT", "redraft")

# X analysts to weight in Grok's Live Search reads. The full list is passed to
# the model as "trusted voices" — search still runs broadly so breaking
# beat-reporter news isn't missed (hard handle-filtering would cap at 20 and
# hide non-listed reporters). Override with a comma-separated X_TRUSTED_HANDLES.
_DEFAULT_X_HANDLES = (
    "PFF_Fantasy,TheFFBallers,MichaelFFlorio,YahooFantasy,jpep20,jac3600,"
    "Pat_Thorman,daltondeldon,TimJablonski,SquareEdgeMike,rotoworld_fb,"
    "LordDontLose,CoopAFiasco,jbchoknows,AlfredoABrown,SuperrNova38,FantasyPros,"
    "NFL_Convo,DynastyDadFF,P2WFantasy,NFLFantasy,MaddJournalist,TheOGfantasy,"
    "TheNFElle,EricNMoody,PPRFantasyTips,TFFFDad,GuruFantasyWrld,mikealfred,"
    "TheFantasyFive,ChrisGimino,fflnfl,FFTylerO,FFDynastyGrill,FB_FilmAnalysis,"
    "MattHarmon_BYB,FantasySource_,MySportsUpdate,Ihartitz,DBro_FFB,DMendy02,"
    "HaydenWinks,FF_Wheeler,WazNFL,TheArmchairFF,justinboone,lukesawhook,"
    "SalVetriDFS,LobosFFDen,17gamepace,RyanJ_Heath"
)
X_TRUSTED_HANDLES = [
    h.strip().lstrip("@")
    for h in os.getenv("X_TRUSTED_HANDLES", _DEFAULT_X_HANDLES).split(",")
    if h.strip()
]

# --- xAI / Grok Agent Tools (x_search + web_search) -------------------------
XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_BASE_URL = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
# Two tiers: grok-4.3 for everyday buzz/questions (cheaper), grok-4.5 for
# high-stakes calls (trades, lineup optimization). grok-3 / Live Search retired.
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4.3")
GROK_MODEL_DEEP = os.getenv("GROK_MODEL_DEEP", "grok-4.5")
# How many days back the X/news search looks.
GROK_LOOKBACK_DAYS = int(os.getenv("GROK_LOOKBACK_DAYS", "3"))
# Per-request timeout (seconds). Agentic search on broad questions can run
# long, so give it room before giving up.
GROK_TIMEOUT = int(os.getenv("GROK_TIMEOUT", "210"))
# Sampling temperature. Lower = more consistent/focused answers run-to-run;
# higher = more varied. 0.3 is the sweet spot for grounded decision-making.
GROK_TEMPERATURE = float(os.getenv("GROK_TEMPERATURE", "0.3"))
# If true, hard-restrict the X search to your trusted handles (max 20). Off by
# default so breaking beat-reporter news isn't filtered out.
X_RESTRICT_TO_HANDLES = os.getenv("X_RESTRICT_TO_HANDLES", "").lower() in (
    "1", "true", "yes", "on"
)
ENABLE_GROK = bool(XAI_API_KEY)

# --- Scheduling -------------------------------------------------------------
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

# Weekday indexes: Monday=0 ... Sunday=6 (matches datetime.weekday()).
# Pre-waiver bid plan + FAAB recs (default Monday 7pm).
PRE_DIGEST_DAY = int(os.getenv("PRE_DIGEST_DAY", "0"))       # Monday
PRE_DIGEST_TIME = _parse_time(os.getenv("PRE_DIGEST_TIME", ""), "19:00")
# Post-waiver results + free-agent pickup guide (default Wednesday 6am).
POST_DIGEST_DAY = int(os.getenv("POST_DIGEST_DAY", "2"))     # Wednesday
POST_DIGEST_TIME = _parse_time(os.getenv("POST_DIGEST_TIME", ""), "06:00")
# Main Start/Sit digest (default Friday 6:45pm).
STARTSIT_DAY = int(os.getenv("STARTSIT_DAY", "4"))          # Friday
STARTSIT_TIME = _parse_time(os.getenv("STARTSIT_TIME", ""), "18:45")
# Sunday last-minute Start/Sit update (default Sunday 11:15am).
GAMEDAY_DAY = int(os.getenv("GAMEDAY_DAY", "6"))             # Sunday
GAMEDAY_TIME = _parse_time(os.getenv("GAMEDAY_TIME", ""), "11:15")

# Live free-agent watch: periodically scan Sleeper trending adds and alert on
# newly-hot players available in your league. Sleeper-only (no Grok cost) so it
# can run often. Quiet outside the hour window to avoid overnight pings.
FA_WATCH_ENABLED = os.getenv("FA_WATCH_ENABLED", "true").lower() in (
    "1", "true", "yes", "on"
)
FA_WATCH_HOURS = float(os.getenv("FA_WATCH_HOURS", "4"))
FA_WATCH_MIN_ADDS = int(os.getenv("FA_WATCH_MIN_ADDS", "4000"))
FA_WATCH_START_HOUR = int(os.getenv("FA_WATCH_START_HOUR", "8"))
FA_WATCH_END_HOUR = int(os.getenv("FA_WATCH_END_HOUR", "23"))

# How long (seconds) to reuse cached league data across commands.
CONTEXT_TTL = int(os.getenv("CONTEXT_TTL", "300"))


def missing_required() -> list[str]:
    """Return the names of required vars that aren't set, for a clear
    startup error instead of a confusing crash deep in the code."""
    required = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }
    # Need at least one way to identify the Sleeper account.
    if not (SLEEPER_USERNAME or SLEEPER_USER_ID or LEAGUE_ID):
        required["SLEEPER_USERNAME|SLEEPER_USER_ID|LEAGUE_ID"] = None
    return [name for name, val in required.items() if not val]
