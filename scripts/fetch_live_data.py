"""
Builds the live gameweek picture for FPL Jakarta from the official
(unauthenticated) FPL API, and writes it to live.json and awards.json at the
repo root.

For every manager in both leagues this collects:
  - their starting XI and bench for the current gameweek
  - live points per player, updated as matches are played
  - captain / vice / active chip
  - how many of their players have finished playing
  - squad value and money in the bank

It also computes LEAGUE effective ownership: how heavily each footballer is
owned inside FPL Jakarta, counting captaincy twice. This is deliberately
different from the global ownership figure livefpl shows - inside a mini
league, what matters is who is differential against your rivals, not against
the world. Both numbers are published, so the pages can show them side by
side.

Two scores are published for every manager. The OFFICIAL one is what FPL has
confirmed. The PROJECTED one adds the bonus points the BPS table implies, the
automatic substitutions the rules imply, and the vice-captain taking over from
a captain who did not play. The first is a fact and the second is a forecast,
so they are kept as separate numbers rather than blended into one. The
arithmetic behind them lives in live_calc.py and is tested by
test_live_calc.py.

Also published: an estimate of each manager's live overall rank. FPL does not
expose one - league and overall ranks only move once a gameweek is finalised -
so it is worked out by sampling managers from across the global game, scoring
their squads the same way, and reading our own totals off the resulting curve.
It is an estimate, is labelled as one everywhere it appears, and is absent
rather than wrong if the sampling fails. Nothing is taken from livefpl.

Run by .github/workflows/refresh-live.yml. The workflow polls often, but this
script only asks it to publish when there is something worth publishing:
shortly before a fixture kicks off, at half time, at full time, and every few
minutes while a match is actually being played. On a day with no football this
writes nothing at all.

Season-long standings, manager of the month and manager of the week are
handled separately by fetch_fpl_data.py and are not touched here.

Known limitation, deliberate: automatic substitutions shown during a gameweek
are predictions. FPL only makes them once every match has finished, so no
source can do better than predict until then.
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from live_calc import (
    captain_counts,
    estimate_rank,
    ownership_counts,
    is_over,
    match_in_progress,
    provisional_bonus,
    sample_pages,
    score_squad,
    weekly_awards,
)
from live_calc import build_rank_curve as make_rank_curve

BASE = "https://fantasy.premierleague.com/api"

LEAGUES = {
    "high_stakes": {"id": 325153, "name": "High Stakes"},
    "main": {"id": 401272, "name": "Main league"},
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fpl-jakarta-dashboard/1.0)"}

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# Courtesy pause between manager requests so we are not hammering the API.
REQUEST_PAUSE = 0.25

# How close to kick-off the pre-match publish fires, in minutes. Scheduled
# GitHub Actions runs are often several minutes late, so this is deliberately
# wider than the polling interval.
PRE_MATCH_WINDOW = 20

# FPL holds a fixture's `minutes` at 45 for the duration of the interval, so
# any poll during the break sees this. If a run is missed the half-time publish
# fires on the next poll instead, a little into the second half.
HALF_TIME_MINUTE = 45

# While a match is actually being played, republish on this cadence even
# without a milestone. Provisional bonus moves continuously as BPS changes, so
# without this the projected scores would sit still for a whole half.
LIVE_REFRESH_MINUTES = 5

# The overall-rank curve costs about a hundred requests to rebuild, so it is
# reused between publishes for this long. Scores drift slowly enough across
# eleven million managers that a curve a few minutes old is still a good read.
RANK_CURVE_MAX_AGE_MINUTES = 15

# FPL's global "Overall" league. Sampling it at these depths gives a spread of
# scores from the very top of the game down to the tail, which is what the
# score-to-rank curve is interpolated from. Ranks past the field size are
# dropped rather than fetched.
SAMPLE_RANKS = [
    1, 3, 10, 30, 100, 300, 1_000, 3_000, 10_000, 30_000, 100_000,
    300_000, 700_000, 1_500_000, 3_000_000, 5_000_000, 7_000_000, 9_000_000,
]
OVERALL_LEAGUE = 314
STANDINGS_PAGE_SIZE = 50
SAMPLES_PER_PAGE = 5


# Read by the workflow after every pass so it knows whether to commit and
# whether to keep going. Not committed; see .gitignore.
STATUS_FILE = ".live-run-status"


def set_output(name, value):
    """Hand a value back to the workflow step that ran us."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    print(f"::publish::{name}={value}")


def write_status(published, in_play):
    """
    Tell the workflow what just happened and whether football is still on.

    The workflow loops on this: commit when something was published, and go
    round again while a match is still being played.
    """
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({"published": bool(published), "in_play": bool(in_play)}, f)
    set_output("publish", "true" if published else "false")
    set_output("in_play", "true" if in_play else "false")


def load_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def due_milestones(fixtures, already, now):
    """
    Publish-worthy moments that have arrived and have not been published yet.
    Each fixture can contribute three: pre-match, half time and full time.
    """
    due = []
    for fx in fixtures:
        fid = str(fx["id"])
        done = set(already.get(fid, []))
        started = bool(fx.get("started"))
        over = is_over(fx)
        minutes = fx.get("minutes") or 0
        kickoff = fx.get("kickoff_time")

        # Once a match is under way the pre-match moment has passed for good,
        # so it can never fire retrospectively.
        if "pre" not in done and not started and kickoff:
            ko = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            lead = (ko - now).total_seconds() / 60
            if 0 <= lead <= PRE_MATCH_WINDOW:
                due.append((fid, "pre"))

        if "ht" not in done and started and not over and minutes >= HALF_TIME_MINUTE:
            due.append((fid, "ht"))

        if "ft" not in done and over:
            due.append((fid, "ft"))
    return due


def minutes_since(stamp, now):
    """Minutes between an ISO timestamp and now, or None if unreadable."""
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 60


def fetch_json(url, retries=3, delay=3):
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - retry regardless of cause
            last_err = e
            time.sleep(delay)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def try_fetch(url):
    """Fetch that returns None instead of raising, for optional extras."""
    try:
        return fetch_json(url, retries=2, delay=2)
    except RuntimeError as e:
        print(f"  skipped: {e}")
        return None


def current_gameweek(bootstrap):
    for ev in bootstrap["events"]:
        if ev.get("is_current"):
            return ev
    # Before the season opens nothing is current yet; fall back to the next one.
    for ev in bootstrap["events"]:
        if ev.get("is_next"):
            return ev
    return bootstrap["events"][0]


def fetch_standings(league_id):
    """Walk every page of a classic league's standings."""
    rows, page = [], 1
    while True:
        data = fetch_json(
            f"{BASE}/leagues-classic/{league_id}/standings/?page_standings={page}"
        )
        standings = data["standings"]
        rows.extend(standings["results"])
        if not standings.get("has_next"):
            return rows, data["league"]["name"]
        page += 1
        time.sleep(REQUEST_PAUSE)


def build_player_index(bootstrap, live, fixtures):
    """One lookup table of every footballer, with their live gameweek return."""
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}

    # Which fixtures have kicked off, and which have finished.
    started, finished = set(), set()
    for fx in fixtures:
        if fx.get("started"):
            started.add(fx["id"])
        if is_over(fx):
            finished.add(fx["id"])

    live_by_id = {el["id"]: el for el in live["elements"]}

    players = {}
    for el in bootstrap["elements"]:
        pid = el["id"]
        stat = live_by_id.get(pid, {})
        explain = stat.get("explain") or []
        fixture_ids = [e.get("fixture") for e in explain if e.get("fixture")]

        has_started = any(f in started for f in fixture_ids)
        has_finished = bool(fixture_ids) and all(f in finished for f in fixture_ids)

        try:
            owned = float(el.get("selected_by_percent") or 0)
        except ValueError:
            owned = 0.0

        players[pid] = {
            "name": el["web_name"],
            "team": teams.get(el["team"], ""),
            "pos": POSITIONS.get(el["element_type"], ""),
            "points": (stat.get("stats") or {}).get("total_points", 0),
            "minutes": (stat.get("stats") or {}).get("minutes", 0),
            "started": has_started,
            "finished": has_finished,
            # Global ownership, so the ownership page can set what FPL Jakarta
            # does against what the rest of the world does.
            "owned": round(owned, 1),
            "cost": round((el.get("now_cost") or 0) / 10, 1),
        }
    return players


def score_entry(picks_data, players, bonus):
    """Both scores, the squad, and everything the pages need, for one entry."""
    hist = picks_data.get("entry_history") or {}
    chip = picks_data.get("active_chip")
    bench_boost = chip == "bboost"

    picks = []
    for p in picks_data.get("picks", []):
        picks.append({
            "id": p["element"],
            "slot": p["position"],
            "mult": p["multiplier"],
            "captain": bool(p.get("is_captain")),
            "vice": bool(p.get("is_vice_captain")),
            "benched": not (p["position"] <= 11 or bench_boost),
        })

    scored = score_squad(picks, players, bonus=bonus,
                         bench_boost=bench_boost, chip=chip)

    counted = [p for p in picks if not p["benched"]]
    played = sum(1 for p in counted if (players.get(p["id"]) or {}).get("finished"))

    return {
        "picks": picks,
        "scored": scored,
        "chip": chip,
        "hit": hist.get("event_transfers_cost", 0),
        "transfers": hist.get("event_transfers", 0),
        "value": round(hist.get("value", 0) / 10, 1),
        "bank": round(hist.get("bank", 0) / 10, 1),
        "played": played,
        "yet_to_play": len(counted) - played,
        "of": len(counted),
    }


def picks_for(entry, gw, cache):
    """Fetch and cache one entry's picks, or None if they have none."""
    if entry not in cache:
        try:
            cache[entry] = fetch_json(f"{BASE}/entry/{entry}/event/{gw}/picks/")
        except RuntimeError:
            # A manager who joined late may have no picks for this gameweek.
            cache[entry] = None
        time.sleep(REQUEST_PAUSE)
    return cache[entry]


def collect_managers(rows, gw, players, bonus, cache):
    """Fetch each manager's picks and work out their live gameweek score."""
    managers = []
    for row in rows:
        entry = row["entry"]
        picks_data = picks_for(entry, gw, cache)
        if not picks_data:
            continue

        s = score_entry(picks_data, players, bonus)
        scored = s["scored"]
        hit = s["hit"]

        # `total` in the standings tracks live points but `rank` does not, so
        # subtracting this gameweek's score is the reliable way back to where
        # the manager stood before it started.
        pre_gw = (row["total"] or 0) - (row.get("event_total") or 0)

        managers.append({
            "entry": entry,
            "manager": row["player_name"],
            "team": row["entry_name"],
            "rank": row["rank"],
            "last_rank": row.get("last_rank", 0),
            "pre_gw_total": pre_gw,
            "total": pre_gw + scored["official"] - hit,
            "total_projected": pre_gw + scored["projected"] - hit,
            "gw_points": scored["official"] - hit,
            "gw_projected": scored["projected"] - hit,
            "pending_bonus": scored["pending_bonus"],
            "sub_gain": scored["sub_gain"],
            "subbed_in": scored["subbed_in"],
            "subbed_out": scored["subbed_out"],
            "hit": hit,
            "transfers": s["transfers"],
            "chip": s["chip"],
            "captain": scored["captain"],
            "captain_changed": scored["captain_changed"],
            "vice": next((p["id"] for p in s["picks"] if p["vice"]), None),
            "bench_points": scored["bench_points"],
            "yet_to_play": s["yet_to_play"],
            "played": s["played"],
            "of": s["of"],
            "value": s["value"],
            "bank": s["bank"],
            "picks": s["picks"],
        })

    return managers


def effective_ownership(managers):
    """
    Share of the league holding each player, counting a captaincy twice.
    A player owned by everyone and captained by nobody sits at 100%.
    """
    if not managers:
        return {}
    totals = {}
    for m in managers:
        for p in m["picks"]:
            if p["benched"]:
                continue
            totals[p["id"]] = totals.get(p["id"], 0) + p["mult"]
    n = len(managers)
    return {pid: round(v * 100 / n, 1) for pid, v in totals.items()}


# --------------------------------------------------------------------------
# live overall rank
# --------------------------------------------------------------------------

def choose_sample_entries(total_players):
    """
    Pick managers spread across the whole global field.

    One request to the Overall league returns fifty managers at a known depth,
    so a handful of requests at geometrically spaced depths covers the game
    from the champion down to the tail. `sample_pages` works out which pages
    those depths actually mean, without asking for the same one twice.

    A page that will not load, or that comes back empty because the field ends
    sooner than the field size implied, is simply skipped: a thinner curve is
    still a usable curve.
    """
    sample = []
    seen = set()
    empty = 0
    for page in sample_pages(SAMPLE_RANKS, total_players, STANDINGS_PAGE_SIZE):
        data = try_fetch(
            f"{BASE}/leagues-classic/{OVERALL_LEAGUE}/standings/"
            f"?page_standings={page}"
        )
        time.sleep(REQUEST_PAUSE)
        results = ((data or {}).get("standings") or {}).get("results") or []
        if not results:
            # The deepest pages are where standings run out. Once a couple in
            # a row come back empty there is nothing further down to find, so
            # stop paying for the requests.
            empty += 1
            if empty >= 2:
                break
            continue
        empty = 0

        # Take the top of each page. Spreading the picks across it was tried
        # and measurably worsened the estimate, so this stays as it was until
        # there is evidence for changing it.
        for row in results[:SAMPLES_PER_PAGE]:
            entry = row.get("entry")
            if not entry or entry in seen:
                continue
            seen.add(entry)
            sample.append({
                "entry": entry,
                "rank": row.get("rank") or (page * STANDINGS_PAGE_SIZE),
                "pre_gw_total": (row.get("total") or 0) - (row.get("event_total") or 0),
            })
    return sample


def build_rank_curve(sample, gw, players, bonus, cache):
    """
    Score every sampled manager the same way we score our own, then turn the
    result into a score-to-rank curve.

    Sampled managers are scored with the same projection our own leagues get,
    so the two sides of the comparison are made of the same thing. A manager
    whose picks will not load is left out.
    """
    points = []
    for s in sample:
        picks_data = picks_for(s["entry"], gw, cache)
        if not picks_data:
            continue
        scored = score_entry(picks_data, players, bonus)
        total = (s["pre_gw_total"] + scored["scored"]["projected"] - scored["hit"])
        points.append((total, s["rank"]))
    return make_rank_curve(points)


def overall_ranks(entries, previous, gw, data_checked):
    """
    Each manager's confirmed overall rank, refreshed once a gameweek.

    FPL only moves overall rank when a gameweek is finalised, so this is worth
    one request per manager per gameweek and no more. The cached copy travels
    inside live.json.
    """
    cached = previous or {}
    fresh = (cached.get("gw") == gw and cached.get("checked") == data_checked)
    if fresh and cached.get("ranks"):
        return cached

    ranks = dict(cached.get("ranks") or {})
    for entry in entries:
        data = try_fetch(f"{BASE}/entry/{entry}/")
        time.sleep(REQUEST_PAUSE)
        if data and data.get("summary_overall_rank"):
            ranks[str(entry)] = data["summary_overall_rank"]
    return {"gw": gw, "checked": data_checked, "ranks": ranks}


# --------------------------------------------------------------------------
# awards
# --------------------------------------------------------------------------

def store_awards(path, gw, gw_name, final, leagues, players, now):
    """
    Fold this gameweek's awards into the archive, leaving past weeks alone.

    The current week is recomputed on every publish because it is still
    moving. Weeks already written keep whatever they finished with.
    """
    archive = load_json_file(path) or {}
    weeks = archive.get("gameweeks") or {}

    weeks[str(gw)] = {
        "gw": gw,
        "name": gw_name,
        "final": final,
        "leagues": {
            key: {
                "name": lg["name"],
                "awards": weekly_awards(
                    lg["managers"], players, ownership=ownership_counts(lg["managers"])
                ),
            }
            for key, lg in leagues.items()
        },
    }

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "current_gw": gw,
        "gameweeks": weeks,
    }


def main():
    now = datetime.now(timezone.utc)
    forced = os.environ.get("FORCE_PUBLISH", "").lower() in ("1", "true", "yes")

    # Deciding whether to publish costs two requests. Only if the answer is yes
    # do we go on to fetch every manager's picks, which costs far more.
    bootstrap = fetch_json(f"{BASE}/bootstrap-static/")
    ev = current_gameweek(bootstrap)
    gw = ev["id"]
    fixtures = fetch_json(f"{BASE}/fixtures/?event={gw}")

    previous = load_json_file("live.json")
    same_gw = previous is not None and previous.get("gameweek") == gw
    already = dict((previous.get("published") or {}) if same_gw else {})

    reasons = []
    if previous is None:
        reasons.append("no live.json yet")
    elif not same_gw:
        reasons.append(f"gameweek changed {previous.get('gameweek')} -> {gw}")
    if forced:
        reasons.append("manual run or code change")

    due = due_milestones(fixtures, already, now)
    reasons += [f"fixture {fid} {kind}" for fid, kind in due]

    # Bonus points move continuously while a match is on, so a live match is
    # reason enough on its own once the last publish has aged out.
    in_play = match_in_progress(fixtures)
    if in_play and same_gw:
        age = minutes_since((previous or {}).get("generated_at"), now)
        if age is None or age >= LIVE_REFRESH_MINUTES:
            reasons.append(f"match in play, last publish {age and round(age)}m ago")

    if not reasons:
        upcoming = [f for f in fixtures if not f.get("started")]
        print(f"GW{gw}: nothing to publish - "
              f"{sum(1 for f in fixtures if is_over(f))}"
              f"/{len(fixtures)} fixtures done, {len(upcoming)} still to come. "
              f"Skipping the manager fetch and leaving live.json untouched.")
        write_status(False, in_play)
        return

    print(f"GW{gw}: publishing because {'; '.join(reasons)}.")

    live = fetch_json(f"{BASE}/event/{gw}/live/")
    players = build_player_index(bootstrap, live, fixtures)
    bonus = provisional_bonus(fixtures, live["elements"])
    if bonus:
        print(f"GW{gw}: {len(bonus)} players carrying provisional bonus.")

    kicked_off = sum(1 for f in fixtures if f.get("started"))
    done = sum(1 for f in fixtures if is_over(f))

    # Shared across leagues so anyone in both is only fetched once.
    picks_cache = {}
    out_leagues = {}

    for key, cfg in LEAGUES.items():
        rows, name = fetch_standings(cfg["id"])
        managers = collect_managers(rows, gw, players, bonus, picks_cache)

        # Two orderings: what the table shows now, and what it would show if
        # every pending bonus and substitution landed as predicted.
        by_official = sorted(managers, key=lambda m: (-m["gw_points"], m["rank"]))
        for i, m in enumerate(by_official, start=1):
            m["live_rank"] = i
        by_projected = sorted(managers, key=lambda m: (-m["gw_projected"], m["rank"]))
        for i, m in enumerate(by_projected, start=1):
            m["projected_rank"] = i

        for m in managers:
            baseline = m["last_rank"] or m["rank"]
            m["rank_change"] = baseline - m["live_rank"]

        managers = by_official
        out_leagues[key] = {
            "id": cfg["id"],
            "name": cfg["name"],
            "full_name": name,
            "managers": managers,
            "eo": effective_ownership(managers),
            "owners": ownership_counts(managers),
            "captains": captain_counts(managers),
        }

    # The overall-rank curve is the expensive part, so it is rebuilt only when
    # the cached one has aged out. Everything about it degrades to absent
    # rather than wrong.
    cached_curve = (previous or {}).get("or_curve") if same_gw else None
    curve_age = minutes_since((cached_curve or {}).get("built_at"), now)
    total_players = bootstrap.get("total_players") or 0

    if cached_curve and curve_age is not None and curve_age < RANK_CURVE_MAX_AGE_MINUTES:
        curve = [tuple(p) for p in cached_curve.get("points") or []]
        curve_meta = cached_curve
        print(f"GW{gw}: reusing the rank curve, {round(curve_age)}m old.")
    else:
        sample = choose_sample_entries(total_players)
        curve = build_rank_curve(sample, gw, players, bonus, picks_cache)
        # A flat run -- several scores sharing one rank -- distorts every
        # estimate that interpolates across it, so it is worth seeing in the
        # log rather than discovering from a bad number on the page.
        ranks = [r for _, r in curve]
        flat = len(ranks) - len(set(ranks))
        curve_meta = {
            "built_at": now.isoformat(timespec="seconds"),
            "points": [list(p) for p in curve],
            "samples": len(curve),
            "sampled_entries": len(sample),
            "flat_runs": flat,
            "total_players": total_players,
        }
        print(f"GW{gw}: rank curve from {len(sample)} sampled managers -> "
              f"{len(curve)} points, {flat} sharing a rank.")

    official_or = overall_ranks(
        sorted({m["entry"] for lg in out_leagues.values() for m in lg["managers"]}),
        (previous or {}).get("official_or") if same_gw else None,
        gw, bool(ev.get("data_checked")),
    )

    for lg in out_leagues.values():
        for m in lg["managers"]:
            m["or_official"] = official_or["ranks"].get(str(m["entry"]))
            m["or_live"] = estimate_rank(curve, m["total_projected"], total_players)

    # Only ship the footballers somebody in the league actually owns.
    used = set()
    for lg in out_leagues.values():
        for m in lg["managers"]:
            used.update(p["id"] for p in m["picks"])

    for fid, kind in due:
        already.setdefault(fid, [])
        if kind not in already[fid]:
            already[fid].append(kind)

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "gameweek": gw,
        "gw_name": ev.get("name", f"Gameweek {gw}"),
        "finished": bool(ev.get("finished")),
        "data_checked": bool(ev.get("data_checked")),
        "fixtures": {"total": len(fixtures), "started": kicked_off, "finished": done},
        "average": ev.get("average_entry_score"),
        "highest": ev.get("highest_score"),
        "total_players": total_players,
        "players": {str(pid): players[pid] for pid in used if pid in players},
        "bonus": {str(pid): v for pid, v in bonus.items() if pid in used},
        "leagues": out_leagues,
        "or_curve": curve_meta,
        "official_or": official_or,
        # What we have already published this gameweek, so the next run knows
        # which moments are spent. This travels with the committed file.
        "published": already,
    }

    with open("live.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    awards = store_awards(
        "awards.json", gw, payload["gw_name"], payload["data_checked"],
        out_leagues, players, now,
    )
    with open("awards.json", "w", encoding="utf-8") as f:
        json.dump(awards, f, separators=(",", ":"))

    total = sum(len(lg["managers"]) for lg in out_leagues.values())
    print(f"GW{gw}: wrote live.json for {total} manager entries "
          f"({len(picks_cache)} unique), {done}/{len(fixtures)} fixtures finished.")
    write_status(True, in_play)


if __name__ == "__main__":
    main()
