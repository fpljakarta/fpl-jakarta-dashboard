"""
The arithmetic behind the live pages, kept apart from the fetching.

Everything here is a pure function over data already in hand: no network, no
files, no clock. That is deliberate. The official FPL API is only reachable
from the GitHub Actions runner, so this is the part that can be tested
anywhere, and `scripts/test_live_calc.py` does exactly that.

Three jobs live here:

  * provisional bonus, worked out from a fixture's BPS table
  * predicted automatic substitutions, following FPL's own rules
  * the weekly awards, and the score -> overall rank curve

`fetch_live_data.py` fetches, calls into this, and writes the JSON.
"""

# FPL's valid formations all sit inside these bounds. A side is legal when it
# has exactly one keeper and stays within every outfield range: 3-4-3, 4-4-2,
# 5-2-3 and the rest are all covered by the same three limits.
FORMATION = {
    "GKP": (1, 1),
    "DEF": (3, 5),
    "MID": (2, 5),
    "FWD": (1, 3),
}


# --------------------------------------------------------------------------
# provisional bonus
# --------------------------------------------------------------------------

def bonus_from_bps(bps_rows):
    """
    Map {player id: bonus} from one fixture's BPS table.

    The top three BPS scores in a match earn 3, 2 and 1 points, and ties are
    settled by sharing the higher award and skipping the ones it consumes.
    Two players tied at the top both take 3 and the next player takes 1; three
    tied at the top all take 3 and nothing else is awarded. Grouping by score
    and looking at how many players are already ahead expresses all of those
    cases at once, without enumerating them.

    `bps_rows` is a sequence of {"element": id, "value": bps} as the API
    returns it, home and away combined.
    """
    by_score = {}
    for row in bps_rows:
        try:
            pid = int(row["element"])
            score = int(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
        by_score.setdefault(score, []).append(pid)

    awards = {}
    ahead = 0
    for score in sorted(by_score, reverse=True):
        if ahead >= 3:
            break
        group = by_score[score]
        # 0 players ahead means this group is first and takes 3, one ahead
        # takes 2, two ahead take 1.
        awards.update({pid: 3 - ahead for pid in group})
        ahead += len(group)
    return awards


def is_over(fixture):
    """
    Whether a fixture is done being played.

    FPL marks a match `finished_provisional` as soon as it ends, and only sets
    `finished` once the data has been checked, which can be hours later. For
    everything here -- substitutions, players yet to play, how many matches are
    done -- provisional is over. Keeping that judgment in one place is
    deliberate: when the fixture counter alone disagreed, the page spent an
    evening announcing "0 of 10 fixtures finished" with six of them long since
    over.
    """
    return bool(fixture.get("finished") or fixture.get("finished_provisional"))


def match_in_progress(fixtures):
    """
    Whether any match is being played right now.

    This decides how long the workflow keeps publishing for. GitHub delivers a
    high-frequency schedule far less often than it is asked to -- roughly once
    an hour here, whatever the cron says -- so a single run stays alive through
    a match and republishes on its own rather than trusting the next trigger to
    arrive on time.
    """
    return any(fx.get("started") and not is_over(fx) for fx in fixtures)


def fixtures_with_official_bonus(live_elements):
    """
    Fixture ids whose bonus FPL has already added to the live totals.

    Bonus lands some minutes after a match is marked finished. Until then our
    own BPS reading is the only figure available; afterwards the official one
    is already inside `total_points`, and adding ours on top would count it
    twice. A single player showing bonus for a fixture settles it for that
    whole fixture.
    """
    settled = set()
    for el in live_elements:
        for entry in el.get("explain") or []:
            fid = entry.get("fixture")
            if fid is None:
                continue
            for stat in entry.get("stats") or []:
                if stat.get("identifier") == "bonus" and (stat.get("points") or 0) > 0:
                    settled.add(fid)
    return settled


def provisional_bonus(fixtures, live_elements):
    """
    {player id: bonus not yet in their live total}, across the gameweek.

    Only fixtures that have kicked off and have not had their bonus published
    contribute. A player in two fixtures in one gameweek accumulates both.
    """
    settled = fixtures_with_official_bonus(live_elements)
    pending = {}
    for fx in fixtures:
        if not fx.get("started") or fx["id"] in settled:
            continue
        rows = []
        for stat in fx.get("stats") or []:
            if stat.get("identifier") == "bps":
                rows.extend(stat.get("h") or [])
                rows.extend(stat.get("a") or [])
        for pid, bonus in bonus_from_bps(rows).items():
            pending[pid] = pending.get(pid, 0) + bonus
    return pending


# --------------------------------------------------------------------------
# automatic substitutions
# --------------------------------------------------------------------------

def _formation_ok(counts):
    return all(lo <= counts.get(pos, 0) <= hi for pos, (lo, hi) in FORMATION.items())


def predict_autosubs(picks, players, bench_boost=False):
    """
    Which bench players FPL will bring on, as [(off id, on id), ...].

    FPL only runs substitutions once every match in the gameweek is over, so
    anything shown before then is a prediction — this one included. It follows
    the real rules: a starter is replaced only when his match is over and he
    did not appear, replacements are tried in bench order, a keeper can only be
    replaced by the other keeper, and the resulting eleven has to be a legal
    formation. A bench player who has not played cannot come on.

    Bench Boost plays all fifteen, so nothing is substituted.
    """
    if bench_boost:
        return []

    def pos_of(pid):
        return (players.get(pid) or {}).get("pos", "")

    def blanked(pid):
        info = players.get(pid) or {}
        return bool(info.get("finished")) and not info.get("minutes")

    def available(pid):
        return bool((players.get(pid) or {}).get("minutes"))

    starters = sorted((p for p in picks if p["slot"] <= 11), key=lambda p: p["slot"])
    bench = sorted((p for p in picks if p["slot"] > 11), key=lambda p: p["slot"])

    xi = [p["id"] for p in starters]
    counts = {}
    for pid in xi:
        counts[pos_of(pid)] = counts.get(pos_of(pid), 0) + 1

    remaining = [p["id"] for p in bench]
    swaps = []

    for pid in list(xi):
        if not blanked(pid):
            continue
        out_pos = pos_of(pid)

        for sub in list(remaining):
            if not available(sub):
                continue
            in_pos = pos_of(sub)
            # A keeper is only ever swapped for the other keeper, in either
            # direction: an outfielder cannot fill the shirt.
            if (out_pos == "GKP") != (in_pos == "GKP"):
                continue

            trial = dict(counts)
            trial[out_pos] = trial.get(out_pos, 0) - 1
            trial[in_pos] = trial.get(in_pos, 0) + 1
            if not _formation_ok(trial):
                continue

            counts = trial
            xi[xi.index(pid)] = sub
            remaining.remove(sub)
            swaps.append((pid, sub))
            break

    return swaps


# --------------------------------------------------------------------------
# scoring a squad
# --------------------------------------------------------------------------

def armband(picks, players, chip=None):
    """
    Who actually wears the armband, and what it is worth.

    If the captain's match finishes without him appearing, FPL hands the
    armband to the vice-captain. That is a rule rather than a prediction, so
    the projection applies it as soon as the captain's fixture is over.
    """
    worth = 3 if chip == "3xc" else 2
    captain = next((p["id"] for p in picks if p.get("captain")), None)
    vice = next((p["id"] for p in picks if p.get("vice")), None)

    info = players.get(captain) or {}
    if captain and vice and info.get("finished") and not info.get("minutes"):
        return vice, worth, True
    return captain, worth, False


def score_squad(picks, players, bonus=None, bench_boost=False, chip=None,
                apply_subs=True):
    """
    Work out a manager's gameweek score two ways.

    `official` counts only what FPL has already published: the eleven as
    picked, no pending bonus, no substitutions. `projected` adds the bonus the
    BPS table implies and the substitutions and armband change the rules
    imply, which is the number that moves during a match. The two are reported
    side by side rather than blended, because one is a fact and the other is a
    forecast.

    Hits are not deducted here; the caller holds them.
    """
    bonus = bonus or {}

    def base(pid):
        return (players.get(pid) or {}).get("points", 0)

    counted = [p for p in picks if p["slot"] <= 11 or bench_boost]
    official = sum(base(p["id"]) * (p.get("mult") or 0) for p in counted)

    swaps = predict_autosubs(picks, players, bench_boost) if apply_subs else []
    swapped_in = {on for _, on in swaps}
    swapped_out = {off for off, _ in swaps}

    on_pitch = [p["id"] for p in counted if p["id"] not in swapped_out]
    on_pitch += [on for _, on in swaps]

    leader, worth, changed = armband(picks, players, chip)

    def multiplier(pid):
        return worth if pid == leader else 1

    projected_base = sum(base(pid) * multiplier(pid) for pid in on_pitch)
    projected_bonus = sum(bonus.get(pid, 0) * multiplier(pid) for pid in on_pitch)

    return {
        "official": official,
        "projected": projected_base + projected_bonus,
        # Split out so the page can say where the difference came from rather
        # than just showing a bigger number.
        "pending_bonus": projected_bonus,
        "sub_gain": projected_base - official,
        "swaps": swaps,
        "captain": leader,
        "captain_changed": changed,
        "bench_points": sum(base(p["id"]) for p in picks if p["slot"] > 11),
        "subbed_in": sorted(swapped_in),
        "subbed_out": sorted(swapped_out),
    }


# --------------------------------------------------------------------------
# overall rank estimate
# --------------------------------------------------------------------------

def sample_pages(ranks, total_players, page_size=50):
    """
    The standings pages to fetch, given the overall ranks worth sampling.

    A page holds fifty managers, so several ranks near the top land on the same
    one: 1, 3, 10 and 30 are all page 1. Asking for it four times costs four
    requests and returns the same fifty people, which is how the first run of
    this came back with a curve barely half as thick as intended. Deduplicating
    here spends those requests further down the field instead.

    Pages past the end of the field are dropped -- they return nothing.
    """
    last = (total_players + page_size - 1) // page_size if total_players else None
    pages = set()
    for rank in ranks:
        if rank < 1:
            continue
        page = (rank + page_size - 1) // page_size
        if last and page > last:
            continue
        pages.add(page)
    return sorted(pages)


def build_rank_curve(samples):
    """
    A score -> overall rank curve, from managers sampled across the game.

    Each sample is (live total, the overall rank that manager held going into
    the gameweek). Sorting the totals highest first and the ranks lowest first
    and pairing them off assumes only that the sample keeps roughly the same
    order it started in, which over millions of entries and one gameweek is
    close enough to true to be useful.

    Returned as [(total, rank), ...] ascending by total, ready to interpolate.
    """
    pairs = [(t, r) for t, r in samples
             if isinstance(t, (int, float)) and isinstance(r, (int, float)) and r >= 1]
    if len(pairs) < 2:
        return []

    totals = sorted((p[0] for p in pairs), reverse=True)
    ranks = sorted(p[1] for p in pairs)
    curve = sorted(zip(totals, ranks))

    # Two managers on the same score must not imply two different ranks, so
    # collapse duplicates to their best rank.
    merged = {}
    for total, rank in curve:
        merged[total] = min(rank, merged.get(total, rank))
    return sorted(merged.items())


def estimate_rank(curve, total, players_total=None):
    """
    Read a live total off the curve, interpolating on log rank.

    Rank thins out geometrically rather than evenly — the gap between 1st and
    1,000th is a handful of points, the gap between a millionth and two
    millionth is one — so a straight line through the ranks themselves would
    be badly wrong in the middle. A straight line through their logarithms is
    not.
    """
    if not curve:
        return None

    lo_total, lo_rank = curve[0]
    hi_total, hi_rank = curve[-1]
    if total <= lo_total:
        return int(min(lo_rank, players_total or lo_rank))
    if total >= hi_total:
        return max(1, int(hi_rank))

    from math import log10

    for i in range(1, len(curve)):
        t1, r1 = curve[i - 1]
        t0, r0 = curve[i]
        if t1 <= total <= t0:
            if t0 == t1:
                return max(1, int(r0))
            span = (total - t1) / (t0 - t1)
            log_rank = log10(max(r1, 1)) + span * (log10(max(r0, 1)) - log10(max(r1, 1)))
            est = int(round(10 ** log_rank))
            if players_total:
                est = min(est, players_total)
            return max(1, est)
    return None


# --------------------------------------------------------------------------
# awards of the week
# --------------------------------------------------------------------------

def _card(key, title, subtitle, tone, icon, winner=None, value=None, note=None):
    card = {
        "key": key,
        "title": title,
        "subtitle": subtitle,
        "tone": tone,
        "icon": icon,
        "value": value,
        "note": note,
    }
    if winner:
        card.update({
            "entry": winner.get("entry"),
            "manager": winner.get("manager"),
            "team": winner.get("team"),
        })
    else:
        card.update({"entry": None, "manager": None, "team": None})
    return card


def _best(managers, key, reverse=True, where=None):
    pool = [m for m in managers if (where is None or where(m))]
    if not pool:
        return None
    return sorted(pool, key=key, reverse=reverse)[0]


CHIP_NAMES = {
    "bboost": "BB",
    "3xc": "TC",
    "freehit": "FH",
    "wildcard": "WC",
    "manager": "AM",
}


def weekly_awards(managers, players, ownership=None):
    """
    The Awards of the Week card list for one league.

    Each manager row is expected to carry the fields `fetch_live_data.py`
    builds: gw_points, bench_points, chip, value, hit, captain, rank_change,
    and picks. An award nobody qualifies for still returns its card, with no
    winner, so the page keeps its shape from one week to the next.
    """
    if not managers:
        return []

    def pts(pid):
        return (players.get(str(pid)) or players.get(pid) or {}).get("points", 0)

    def name(pid):
        return (players.get(str(pid)) or players.get(pid) or {}).get("name", "—")

    cards = []

    top = _best(managers, lambda m: m["gw_points"])
    cards.append(_card(
        "top_gun", "Top Gun", "Highest Gameweek score", "good", "star",
        top, f"{top['gw_points']} pts",
    ))

    low = _best(managers, lambda m: m["gw_points"], reverse=False)
    cards.append(_card(
        "tough_week", "Tough Week", "Lowest Gameweek score", "bad", "layers",
        low, f"{low['gw_points']} pts",
    ))

    riser = _best(managers, lambda m: m.get("rank_change", 0))
    up = riser.get("rank_change", 0) if riser else 0
    cards.append(_card(
        "rank_riser", "Rank Riser", "Biggest rank climb", "good", "chart",
        riser if up > 0 else None, f"+{up}" if up > 0 else None,
    ))

    faller = _best(managers, lambda m: m.get("rank_change", 0), reverse=False)
    down = faller.get("rank_change", 0) if faller else 0
    cards.append(_card(
        "rank_crasher", "Rank Crasher", "Biggest rank fall", "bad", "chart",
        faller if down < 0 else None, str(down) if down < 0 else None,
    ))

    chipped = _best(managers, lambda m: m["gw_points"], where=lambda m: m.get("chip"))
    cards.append(_card(
        "chip_master",
        "Chip Master",
        f"Best score with a chip ({CHIP_NAMES.get(chipped['chip'], chipped['chip'])})"
        if chipped else "Best score with a chip",
        "good", "trophy",
        chipped, f"{chipped['gw_points']} pts" if chipped else None,
    ))

    clean = _best(managers, lambda m: m["gw_points"], where=lambda m: not m.get("chip"))
    cards.append(_card(
        "no_chip_warrior", "No-Chip Warrior", "Best score without playing a chip",
        "good", "trophy",
        clean, f"{clean['gw_points']} pts" if clean else None,
    ))

    rich = _best(managers, lambda m: m.get("value", 0))
    cards.append(_card(
        "value_king", "Value King", "Highest Team Value", "good", "trophy",
        rich, f"£{rich.get('value', 0)}m" if rich else None,
    ))

    wasteful = _best(
        managers, lambda m: m.get("bench_points", 0),
        where=lambda m: m.get("chip") != "bboost" and m.get("bench_points", 0) >= 20,
    )
    cards.append(_card(
        "bench_disaster", "Bench Disaster",
        "Left 20+ points on the bench (no Bench Boost)", "bad", "layers",
        wasteful, f"{wasteful['bench_points']} pts" if wasteful else None,
    ))

    def captain_haul(m):
        cap = m.get("captain")
        if not cap:
            return 0
        mult = 3 if m.get("chip") == "3xc" else 2
        return pts(cap) * mult

    skipper = _best(managers, captain_haul, where=lambda m: m.get("captain"))
    cards.append(_card(
        "captain_marvel", "Captain Marvel", "Best captain haul", "good", "star",
        skipper, f"{captain_haul(skipper)} pts" if skipper else None,
        f"Captained {name(skipper['captain'])}" if skipper else None,
    ))

    flop = _best(managers, captain_haul, reverse=False, where=lambda m: m.get("captain"))
    cards.append(_card(
        "armband_fail", "Armband Fail", "Worst captain haul", "bad", "star",
        flop, f"{captain_haul(flop)} pts" if flop else None,
        f"Captained {name(flop['captain'])}" if flop else None,
    ))

    hitter = _best(
        managers, lambda m: (m.get("hit", 0), m["gw_points"]),
        where=lambda m: m.get("hit", 0) > 0,
    )
    cards.append(_card(
        "hit_man", "Hit Man", "Biggest points hit taken", "bad", "layers",
        hitter, f"-{hitter['hit']}" if hitter else None,
        f"Still scored {hitter['gw_points']} pts" if hitter else None,
    ))

    # A differential is a player almost nobody else in the league fielded --
    # here, at most a tenth of it. The award goes to whoever got the most out
    # of one.
    owners = ownership or {}
    threshold = max(1, len(managers) // 10)
    rare = {pid for pid, n in owners.items() if n <= threshold}

    def best_differential(m):
        """(points, player id) for this manager's best rare pick."""
        fielded = [p["id"] for p in m.get("picks", [])
                   if p["slot"] <= 11 or m.get("chip") == "bboost"]
        scored = [(pts(pid), pid) for pid in fielded if pid in rare]
        return max(scored) if scored else (0, None)

    diff = _best(managers, lambda m: best_differential(m)[0],
                 where=lambda m: best_differential(m)[0] > 0)
    if diff:
        diff_points, diff_pid = best_differential(diff)
        note = (f"{name(diff_pid)}, owned by "
                f"{owners.get(diff_pid, 0)} of {len(managers)}")
    else:
        diff_points, note = None, None

    cards.append(_card(
        "differential_king", "Differential King",
        "Most points from a player almost nobody owns", "good", "chart",
        diff, f"{diff_points} pts" if diff else None, note,
    ))

    return cards


def ownership_counts(managers):
    """How many managers fielded each player, captaincy counted once."""
    counts = {}
    for m in managers:
        bench_boost = m.get("chip") == "bboost"
        for p in m.get("picks", []):
            if p["slot"] > 11 and not bench_boost:
                continue
            counts[p["id"]] = counts.get(p["id"], 0) + 1
    return counts


def captain_counts(managers):
    """How many managers gave each player the armband."""
    counts = {}
    for m in managers:
        cap = m.get("captain")
        if cap:
            counts[cap] = counts.get(cap, 0) + 1
    return counts
