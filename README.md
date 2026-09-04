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
| `/log` | Record a move you made, and what you expected |
| `/journal` | Review recent decisions |
| `/review` | Score past calls against what actually happened |
| `/usage` | Whose role grew or shrank, and where points lag the role |
| `/stash` | Who is one injury away from starter value |
| `/bench` | Why do I own each bench player |
| `/plan` | The next 2-4 weeks: byes, thin spots, what to buy early |
| `/news` | Scan X + news now for anything actionable on your wire |
| `/trending` | Most-added players across Sleeper right now |
| `/player <name>` | Outlook + availability in your league + FAAB bid |
| `/startsit` | Optimal lineup + start/sit calls for the week |
| `/trade [focus]` | Find the best trade: fair deal, opening offer, how to pitch |
| `/tradecheck A for B` | Price a specific offer instantly — no model, just the math |
| `/deep <question>` | Force the flagship model for a big call |
| `/gameday` | Quick injury sweep of your starters |
| `/reset` | Clear conversation memory |

Scheduled automatically (all ET, each individually configurable). The times are
chosen around when the decision is actually made, not around convenience:

| When | Brief | Why then |
|---|---|---|
| **Sun 9:30pm** | Get there first | Roles changed today; the top Tuesday claim is often still free tonight |
| **Mon 10am** | What changed | Usage postmortem before the articles set the market |
| **Tue 7pm** | Waiver claims & FAAB | After Monday night, before Wednesday 3am processing — the real deadline |
| **Wed 6am** | Post-waiver | Who went unclaimed, and what rivals dropped |
| **Thu 4pm** | Tonight's lock | TNF starters, and keeping FLEX free for Sunday |
| **Fri 6:45pm** | Start/sit | Practice reports in, lineup takes shape |
| **Sat 11am** | Bench audit | Every spot justified; free options on questionable starters |
| **Sun 11:15am** | Inactives | Final sweep before kickoff |

Plus two watches: a **breaking-news watch** every 3h that reads X and the news
directly and only speaks when the beneficiary is free in your league, and a
free-agent watch every 4h on add volume (Sleeper-only, so free).

> The old Monday 7pm pre-waiver digest is **off by default**: it fired before
> Monday Night Football, so it priced claims on an incomplete week. The Tuesday
> brief does that job at the point the decision is actually made. Set
> `PRE_DIGEST_ENABLED=true` to restore it.

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

## How it thinks

The model is told to reason in a fixed order on every question, because the
order is what separates a decision from a guess:

1. **Opportunity beats efficiency.** Snap share, routes, target share and
   especially red-zone and goal-line work are the foundation. Yards per carry
   and catch rate swing wildly and regress; volume persists.
2. **Usage leads production by one to two weeks.** When snaps or targets climb
   before the points arrive, that's the buying window — and it closes once the
   box score catches up and the rest of the league sees it. This is the edge the
   bot is built to hunt, and why the news watch reads reporting directly rather
   than waiting on add counts.
3. **Role is the question, not talent.** What job does he have, and did
   anything change it — a depth-chart move, an injury ahead of him, a committee
   resolving? A first-string listing the market hasn't repriced is worth more
   than a famous name in a timeshare.
4. **Game script shapes volume.** Favored teams run, trailing teams throw.
5. **Sample size and regression.** One big game is noise. Discount a hot week
   built on touchdowns that won't repeat; don't abandon an intact role after a
   quiet one.
6. **Availability is a prerequisite.** A player on bye or ruled out scores zero
   regardless of talent, and the context flags it.

## The weekly operating rhythm

Each day has a different job, and the bot is told which one it's doing. A
Monday answer should not read like a Sunday answer.

| Day | The job |
|---|---|
| **Mon** | Postmortem on *usage*, not points. Classify every notable game as role-, skill-, situation-, efficiency- or TD-driven. Build the waiver board before the articles do. |
| **Tue** | Injury forensics — map the whole tree downstream of each injury. Find the players one injury away from starter value who are still free. |
| **Wed** | Attack what waivers left behind: unclaimed targets, and what rivals *dropped* to make room. |
| **Thu** | Start/sit begins. Keep Thursday players out of FLEX — that slot is your Sunday insurance. |
| **Fri** | Practice-report trajectory (DNP→LP→FP is a different player from FP→LP→DNP), and the 2–4 week plan. |
| **Sat** | Bench audit — every spot needs a reason. Take the free options on questionable starters. |
| **Sun** | Information war: inactives, beat reporters, weather, O-line. Early games lock first, so preserve late flexibility. |

## The decision journal

Every meaningful call is written down with the information available when it
was made — the brief's recommendations and every `/tradecheck` automatically,
your own moves via `/log` — and scored later against what actually happened.

The point is to separate **decision quality from outcome quality**. Adding a
backup back before the starter got hurt was a good decision even if the starter
stayed healthy. Starting a receiver who caught a 75-yard touchdown after you
projected him two targets was a bad one that happened to pay. A system that
learns only from fantasy points becomes results-oriented, and results-oriented
gets steadily worse.

`/review` pulls decisions from completed weeks, attaches the real box score,
and shows expected against actual side by side rather than collapsing them.

Storage is a single JSON file written atomically, bounded at 500 entries, and
resilient to a corrupt or missing file. **Set `JOURNAL_PATH` to a mounted
Railway volume** — the default `/data/journal.json` assumes one. Anywhere
inside the app directory is wiped on redeploy, and `/diag` reports which of the
two you have rather than pretending the writes are durable.

## What it's optimizing for

Not "most projected points this week" — that's the right objective almost
never. The bot reads the standings and states the objective the season
situation actually calls for, and every answer is held to it:

| Situation | Objective |
|---|---|
| First few weeks | Grow roster value. Buy ascending roles; unspent FAAB in Week 17 was wasted. |
| Comfortably in | Weight the playoff weeks — schedules, insurance on the players the run depends on, ceiling over floor. |
| On the bubble | Qualifying comes first. Favor floor; you can't win a title you miss the playoffs for. |
| Chasing | Every week must-win. Take the higher ceiling on close calls. |
| Games back with games to play | Maximise variance. Playing for the median is pointless; spend what's left. |

Two related guards. **Signal conflicts** are surfaced rather than averaged —
when usage says starter and the market says depth, that disagreement is where
the opportunity is, and quietly splitting the difference hides it. **Data
confidence** reports which feeds failed to load, so a missing input is treated
as unknown rather than as zero.

## How the numbers are decided

- **FAAB bids** are a percentage of your *remaining* budget, scaled by a
  player's add velocity (vs. the hottest available FA) and multiplied 1.5× if
  they fill a positional need. They're a starting point — nudge up for players
  you really want.
- **Needs** compare healthy players per position against your league's required
  starters (including flex demand on RB/WR/TE).
- **Drops** are scored from injury status, league-wide drop velocity, and how
  buried a player is on your depth chart.
- **Depth chart and availability** come straight from Sleeper. A player listed
  first string who's still unowned is flagged as a market lag worth taking; a
  #2 is flagged as the next man up. Sleeper's feed carries no bye weeks, so
  they're inferred from the weekly projection feed: a player with a real season
  projection who's absent from this week's has no game, and is both barred from
  the lineup and protected from being dropped for it.
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
- **Below the waiver line, value stops being the argument.** VORP floors every
  replaceable player at zero, so "0.0 vs 0.0" answers nothing — and that's the
  most common waiver case there is. There the decision is made on fit: whether
  he's the handcuff to a back you depend on (or to someone else's stud),
  whether he's young with room for the role to grow, whether you carry any
  spare at that position, and whether the man he'd replace is doing anything at
  all. A flier with a real role case beats a hot name with none; if neither has
  one, it says so.
- **League rules** constrain the advice. The trade deadline, IR slots and what's
  IR-eligible, how long dropped players sit on waivers, and the veto threshold
  all reach the model, so it won't propose a trade after the deadline or tell
  you to cut a player you could stash on IR for free. Drop rankings skip
  IR-eligible injuries outright while a slot is open.
- **Usage, not points.** Sleeper's stats feed carries the participation data —
  offensive snaps, targets, carries, red-zone looks — so snap share, target
  share and carry share are computed for the last few weeks along with their
  *direction*. Two boards fall out: **usage rising** (a player whose snaps
  jumped while his points stayed quiet is the buy window, still cheap because
  the box score hasn't told the league yet) and **usage falling** on your own
  roster (sell or bench before the production follows the snaps down).
- **Every player carries three valuations**, because they answer different
  questions: what he's worth *this week*, his *rest-of-season* value, and his
  *ceiling* if the situation breaks his way. A backup back behind a workhorse
  might read `week 0.0 · ros 0.0 · ceiling 70.0`.
- **Bench spots are not small starting spots.** A starter is judged on expected
  points; a bench player is judged on the chance he becomes something. So bench
  ranking uses **contingent value** — what a player inherits if the man ahead of
  him disappears, weighted by how cleanly the job transfers (RB roles transfer;
  receiving roles get redistributed). The effect: a handcuff with ceiling 70
  outranks a veteran who outprojects him weekly but will never be started.
- **Byes are read three weeks forward.** Sleeper publishes no bye schedule, so
  they're inferred from future weekly projection feeds, and flagged when a week
  leaves you short at a position: *"Week 4 — WR: Nacua, Olave ⚠ SHORT AT TE"*.
  Cover it early while the wire is cheap.
- **Rival intel makes waivers game theory.** Each opponent's surplus, holes and
  remaining FAAB are computed, so a bid can be priced against who would actually
  start the player — and trade partners are picked by matching their surplus to
  your hole.
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
  journal.py         durable decision log, scored for process not outcome
  prompting.py       question classification + the analysis directives
  grok.py            xAI Grok Live Search over X + news
  digest.py          pre/post-waiver + gameday digest builders
  telegram_bot.py    command handlers + scheduled jobs
```
