"""
Pulls the season-long picture for FPL Jakarta from the official
(unauthenticated) FPL API and writes two files at the repo root:

  data.json    league standings for both leagues, plus manager of the month
               and manager of the week worked out separately for each league
               from that league's own members
  prices.json  price changes and transfer momentum for the price page

Every commit here triggers a rebuild of the site, so neither file is written
unless its contents actually changed. The timestamp is
ignored when comparing, otherwise the file would differ on every single run and
rebuild the site hourly to change one line nobody reads. Between gameweeks,
when nothing moves, this writes nothing and the site is not rebuilt at all.

Prices are refreshed at most once a day for the same reason: transfer counts
tick up continuously, so writing them every hour would cost a rebuild an hour
for figures that only matter once a day, when FPL applies price changes.

Run hourly by .github/workflows/refresh.yml. The live gameweek view is handled
separately by fetch_live_data.py. The cup is not published by FPL's API and is
maintained by hand, so it is not touched here.
"""

import json
import os
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

# How many past price changes to keep on the price page.
CHANGE_LOG_LIMIT = 120

# How many players to show in each transfer momentum column.
MOMENTUM_SIZE = 15

# Courtesy pause between manager requests so we are not hammering the API.
REQUEST_PAUSE = 0.25


def set_output(name, value):
    """Hand a value back to the workflow step that ran us."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    print(f"::publish::{name}={value}")


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


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def content_changed(path, payload):
    """True if this file would differ from what is on disk, timestamp aside."""
    old = load_json(path)
    if old is None:
        return True
    strip = lambda d: {k: v for k, v in d.items() if k != "generated_at"}
    return strip(old) != strip(payload)


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


def fetch_transfers(entry_id):
    """Every transfer this manager has made all season, newest first."""
    return fetch_json(f"{BASE}/entry/{entry_id}/transfers/")


# The two chips that make a transfer list meaningless: a wildcard or a free hit
# can move the whole squad, so the page names the chip instead of printing
# fifteen rows.
SQUAD_CHIPS = {"wildcard": "Wildcard", "freehit": "Free Hit"}

# What FPL calls the rest, for the badge beside a normal transfer list.
OTHER_CHIPS = {"bboost": "Bench Boost", "3xc": "Triple Captain",
               "manager": "Assistant Manager"}


def chip_by_event(history):
    """{gameweek: chip name as FPL spells it} for one manager."""
    out = {}
    for chip in (history or {}).get("chips") or []:
        event_id, name = chip.get("event"), chip.get("name")
        if event_id is not None and name:
            out[int(event_id)] = name
    return out


def team_values(rows, histories):
    """
    Each manager's team value, richest first.

    FPL's `value` is the whole £100.0m a manager started with, moved by price
    changes -- the squad *and* whatever is in the bank, not the squad alone.
    Checked against real data before this was written: two gameweeks in, `value`
    alone spanned 100.1 to 100.4 across the league, while value plus bank
    reached 105.7, which no amount of price movement could do in a fortnight.
    Adding the bank on top would count it twice, and this decides a prize.

    `change` is the move since the previous gameweek, so the page can show
    which way a squad is going rather than only where it stands.
    """
    out = []
    for row in rows:
        current = ((histories.get(row["entry_id"]) or {}).get("current")) or []
        if not current:
            continue
        latest = current[-1]
        previous = current[-2] if len(current) > 1 else None
        value = latest.get("value")
        if value is None:
            continue
        out.append({
            "entry_id": row["entry_id"],
            "manager": row["manager"],
            "team": row["team"],
            "gw": latest.get("event"),
            # Tenths of a million in the API; pounds here, as the game shows it.
            "value": round(value / 10, 1),
            "bank": round((latest.get("bank") or 0) / 10, 1),
            "change": (round((value - previous["value"]) / 10, 1)
                       if previous and previous.get("value") is not None else None),
        })
    # Richest first; ties settled by name so the order never jitters between
    # runs for two managers on the same value.
    out.sort(key=lambda m: (-m["value"], m["manager"].lower()))
    return out


def transfers_by_gameweek(rows, histories, transfers, started_gws):
    """
    {gameweek: [one entry per manager]}, for gameweeks that have started.

    Deliberately only started ones. FPL will happily tell you what a manager has
    already done for the gameweek *after* this one, and publishing that would
    turn this page into a way of watching rivals plan before the deadline. A
    transfer becomes public here at the same moment it becomes real.
    """
    by_entry = {}
    for entry_id, moves in (transfers or {}).items():
        for move in moves or []:
            event_id = move.get("event")
            if event_id is None:
                continue
            slot = by_entry.setdefault(entry_id, {}).setdefault(int(event_id),
                                                               {"in": [], "out": []})
            if move.get("element_in") is not None:
                slot["in"].append(move["element_in"])
            if move.get("element_out") is not None:
                slot["out"].append(move["element_out"])

    out = {}
    for gw in sorted(started_gws):
        week = []
        for row in rows:
            entry_id = row["entry_id"]
            moved = (by_entry.get(entry_id) or {}).get(gw) or {"in": [], "out": []}
            chip = chip_by_event(histories.get(entry_id)).get(gw)
            week.append({
                "entry_id": entry_id,
                "manager": row["manager"],
                "team": row["team"],
                "in": moved["in"],
                "out": moved["out"],
                "chip": chip,
            })
        out[str(gw)] = week
    return out


def event_started(event, now):
    """Whether a gameweek is under way, by its deadline having passed."""
    if not event:
        return False
    if event.get("finished") or event.get("is_current"):
        return True
    deadline = event.get("deadline_time")
    if not deadline:
        return False
    try:
        return now >= datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except ValueError:
        return False


def month_is_over(phase, next_phase, events_by_id, now):
    """
    Whether a month's award can be handed out yet.

    A month is not settled the moment one of its gameweeks has been played, and
    it is not settled by the calendar either: fixtures get moved, and a
    gameweek straddling the turn of the month belongs to whichever month FPL
    assigned it to. The one unambiguous signal is the next month starting --
    once the first gameweek of September is under way, August can no longer
    change, so August's trophy is safe to award.

    The final month of the season has no next month to wait for, so it waits
    for its own last gameweek to finish instead.
    """
    if next_phase:
        return event_started(events_by_id.get(next_phase.get("start_event")), now)
    last = events_by_id.get(phase.get("stop_event"))
    return bool(last and last.get("finished"))


def winners_for(entries, phases, histories, events_by_id=None, now=None):
    """
    Manager of the month and manager of the week among these entries only, so
    each league is judged against its own members rather than the whole group.
    Scores are net of transfer hits.

    A month with football still to come is reported with no winner: an award
    handed out halfway through the month would change hands as the month went
    on, which is worse than showing nothing.
    """
    events_by_id = events_by_id or {}
    now = now or datetime.now(timezone.utc)
    per_gw_best, per_phase_totals = {}, {}

    for entry in entries:
        hist = histories.get(entry["entry_id"])
        if not hist:
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

    lookup = {e["entry_id"]: e for e in entries}
    motm = []
    for i, phase in enumerate(phases):
        bucket = per_phase_totals.get(phase["name"], {})
        next_phase = phases[i + 1] if i + 1 < len(phases) else None
        settled = month_is_over(phase, next_phase, events_by_id, now)

        # `started` separates a month being played from one that has not begun,
        # so the page can say which it is instead of calling both unplayed.
        card = {
            "month": phase["name"],
            "started": bool(bucket),
            "settled": bool(settled),
            "manager": None,
            "team": None,
            "points": None,
        }
        if bucket and settled:
            winner_id = max(bucket, key=bucket.get)
            winner = lookup.get(winner_id, {})
            card.update({
                "manager": winner.get("manager"),
                "team": winner.get("team"),
                "points": bucket[winner_id],
            })
        motm.append(card)

    return motm, motw


def build_prices(bootstrap, previous, now):
    """
    The price page's data. Returns None when there is nothing new worth
    writing, so a quiet run does not cost a rebuild.

    FPL only tells us a player's current price and how far it has moved since
    the season started, never when it moved. So we keep our own log: every time
    a price differs from the one we recorded last, that change is dated and
    added. The log is what makes this a price *changes* page rather than a
    price list.
    """
    today = now.date().isoformat()
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    elements = bootstrap["elements"]

    seen_before = (previous or {}).get("prices") or {}

    observed = []
    for el in elements:
        was = seen_before.get(str(el["id"]))
        if was is None or was == el["now_cost"]:
            continue
        observed.append({
            "date": today,
            "name": el["web_name"],
            "team": teams.get(el["team"], ""),
            "pos": POSITIONS.get(el["element_type"], ""),
            "from": round(was / 10, 1),
            "to": round(el["now_cost"] / 10, 1),
        })

    # Refresh when a price actually moved, or once a day so the momentum
    # figures do not go stale. Otherwise leave the file alone.
    already_today = (previous or {}).get("snapshot_date") == today
    if previous and not observed and already_today:
        return None

    def describe(el, extra=None):
        row = {
            "name": el["web_name"],
            "team": teams.get(el["team"], ""),
            "pos": POSITIONS.get(el["element_type"], ""),
            "price": round(el["now_cost"] / 10, 1),
        }
        if extra:
            row.update(extra)
        return row

    moved = [e for e in elements if e["cost_change_start"]]
    risers = sorted(moved, key=lambda e: -e["cost_change_start"])
    fallers = sorted(moved, key=lambda e: e["cost_change_start"])

    def net(el):
        return el["transfers_in_event"] - el["transfers_out_event"]

    by_net = sorted(elements, key=net)

    log = observed + ((previous or {}).get("log") or [])

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "snapshot_date": today,
        "season_started": bool(moved),
        "risers": [
            describe(e, {"change": round(e["cost_change_start"] / 10, 1)})
            for e in risers if e["cost_change_start"] > 0
        ][:MOMENTUM_SIZE],
        "fallers": [
            describe(e, {"change": round(e["cost_change_start"] / 10, 1)})
            for e in fallers if e["cost_change_start"] < 0
        ][:MOMENTUM_SIZE],
        "momentum_in": [
            describe(e, {"net": net(e)}) for e in reversed(by_net[-MOMENTUM_SIZE:])
        ],
        "momentum_out": [
            describe(e, {"net": net(e)}) for e in by_net[:MOMENTUM_SIZE]
        ],
        "log": log[:CHANGE_LOG_LIMIT],
        # Baseline for spotting the next move. Not for display.
        "prices": {str(e["id"]): e["now_cost"] for e in elements},
    }


def main():
    now = datetime.now(timezone.utc)

    bootstrap = fetch_json(f"{BASE}/bootstrap-static/")
    events = bootstrap.get("events", [])
    phases = [p for p in bootstrap.get("phases", []) if p.get("name") != "Overall"]

    next_event = next((e for e in events if e.get("is_next")), None)
    current_event = next((e for e in events if e.get("is_current")), None)

    standings = {}
    for key, cfg in LEAGUES.items():
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
            for r in fetch_standings(cfg["id"])
        ]

    # One history per manager, shared between the leagues. Anyone in both is
    # only fetched once.
    everyone = {e["entry_id"] for rows in standings.values() for e in rows}
    histories = {}
    for entry_id in sorted(everyone):
        try:
            histories[entry_id] = fetch_history(entry_id)
        except Exception:  # noqa: BLE001 - one bad manager should not stop the run
            continue
        time.sleep(REQUEST_PAUSE)

    events_by_id = {e["id"]: e for e in events}
    motm, motw = {}, {}
    for key, rows in standings.items():
        motm[key], motw[key] = winners_for(rows, phases, histories, events_by_id, now)

    values = {key: team_values(rows, histories) for key, rows in standings.items()}

    # One transfer list per manager, covering the whole season, so this is a
    # single request each rather than one per gameweek.
    entry_transfers = {}
    for entry_id in sorted(everyone):
        try:
            entry_transfers[entry_id] = fetch_transfers(entry_id)
        except Exception:  # noqa: BLE001 - one bad manager should not stop the run
            continue
        time.sleep(REQUEST_PAUSE)

    started = [e["id"] for e in events if event_started(e, now)]
    elements_by_id = {e["id"]: e for e in bootstrap.get("elements", [])}
    published = {
        key: transfers_by_gameweek(rows, histories, entry_transfers, started)
        for key, rows in standings.items()
    }

    # Names are collected from what is actually published, not from every
    # transfer on file. Building the lookup from the whole feed would put next
    # week's targets in it, and anyone could read off a name that appears in the
    # lookup but in nobody's gameweek -- which is the thing the started-only
    # rule exists to prevent.
    named = sorted({pid
                    for league in published.values()
                    for week in league.values()
                    for row in week
                    for pid in row["in"] + row["out"]})

    transfers = {
        "generated_at": now.isoformat(timespec="seconds"),
        "current_gameweek": current_event["id"] if current_event else None,
        "leagues": {k: {"id": v["id"], "name": v["name"]} for k, v in LEAGUES.items()},
        # Names once, referenced by id everywhere else. Thirty-eight gameweeks
        # of two leagues repeating "Gianluigi Donnarumma" would be most of the
        # file.
        "players": {
            str(pid): {
                "name": elements_by_id[pid].get("web_name", "?"),
                "cost": round((elements_by_id[pid].get("now_cost") or 0) / 10, 1),
            }
            for pid in named if pid in elements_by_id
        },
        "gameweeks": published,
    }

    data = {
        "generated_at": now.isoformat(timespec="seconds"),
        "current_gameweek": current_event["id"] if current_event else None,
        "next_gameweek": next_event["id"] if next_event else None,
        "next_deadline": next_event["deadline_time"] if next_event else None,
        "leagues": {k: {"id": v["id"], "name": v["name"]} for k, v in LEAGUES.items()},
        "standings": standings,
        "motm": motm,
        "motw": motw,
        "values": values,
    }

    wrote = []

    if content_changed("data.json", data):
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        wrote.append("data.json")

    if content_changed("transfers.json", transfers):
        with open("transfers.json", "w", encoding="utf-8") as f:
            json.dump(transfers, f, separators=(",", ":"))
        wrote.append("transfers.json")

    prices = build_prices(bootstrap, load_json("prices.json"), now)
    if prices and content_changed("prices.json", prices):
        with open("prices.json", "w", encoding="utf-8") as f:
            json.dump(prices, f, separators=(",", ":"))
        wrote.append("prices.json")

    if wrote:
        print(f"Updated {', '.join(wrote)} - publishing.")
        set_output("publish", "true")
    else:
        print("Standings, winners and prices all unchanged - nothing to publish, "
              "leaving the files untouched so the site is not rebuilt.")
        set_output("publish", "false")


if __name__ == "__main__":
    main()
