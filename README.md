# FPL Jakarta dashboard

Live site: **https://fpljakarta.qd.je/**

A static site — no build step, no framework. Twelve pages at the repo root
(`index.html`, `live.html`, `prizes.html`, `transfers.html`, `teamvalue.html`,
`ownership.html`, `compare.html`, `awards.html`, `prices.html`, `signup.html`,
and the two partner venues `taproom22.html` and `milospadel.html`), five data
files (`data.json`, `live.json`, `awards.json`, `prices.json`,
`transfers.json`) written by the scripts in `scripts/`, and a shared shell in
`assets/` that the pages added after the first three use instead of carrying a
fifth copy of the same CSS.

## Transfers, and what is deliberately not on that page

`transfers.html` shows who each manager sold and who they brought in, one
gameweek at a time. A wildcard or a free hit is named instead, with a link to
the team on FPL: those chips can move the whole squad, so fifteen rows of swaps
would be noise where the chip is the story.

**A gameweek appears only once its deadline has passed.** FPL will happily tell
you what somebody has already done for the gameweek *after* this one, and
publishing that would turn the page into a way of watching rivals plan. The
same rule governs the player-name lookup in `transfers.json`, which is built
from what is published rather than from the whole transfer feed — otherwise a
name could be read off the lookup before it appeared in anyone's gameweek.

`in` and `out` are two lists rather than pairs, because FPL does not record
which sale paid for which purchase. The page lines them up by position, which
is the honest reading of a set of moves made together.

A player who went both ways in the same gameweek cancels out. FPL logs every
move as it is made rather than the net effect, so buying a player and selling
him again before the deadline appears twice in the feed and not at all in the
squad. On a wildcard this is the difference between a readable list and a
transcript of somebody trying shapes: one GW2 rebuild came through as 37 in and
37 out, of which 26 cancelled.

## Team value, and what it means

`teamvalue.html` ranks every squad, richest first, from the `values` block in
`data.json`.

FPL's `value` is **the squad and the bank together** — the whole £100.0m a
manager started with, moved by price changes. This was checked against real
league data rather than assumed, because a prize is settled on it: two
gameweeks in, `value` alone spanned 100.1 to 100.4 across the league, while
value plus bank reached 105.7, which no amount of price movement could do in a
fortnight. The bank is shown in its own column because it is part of that
total, not an addition to it, and adding it would count it twice.

Both pages need one extra request per manager per refresh — `entry/{id}/
transfers/`, which returns the whole season, so it is one call each rather than
one per gameweek. Team value needs none at all: it comes out of the history
already fetched for the monthly and weekly winners.

## The prizes page

`prizes.html` lists what the league pays out, per league, and holds the figures
in a `PRIZES` constant in the page itself. There is no data file behind it: the
amounts are set by the organisers and change by commit, not by a fetch.

Everything derived is derived from that one constant — the pot totals, the bar
widths in the placings table, and the worked tie examples. Nothing on the page
restates a number that is also written somewhere else, so the arithmetic cannot
drift from the table above it.

The two rules the page has to state, because money depends on them: every prize
is paid at the end of the season, whenever it was won; and tied placings pool
the prizes for the places they occupy and split them equally, while a tied
one-off prize is split between everyone level.

## The partner venue pages

`taproom22.html` and `milospadel.html` are the two Jakarta venues where league
members get discounts and benefits during Premier League games. They have no
data file behind them and never change on their own.

Their address, hours and contact details came from public directory listings,
because the venues' own pages and Instagram accounts could not be read directly.
Listings go stale, so each page points at the venue's own map listing, Instagram
and WhatsApp as the authority, and says so where a detail is genuinely uncertain
rather than inventing a precise one — two listings disagreed on Taproom 22's
opening hours, which is why that page gives them as an afternoon-until-late range
and sends you to the map listing before an early arrival. Anything confirmed with
the venues should be corrected here in the same commit.

## Where the data comes from

Both scripts read the official, unauthenticated Fantasy Premier League API at
`https://fantasy.premierleague.com/api`. Nothing is scraped from livefpl.net.

| Script | Writes | Run by | Cadence |
| --- | --- | --- | --- |
| `scripts/fetch_fpl_data.py` | `data.json`, `prices.json` | `refresh.yml` hourly, and `refresh-live.yml` on every run | hourly, plus every half hour inside a live run |
| `scripts/fetch_live_data.py` | `live.json`, `awards.json` | `.github/workflows/refresh-live.yml` | every 5 minutes during a match |

Both fetchers run inside `refresh-live.yml`, so **whichever workflow GitHub
happens to deliver brings the whole site current**. That was not true until the
GW2 rollover went wrong: `data.json` carries the gameweek number, the standings,
the deadline and the monthly and weekly winners, and it used to be refreshed
only by the hourly job — so a delivered run of the live workflow left the home
page sitting on the old gameweek.

Neither script writes a file unless its contents actually changed, so quiet
periods produce no commits at all. The live script only publishes at moments
worth publishing: shortly before a kick-off, at half time, at full time, and
every five minutes while a match is actually being played. On a day with no
football it writes nothing.

### What a run stays awake for

A run keeps publishing while any of three things is true, and exits in seconds
when none of them is:

- **a match is being played** — provisional bonus moves continuously;
- **a kick-off falls inside the window** — a run that wakes before the whistle
  and exits leaves the half that follows to whenever the next run arrives;
- **a gameweek deadline is close** — either coming up inside the window, or
  gone within the last 45 minutes. This is the one that matters most, because a
  gameweek *starts* at its deadline. Miss a kick-off and the site shows the
  right gameweek with an old score. Miss a deadline and it shows the wrong
  gameweek, and nothing later in the week corrects it.

That last case is not hypothetical, and it has now failed twice for different
reasons.

On 27 August 2026 GitHub delivered no scheduled run at all, and none between
01:31 and 19:21 on the 28th, so GW2 began with the site still on GW1. That is
what the window is for.

On 4 September a run *was* there. Run #538 started at 13:30, held for four
hours waiting for the 17:30 deadline exactly as intended — and broke out of the
loop at **17:30:16**, sixteen seconds after it, with `deadline_soon=false`. FPL
does not move `is_current` at the stroke of the deadline; it takes a few
minutes. The run had waited all afternoon for the rollover and then left the
room just before it happened.

Hence the grace period. The interesting moment is not the deadline itself but
the minutes just after it, so a deadline keeps a run publishing for 45 minutes
past. And when the live fetcher does see the gameweek move, the standings fetch
runs on that same pass rather than waiting up to half an hour for its own
cadence — which is why the live script now runs first in each pass and reports
`gw_changed`.

### Why the workflow loops instead of trusting the cron

The cron asks for a run every five minutes. GitHub does not oblige. Measured
across a match day, this repository gets a scheduled run roughly **once an
hour** whatever the cron says — the same was true of the `*/10` that preceded
it. The `schedule` event is best-effort and high-frequency crons are dropped
under load.

Once an hour is useless for provisional bonus, which moves continuously and was
the entire reason for a fast cadence: the projected scores would sit still for
most of a half.

So the cron is treated as a wake-up call rather than the cadence. When a run
starts, it publishes and then asks the script whether there is more football
coming. While a match is being played — **or is about to kick off** — the job
stays alive and publishes again every five minutes, for up to two hours, before
letting the next scheduled run take over. On a day with no football the first
pass finds nothing and the job exits in seconds.

Waiting for a kick-off matters as much as staying through a match, and that
half was missing at first. On 23 August the scheduled runs landed at 09:56,
10:55, 11:46 — and then not again until 13:41. Two matches kicked off at 13:00.
The 11:46 run correctly found nothing in play and exited in nine seconds, so
nothing published the first half of either: the site sat on data ten hours old
while the football was on. A run that waits for an imminent kick-off is the run
that covers the match.

The window has been widened twice for the same reason, and both times by
measurement rather than by guess. Fifty-five minutes assumed runs arrive about
hourly; the 115-minute gap above is what happens when they do not, and that took
it to 120. Then came 27 August, when nothing was delivered all day, and 120 was
not remotely enough — so it is now **300 minutes**, five hours, against a
six-hour cap on how long GitHub will run a job at all.

A wide window does not make the cron reliable. What it does is make each
delivered run worth much more: a single run landing any time in the five hours
before a deadline now carries the site through the rollover. Nothing in this
repository can promise a run will land at all — the only trigger that would is
an external one, calling the `workflow_dispatch` API on a schedule GitHub does
not control.

The script tells the workflow what happened through `.live-run-status`, an
untracked file holding `published`, `in_play`, `starts_soon`, `deadline_soon`
and `gw_changed`. A held runner
is free on a public repository, so the cost of this is a noisier commit history
on match days, which is the trade the fast cadence was always making.

Publishing takes `main` as it stands and puts the generated files on top, rather
than rebasing onto it. There is never anything to merge — each file is rewritten
whole, so the copy just written is by definition the newest — and rebasing one
full rewrite onto another conflicts every time. That is not hypothetical: a run
queued behind a long one started from a 39-minute-old checkout on 23 August and
died on exactly that conflict.

Only the files a pass actually rewrote are pushed. Each fetcher leaves a file
alone when it has nothing new, so the working tree against the checkout is an
exact record of what this pass wrote. That record comes from `git status`, not
`git diff`: diff says nothing about a file git has never seen, so the first run
after a new data file is added would quietly never commit it. `transfers.json`
is how that was found out. That matters now that one run writes all four:
a run that only refreshed the standings must not carry its hours-old `live.json`
along and roll live scores back to whatever they were when it was queued.

## The external trigger

GitHub's `schedule` is best-effort and this repository has been badly served by
it — nothing delivered at all on 27 August 2026, and a 34-hour gap straddling
GW2's deadline. Everything above makes a *delivered* run worth more; none of it
makes a run arrive. The only thing that does is a trigger from outside GitHub.

A scheduled ping calls this endpoint, which starts `refresh-live.yml`, which
refreshes all four data files:

    POST https://api.github.com/repos/fpljakarta/fpl-jakarta-dashboard/actions/workflows/refresh-live.yml/dispatches

    Accept: application/vnd.github+json
    Authorization: Bearer <token>
    X-GitHub-Api-Version: 2022-11-28
    Content-Type: application/json

    {"ref": "main"}

A success is **HTTP 204** with an empty body. Anything else means the token is
wrong, expired, or lacks the permission below.

`scripts/ping-refresh.sh` makes exactly that request, so a token can be proved
before it is pasted into anything:

    GITHUB_TOKEN=github_pat_... ./scripts/ping-refresh.sh

It prints `queued` on success and, on failure, what GitHub said and which of
the four usual causes it is.

Hourly is plenty: a run that finds nothing on exits in seconds, and one that
finds a deadline or a match stays up for it. Pings that land while a run is
already going are absorbed by the `refresh-live-data` concurrency group.

### The token

A fine-grained personal access token, scoped as narrowly as it goes:

- **Repository access** — only `fpljakarta/fpl-jakarta-dashboard`
- **Permissions** — Actions: *Read and write*. Nothing else.
- **Expiry** — GitHub caps fine-grained tokens at a year. Whatever is chosen,
  the trigger stops dead the day it lapses, so it is worth a calendar reminder.

Made at <https://github.com/settings/personal-access-tokens>. It goes into the
cron service and nowhere else — not into this repository, not into a commit,
and not into a chat window.

### Why a dispatch does not force a publish

`workflow_dispatch` used to force one, on the reasoning that a person only runs
it by hand when they want a refresh now. With something pinging it every hour
that would put a commit an hour into the history saying nothing had changed. So
forcing is now an explicit `force` input, off by default: the hourly ping
behaves exactly like a scheduled run, and the tick-box is still there for a
person who wants to force one from the Actions tab.

### If a third-party service is unwanted

The same token can make the workflow keep itself alive instead: a run that
dispatches its successor before its window closes never depends on the cron
again. It works — a dispatch authenticated with a PAT does start a new run,
where one authenticated with `GITHUB_TOKEN` deliberately does not — but it means
a runner is held essentially all the time, which is free on a public repository
and noisy in the Actions tab. The external ping is the tidier of the two.

## The fixture list

`live.json` carries the current gameweek's matches — score, scorers, the 3/2/1
bonus, and how many footballers somebody in FPL Jakarta has in their eleven for
that match. The home page reads it from there rather than from `data.json`,
because `data.json` is hourly and a score an hour old is not a live score.

The ownership count is stored per league, so it follows the High Stakes / Main
league switch like everything else on the page. Because the fixtures sit above
that switch, the section heading names the league the counts belong to.

The bonus shown on a fixture is deliberately computed differently from the
bonus added to a manager's projected score. The scoring path must skip any
fixture whose bonus FPL has already published, or it would count it twice; the
fixture list has no such worry and always shows the 3/2/1, published or not.

## Two scores, and why

Every manager carries an **official** score and a **projected** one.

The official score is what FPL has confirmed. The projected score adds three
things the rules make inevitable but FPL has not applied yet:

- **provisional bonus**, worked out from each fixture's BPS table (3/2/1, with
  FPL's tie rules), and only for fixtures whose bonus has not been published —
  once it has, it is already inside the official total and adding ours would
  count it twice;
- **automatic substitutions**, following the real rules: bench order, a keeper
  only ever replaced by the other keeper, and a legal formation at the end of
  it;
- **the armband moving to the vice-captain** when the captain's match finishes
  without him appearing.

They are kept as two numbers rather than blended into one, because the first is
a fact and the second is a forecast. FPL only makes substitutions once every
match in the gameweek has finished, so until then nothing — here or anywhere —
can do better than predict them.

## The live overall rank is an estimate

FPL publishes no live overall rank; league and overall ranks only move once a
gameweek is finalised. The figure marked `~` on the live page is therefore
computed, not reported:

1. sample managers from across the global Overall league (id 314) at
   geometrically spaced depths, from the champion down to the tail;
2. score each sampled squad exactly the way our own are scored, so both sides
   of the comparison are made of the same thing;
3. pair the sampled scores against the sampled ranks to get a score-to-rank
   curve, and read our own totals off it, interpolating on log rank because
   rank thins out geometrically rather than evenly.

It is labelled as an estimate everywhere it appears, and it is absent rather
than wrong if the sampling fails. It costs roughly a hundred requests, so the
curve is reused for fifteen minutes before being rebuilt. The confirmed
overall rank sits beside it, refreshed once a gameweek.

## Tests

The arithmetic that is easy to get subtly wrong — bonus ties, substitution
legality, the rank curve, award selection — lives in `scripts/live_calc.py`
as pure functions, apart from the fetching, and is tested without a network:

    python -m unittest discover -s scripts -p 'test_*.py'

The live workflow runs them whenever that code changes, but not on the
five-minute schedule.

## Hosting

GitHub Pages is the home. It serves the repo root from `main`, rebuilds on every
push, and is free and unmetered for a public repo.

The site answers on **https://fpljakarta.qd.je/**. The root `CNAME` file is what
tells Pages so; Pages issues the certificate itself, and redirects the old
project URLs under `fpljakarta.github.io/fpl-jakarta-dashboard/` to the matching
page on the domain, so links already shared in the group keep working. Because
the site now serves from a domain root rather than a project path, internal
links are no longer *required* to be relative — but they are relative
throughout, and leaving them that way keeps the pages working when opened from
a checkout or from the Netlify mirror.

The domain is a free registration from DigitalPlat FreeDomain
(<https://dash.domain.digitalplat.org>). It costs nothing, but unlike a paid
domain it has to be renewed periodically, and an unrenewed one stops resolving.
If that ever happens — or if the domain needs to go for any other reason — the
recovery is a one-file change: **delete `CNAME`, and the site is immediately
back at `https://fpljakarta.github.io/fpl-jakarta-dashboard/`.** Nothing else in
the repo depends on the domain except the `<link rel="canonical">` tag at the
top of each page.

The Netlify mirror at https://fpljkt.netlify.app is optional, and automatic
builds are switched off there. Netlify's free tier bills build minutes, and a
commit every ten minutes during a gameweek exhausts a month of them, after
which the deploy freezes. That no longer matters, because each page reads its
data file from the deployed copy *and* from `raw.githubusercontent.com` and
uses whichever `generated_at` is later — so a deploy that never rebuilds still
shows current numbers, and a slow or unreachable GitHub (5s timeout) falls back
to the deployed copy.

The mirror therefore only needs a new deploy when the HTML itself changes, and
that deploy should be a file upload rather than a build, which costs no build
minutes:

    npx netlify-cli login
    npx netlify-cli deploy --prod --dir=. --site fpljkt

`netlify.toml` is there for the case where automatic builds are ever switched
back on: it skips the build for commits that only touch the data files.
