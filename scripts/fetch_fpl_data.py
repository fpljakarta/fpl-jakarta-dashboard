"""
Pulls live data for FPL Jakarta from the official (unauthenticated) FPL API:
  - league standings (both leagues)
  - manager of the month (from FPL's own monthly phases)
  - manager of the week, all played gameweeks (net of transfer hits)

Writes the result to data.json at the repo root. Run hourly by
.github/workflows/refresh.yml. The cup is not published by FPL's API and is
maintained by hand, so it is not touched here.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://fantasy.premierleague.com/api"

LEAGUES = {
    "high_stakes": {"id": 325153, "name": "High Stakes"},
    "main": {"id": 401272, "name": "Main league"},
}

# Manager of the month / week are computed from this league's members, since
# everyone in the group is in the main league. Switch to "high_stakes" if that
# ever changes.
MOTM_MOTW_SOURCE = "main"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fpl-jakarta-dashboard/1.0)"}


def fetch_json(url, retries=3, delay=3):
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - keep retrying regardless of cause
            last_err = e
            time.sleep(delay)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def fetch_standings(league_id):
    entries, page = [], 1
    while True:
        url = f"{BASE}/leagues-classic/{league_id}/standings/?page_standings={page}"
        data = fetch_json(url)
        block = data.get("standings", {})
        entries.extend(block.get("results", []))
        if not block.get("has_next") or page > 20:
            break
        page += 1
    return entries


def fetch_history(entry_id):
    return fetch_json(f"{BASE}/entry/{entry_id}/history/")


def main():
    bootstrap = fetch_json(f"{BASE}/bootstrap-static/")
    events = bootstrap.get("events", [])
    phases = [p for p in bootstrap.get("phases", []) if p.get("name") != "Overall"]

    next_event = next((e for e in events if e.get("is_next")), None)
    current_event = next((e for e in events if e.get("is_current")), None)

    standings = {}
    for key, cfg in LEAGUES.items():
        results = fetch_standings(cfg["id"])
        standings[key] = [
            {
                "rank": r["rank"],
                "last_rank": r["last_rank"],
                "manager": r["player_name"],
                "team": r["entry_name"],
                "entry_id": r["entry"],
                "total": r["total"],
                "event_total": r["event_total"],
            }
            for r in results
        ]

    source_entries = standings.get(MOTM_MOTW_SOURCE, [])
    per_gw_best = {}
    per_phase_totals = {}

    for entry in source_entries:
        try:
            hist = fetch_history(entry["entry_id"])
        except Exception:
            continue

        for gw in hist.get("current", []):
            event = gw["event"]
            net = gw["points"] - gw.get("event_transfers_cost", 0)

            best = per_gw_best.get(event)
            if best is None or net > best["points"]:
                per_gw_best[event] = {
                    "gw": event,
                    "manager": entry["manager"],
                    "team": entry["team"],
                    "points": net,
                }

            for phase in phases:
                if phase["start_event"] <= event <= phase["stop_event"]:
                    bucket = per_phase_totals.setdefault(phase["name"], {})
                    bucket[entry["entry_id"]] = bucket.get(entry["entry_id"], 0) + net

    motw = [per_gw_best[gw] for gw in sorted(per_gw_best)]

    entry_lookup = {e["entry_id"]: e for e in source_entries}
    motm = []
    for phase in phases:
        bucket = per_phase_totals.get(phase["name"], {})
        if not bucket:
            motm.append({"month": phase["name"], "manager": None, "team": None, "points": None})
            continue
        winner_id = max(bucket, key=bucket.get)
        winner = entry_lookup.get(winner_id, {})
        motm.append(
            {
                "month": phase["name"],
                "manager": winner.get("manager"),
                "team": winner.get("team"),
                "points": bucket[winner_id],
            }
        )

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_gameweek": current_event["id"] if current_event else None,
        "next_gameweek": next_event["id"] if next_event else None,
        "next_deadline": next_event["deadline_time"] if next_event else None,
        "leagues": {k: {"id": v["id"], "name": v["name"]} for k, v in LEAGUES.items()},
        "motm_motw_source_league": MOTM_MOTW_SOURCE,
        "standings": standings,
        "motm": motm,
        "motw": motw,
    }

    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
