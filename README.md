# FPL Jakarta dashboard

Live site: **https://fpljakarta.github.io/fpl-jakarta-dashboard/**

A static site — no build step, no framework. Four pages at the repo root
(`index.html`, `live.html`, `prices.html`, `signup.html`) and three data files
(`data.json`, `live.json`, `prices.json`) written by the scripts in `scripts/`.

## Where the data comes from

Both scripts read the official, unauthenticated Fantasy Premier League API at
`https://fantasy.premierleague.com/api`. Nothing is scraped from livefpl.net.

| Script | Writes | Run by | Cadence |
| --- | --- | --- | --- |
| `scripts/fetch_fpl_data.py` | `data.json`, `prices.json` | `.github/workflows/refresh.yml` | hourly |
| `scripts/fetch_live_data.py` | `live.json` | `.github/workflows/refresh-live.yml` | every 10 minutes |

Neither script writes a file unless its contents actually changed, so quiet
periods produce no commits at all.

## Hosting

GitHub Pages is the home. It serves the repo root from `main`, rebuilds on every
push, and is free and unmetered for a public repo. Because it is a *project*
site, everything lives under the `/fpl-jakarta-dashboard/` path — so all
internal links must be relative (`live.html`, not `/live`). A root-absolute link
resolves to `fpljakarta.github.io/` and 404s.

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
