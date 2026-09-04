# 🏈 Sleeper Fantasy Football Assistant

A personalized Telegram bot that watches your [Sleeper](https://sleeper.com)
league for waiver opportunities and free agents, layers in **live X + news
buzz via xAI Grok**, and sends you pre/post-waiver digests with concrete FAAB
bids and drop suggestions — plus on-demand commands for any player.

## What it does

- **Loads your roster & league** from Sleeper's free public API (no key needed).
- **Pre-waiver digest** — positional needs, ranked waiver targets with FAAB bid
  ranges, and drop candidates to make room.
- **Post-waiver digest** — recap of the league's processed claims + what's still
  available to chase.
- **Live free-agent scanning** — Sleeper trending-adds tell you *who's* hot;
  Grok Live Search over **X and news** tells you *why* (injury/role/snap
  changes) and filters hype from credible beat-reporter signal.
- **Strategy-aware** — recommendations weight positional need, injuries, and
  roster depth, not just raw add velocity.
- **Interactive** — Telegram commands for roster, needs, trending, and any
  player on demand.

## Commands

Just **text the bot any question** — it answers with your full league loaded
(roster, all 12 rosters, everyone's FAAB, available free agents) and remembers
the last few turns, so follow-ups like *"in my league?"* work. Questions about
trades / lineup optimization auto-upgrade to the flagship model.

| Command | What it does |
|---|---|
| `/pre` | Pre-waiver digest (needs, FAAB bids, drops, live buzz) |
| `/post` | Post-waiver digest (league recap + free-agent guide) |
| `/waivers` | Hot free agents + suggested FAAB bids |
| `/drops` | Droppable players on your roster |
| `/roster` | Your team by position (injuries flagged) |
| `/needs` | Where your roster is thin |
| `/news` | Scan X + news now for anything actionable on your wire |
| `/trending` | Most-added players across Sleeper right now |
| `/player <name>` | Outlook + availability in your league + FAAB bid |
| `/startsit` | Optimal lineup + start/sit calls for the week |
| `/trade [focus]` | Find the best trade: fair deal, opening offer, how to pitch |
| `/tradecheck A for B` | Price a specific offer instantly — no model, just the math |
| `/deep <question>` | Force the flagship model for a big call |
| `/gameday` | Quick injury sweep of your starters |
| `/reset` | Clear conversation memory |

Scheduled automatically (all ET, configurable): **pre-waiver** Mon 7pm,
**post-waiver** Wed 6am, **start/sit** Fri 6:45pm, **last-minute start/sit**
Sun 11:15am, plus two watches on your wire:

- **Breaking-news watch** (every 3h, `NEWS_WATCH_*`) — reads X and the news
  directly for injuries, inactives, snap-count and depth-chart changes, and
  speaks *only* when the beneficiary is actually a free agent in your league:
  who got hurt, who inherits the work, whether he's a free add or a waiver
  claim, and how long you have. This is the leading signal — the point is to
  act before your leaguemates see it. It stays silent when there's nothing,
  and never repeats an alert it has already sent.
- **Free-agent watch** (every 4h, Sleeper-only so it's free) — fires once a
  player is already being added league-wide. A lagging confirmation, useful as
  a backstop but never first.

## Setup

1. **Create a Telegram bot** with [@BotFather](https://t.me/BotFather) → copy
   the token. Get your chat id from [@userinfobot](https://t.me/userinfobot).
2. **(Optional) xAI key** at <https://console.x.ai/> for live X/news buzz.
3. **Configure** — copy `.env.example` to `.env` (local) or set the same
   variables in **Railway → Variables**. You need `TELEGRAM_TOKEN`,
   `TELEGRAM_CHAT_ID`, and your `SLEEPER_USERNAME`.

### Run locally

```bash
pip install -r requirements.txt
python waiver_bot.py            # always-on bot
MODE=pre python waiver_bot.py   # send one digest and exit (smoke test)
```

### Deploy on Railway

`railway.json` is already set to run `python waiver_bot.py` as an always-on
service (`restartPolicy: ON_FAILURE`). Push the repo, set the environment
variables, and it stays live — polling Telegram for commands and firing the
scheduled digests. No Railway Cron needed; the scheduler is internal.

## How the numbers are decided

- **FAAB bids** are a percentage of your *remaining* budget, scaled by a
  player's add velocity (vs. the hottest available FA) and multiplied 1.5× if
  they fill a positional need. They're a starting point — nudge up for players
  you really want.
- **Needs** compare healthy players per position against your league's required
  starters (including flex demand on RB/WR/TE).
- **Drops** are scored from injury status, league-wide drop velocity, and how
  buried a player is on your depth chart.
- **Player value** is the number everything else hangs off, and it's computed,
  not remembered. Raw projected points can't be compared across positions — a
  QB outscores every running back and is still the cheapest starter to replace,
  because QB13 is nearly as good as QB5. So each player is priced by **value
  over replacement**: projected points in *your* scoring, minus the points of
  the freely-available player who'd take that slot. The replacement line is
  derived from your league's real settings — team count, starters per position,
  and how flex slots split across eligible positions — and lands where fantasy
  convention says it should (in a 12-team, 2RB/3WR/1TE/1FLEX league: RB35,
  WR47, TE15, QB14). The result is normalized to **0-100 and comparable across
  positions**: a QB and a RB with the same score are worth the same in a trade,
  and 0 means freely replaceable off waivers. Injuries discount the score;
  designations from Questionable to IR scale it down.
- **Start/sit** gets a computed **projection-optimal lineup** before the model
  sees the question: every slot filled with the highest-projected eligible
  player for that week, injury-discounted, plus the bench players within 3
  points of a starter flagged as close calls. Slot assignment is optimal, not
  just greedy — filling fixed slots before flex can't cost a better flex. The
  model's job is then to override the arithmetic with news, and say what made
  it deviate.
- **Waivers** rank by value, not by add volume. The wire board lists every
  unowned player with the **upgrade** each would give your starting lineup —
  the difference between his value and the player he'd displace — so a quiet
  free agent who'd start immediately outranks a hot name who'd sit. Add volume
  is kept as a separate signal, since a surge usually means news broke.
- **FAAB bids** are computed in dollars against your real budget, from three
  separate inputs. *Upgrade over the player he'd displace* sets what he's worth
  to you and caps the bid. *Rival demand* decides how much of that cap you
  actually have to spend — and it's measured properly, as the teams whose
  starting lineup this player would improve, not the teams who look thin. With
  nobody competing, the minimum wins. *Add volume* is only a tiebreak. A player
  who wouldn't crack your lineup is priced as a free add or $1 claim, never a
  budget item.
- **Every add names its drop.** A pickup is only worth making if it beats the
  player you'd cut for it, so each recommendation carries that comparison
  explicitly — and when the best free agent is worse than your own worst bench
  player, it says SKIP and tells you to stand pat rather than manufacturing a
  target.
- **League rules** constrain the advice. The trade deadline, IR slots and what's
  IR-eligible, how long dropped players sit on waivers, and the veto threshold
  all reach the model, so it won't propose a trade after the deadline or tell
  you to cut a player you could stash on IR for free. Drop rankings skip
  IR-eligible injuries outright while a slot is open.
- **Roster fit** decides who's actually available to trade. Every player on your
  team is tiered CORE / STARTER / DEPTH / EXPENDABLE by ranking him within his
  position against how many that slot really starts. Offers get built from
  DEPTH and EXPENDABLE; sending a CORE player requires showing the math. Tiers
  use *healthy* value on purpose — an injured starter is someone you stash, so
  he can never be demoted into a drop suggestion by his own injury.
- **The two-layer process** runs on every question, not just trades. The Sleeper
  layer is the baseline — value score, market rank, projections, and the round
  your league actually paid. The X layer adjusts it: current expert rankings
  plus recent discussion from the analysts in `X_TRUSTED_HANDLES` can move a
  player off his price for a role change, injury, or depth-chart shift, but the
  model has to name what moved him. Contradicting the value score is allowed —
  silently ignoring it isn't.

## Notes & limitations

- Projections come from a Sleeper endpoint that isn't part of the documented
  API, so they're treated as a bonus signal: if it stops answering, the bot
  falls back to market rank, draft capital, add/drop velocity, injuries and
  depth without breaking. `/diag` reports whether projections, market ranks and
  draft picks actually loaded. Everything carries a plain-English reason.
- If projections go missing, valuation falls back to a decay curve fitted to
  market rank. Tested both ways: the fallback reproduces the same replacement
  ranks and nearly identical scores, so ordering and trade verdicts hold — only
  the point totals become approximate. `/diag` says which mode you're in.
- Without `XAI_API_KEY`, the bot runs fully on Sleeper data; only the live
  X/news buzz (`/player` and the buzz block in digests) is disabled.
  `/tradecheck` needs no model at all — it's pure math.
- The bot only talks to the single `TELEGRAM_CHAT_ID` you configure.

## Project layout

```
waiver_bot.py        entry point (always-on, or MODE=… one-shot)
bot/
  config.py          env vars, schedule, timezone
  sleeper.py         Sleeper API client + player cache
  analysis.py        league context, needs, FAAB bids, drops
  valuation.py       value over replacement, roster tiers, trade math
  prompting.py       question classification + the analysis directives
  grok.py            xAI Grok Live Search over X + news
  digest.py          pre/post-waiver + gameday digest builders
  telegram_bot.py    command handlers + scheduled jobs
```
