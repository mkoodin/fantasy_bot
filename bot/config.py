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
SLEEPER_USER_ID = os.getenv("SLEEPER_USER_ID")
# If you're in multiple leagues, pin the one you want. Otherwise the bot
# uses the first league it finds for the season.
LEAGUE_ID = os.getenv("LEAGUE_ID")
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
# grok-4.5 is the current tool-capable model; grok-3 / Live Search are retired.
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4.5")
# How many days back the X/news search looks.
GROK_LOOKBACK_DAYS = int(os.getenv("GROK_LOOKBACK_DAYS", "3"))
# Per-request timeout (seconds). Agentic search on broad questions can run
# long, so give it room before giving up.
GROK_TIMEOUT = int(os.getenv("GROK_TIMEOUT", "210"))
# If true, hard-restrict the X search to your trusted handles (max 20). Off by
# default so breaking beat-reporter news isn't filtered out.
X_RESTRICT_TO_HANDLES = os.getenv("X_RESTRICT_TO_HANDLES", "").lower() in (
    "1", "true", "yes", "on"
)
ENABLE_GROK = bool(XAI_API_KEY)

# --- Scheduling -------------------------------------------------------------
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

# Weekday indexes: Monday=0 ... Sunday=6 (matches datetime.weekday()).
# Pre-waiver digest: night before waivers process (default Tuesday 8pm).
PRE_DIGEST_DAY = int(os.getenv("PRE_DIGEST_DAY", "1"))       # Tuesday
PRE_DIGEST_TIME = _parse_time(os.getenv("PRE_DIGEST_TIME", ""), "20:00")
# Post-waiver digest: after waivers clear (default Wednesday 9am).
POST_DIGEST_DAY = int(os.getenv("POST_DIGEST_DAY", "2"))     # Wednesday
POST_DIGEST_TIME = _parse_time(os.getenv("POST_DIGEST_TIME", ""), "09:00")
# Gameday injury/inactive sweep (default Sunday 11am).
GAMEDAY_DAY = int(os.getenv("GAMEDAY_DAY", "6"))             # Sunday
GAMEDAY_TIME = _parse_time(os.getenv("GAMEDAY_TIME", ""), "11:00")

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
