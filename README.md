# FPL Jakarta dashboard

Live site: **https://fpljakarta.qd.je/**

A static site — no build step, no framework. Ten pages at the repo root
(`index.html`, `live.html`, `prizes.html`, `ownership.html`, `compare.html`,
`awards.html`, `prices.html`, `signup.html`, and the two partner venues
`taproom22.html` and `milospadel.html`), four data files (`data.json`,
`live.json`, `awards.json`, `prices.json`) written by the scripts in
`scripts/`, and a shared shell in `assets/` that the pages added after the
first three use instead of carrying a fifth copy of the same CSS.

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
- **a gameweek deadline falls inside the window** — this is the one that
  matters most, because a gameweek *starts* at its deadline. Miss a kick-off and
  the site shows the right gameweek with an old score. Miss a deadline and it
  shows the wrong gameweek, and nothing later in the week corrects it.

That last case is not hypothetical. GitHub delivered no scheduled run at all on
27 August 2026, and none between 01:31 and 19:21 on the 28th. GW2's deadline
passed at 17:30 and the site sat on GW1 until the workflow was started by hand.

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
untracked file holding `published`, `in_play`, `starts_soon` and
`deadline_soon`. A held runner
is free on a public repository, so the cost of this is a noisier commit history
on match days, which is the trade the fast cadence was always making.

Publishing takes `main` as it stands and puts the generated files on top, rather
than rebasing onto it. There is never anything to merge — each file is rewritten
whole, so the copy just written is by definition the newest — and rebasing one
full rewrite onto another conflicts every time. That is not hypothetical: a run
queued behind a long one started from a 39-minute-old checkout on 23 August and
died on exactly that conflict.

Only the files a pass actually rewrote are pushed. Each fetcher leaves a file
alone when it has nothing new, so `git diff` against the checkout is an exact
record of what this pass wrote. That matters now that one run writes all four:
a run that only refreshed the standings must not carry its hours-old `live.json`
along and roll live scores back to whatever they were when it was queued.

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
