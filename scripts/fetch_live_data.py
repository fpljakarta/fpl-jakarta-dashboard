"""
Builds the live gameweek picture for FPL Jakarta from the official
(unauthenticated) FPL API, and writes it to live.json at the repo root.

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
the world.

Run every 15 minutes by .github/workflows/refresh-live.yml.
Season-long standings, manager of the month and manager of the week are
handled separately by fetch_fpl_data.py and are not touched here.

Known limitations, both deliberate:
  - Automatic substitutions are not applied. FPL only makes subs once every
    match in the gameweek has finished, so any live sub shown anywhere is a
    prediction. Bench points are displayed but not counted.
  - Provisional bonus points are not included. Bonus lands after each match
    is marked finished, so scores firm up through the day.
"""

import json
import time
import urllib.request
from datetime import datetime, timezone

BASE = "https://fantasy.premierleague.com/api"

LEAGUES = {
    "high_stakes": {"id": 325153, "name": "High Stakes"},
    "main": {"id": 401272, "name": "Main league"},
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fpl-jakarta-dashboard/1.0)"}

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# Courtesy pause between manager requests so we are not hammering the API.
REQUEST_PAUSE = 0.25


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
        if fx.get("finished") or fx.get("finished_provisional"):
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

        players[pid] = {
            "name": el["web_name"],
            "team": teams.get(el["team"], ""),
            "pos": POSITIONS.get(el["element_type"], ""),
            "points": (stat.get("stats") or {}).get("total_points", 0),
            "minutes": (stat.get("stats") or {}).get("minutes", 0),
            "started": has_started,
            "finished": has_finished,
        }
    return players


def collect_managers(rows, gw, players, cache):
    """Fetch each manager's picks and work out their live gameweek score."""
    managers = []
    for row in rows:
        entry = row["entry"]

        if entry not in cache:
            try:
                cache[entry] = fetch_json(f"{BASE}/entry/{entry}/event/{gw}/picks/")
            except RuntimeError:
                # A manager who joined late may have no picks for this gameweek.
                cache[entry] = None
            time.sleep(REQUEST_PAUSE)

        picks_data = cache[entry]
        if not picks_data:
            continue

        hist = picks_data.get("entry_history") or {}
        chip = picks_data.get("active_chip")
        bench_boost = chip == "bboost"

        gw_points = 0
        yet_to_play = 0
        played = 0
        counted = 0
        captain_id = vice_id = None
        picks = []

        for p in picks_data.get("picks", []):
            pid = p["element"]
            info = players.get(pid, {})
            mult = p["multiplier"]
            on_pitch = p["position"] <= 11 or bench_boost

            if p.get("is_captain"):
                captain_id = pid
            if p.get("is_vice_captain"):
                vice_id = pid

            if on_pitch:
                counted += 1
                gw_points += info.get("points", 0) * mult
                if info.get("finished"):
                    played += 1
                else:
                    yet_to_play += 1

            picks.append({
                "id": pid,
                "slot": p["position"],
                "mult": mult,
                "captain": bool(p.get("is_captain")),
                "vice": bool(p.get("is_vice_captain")),
                "benched": not on_pitch,
            })

        hit = hist.get("event_transfers_cost", 0)

        managers.append({
            "entry": entry,
            "manager": row["player_name"],
            "team": row["entry_name"],
            "rank": row["rank"],
            "last_rank": row.get("last_rank", 0),
            "total": row["total"],
            "gw_points": gw_points - hit,
            "hit": hit,
            "transfers": hist.get("event_transfers", 0),
            "chip": chip,
            "captain": captain_id,
            "vice": vice_id,
            "yet_to_play": yet_to_play,
            "played": played,
            "of": counted,
            "value": round(hist.get("value", 0) / 10, 1),
            "bank": round(hist.get("bank", 0) / 10, 1),
            "picks": picks,
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


def main():
    bootstrap = fetch_json(f"{BASE}/bootstrap-static/")
    ev = current_gameweek(bootstrap)
    gw = ev["id"]

    live = fetch_json(f"{BASE}/event/{gw}/live/")
    fixtures = fetch_json(f"{BASE}/fixtures/?event={gw}")
    players = build_player_index(bootstrap, live, fixtures)

    kicked_off = sum(1 for f in fixtures if f.get("started"))
    done = sum(1 for f in fixtures if f.get("finished"))

    # Shared across leagues so anyone in both is only fetched once.
    picks_cache = {}
    out_leagues = {}

    for key, cfg in LEAGUES.items():
        rows, name = fetch_standings(cfg["id"])
        managers = collect_managers(rows, gw, players, picks_cache)
        managers.sort(key=lambda m: (-m["gw_points"], m["rank"]))
        for i, m in enumerate(managers, start=1):
            m["live_rank"] = i

        out_leagues[key] = {
            "id": cfg["id"],
            "name": cfg["name"],
            "full_name": name,
            "managers": managers,
            "eo": effective_ownership(managers),
        }

    # Only ship the footballers somebody in the league actually owns.
    used = set()
    for lg in out_leagues.values():
        for m in lg["managers"]:
            used.update(p["id"] for p in m["picks"])

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gameweek": gw,
        "gw_name": ev.get("name", f"Gameweek {gw}"),
        "finished": bool(ev.get("finished")),
        "data_checked": bool(ev.get("data_checked")),
        "fixtures": {"total": len(fixtures), "started": kicked_off, "finished": done},
        "players": {str(pid): players[pid] for pid in used if pid in players},
        "leagues": out_leagues,
    }

    with open("live.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    total = sum(len(lg["managers"]) for lg in out_leagues.values())
    print(f"GW{gw}: wrote live.json for {total} manager entries "
          f"({len(picks_cache)} unique), {done}/{len(fixtures)} fixtures finished.")


if __name__ == "__main__":
    main()
