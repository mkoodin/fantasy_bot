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

| Command | What it does |
|---|---|
| `/pre` | Pre-waiver digest (needs, FAAB bids, drops, live buzz) |
| `/post` | Post-waiver digest (league recap + next targets) |
| `/waivers` | Hot free agents + suggested FAAB bids |
| `/drops` | Droppable players on your roster |
| `/roster` | Your team by position (injuries flagged) |
| `/needs` | Where your roster is thin |
| `/trending` | Most-added players across Sleeper right now |
| `/player <name>` | Live X + news buzz on any player (Grok) |
| `/gameday` | Injury sweep of your starters |

Scheduled automatically: **pre-waiver** (Tue 8pm ET), **post-waiver**
(Wed 9am ET), **gameday injury sweep** (Sun 11am ET). All configurable.

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

## Notes & limitations

- Sleeper doesn't expose projections or bye weeks in the players feed, so the
  strategy layer uses market velocity + injuries + depth rather than projected
  points. Everything carries a plain-English reason.
- Without `XAI_API_KEY`, the bot runs fully on Sleeper data; only the live
  X/news buzz (`/player` and the buzz block in digests) is disabled.
- The bot only talks to the single `TELEGRAM_CHAT_ID` you configure.

## Project layout

```
waiver_bot.py        entry point (always-on, or MODE=… one-shot)
bot/
  config.py          env vars, schedule, timezone
  sleeper.py         Sleeper API client + player cache
  analysis.py        league context, needs, FAAB bids, drops
  grok.py            xAI Grok Live Search over X + news
  digest.py          pre/post-waiver + gameday digest builders
  telegram_bot.py    command handlers + scheduled jobs
```
