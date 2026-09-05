"""
Tests for the season-long picture, chiefly when a month's award is safe to give.

The FPL API is only reachable from the GitHub Actions runner, so these run
against hand-built fixtures. Importing fetch_fpl_data does no network on its
own; everything under test here is a pure function over data already in hand.

    python -m unittest discover -s scripts -p 'test_*.py'
"""

import io
import json as _json
import unittest
from datetime import datetime, timezone
from unittest import mock

import fetch_fpl_data
from fetch_fpl_data import (
    attach_squads,
    event_started,
    month_is_over,
    team_values,
    squad_chip_weeks,
    transfers_by_gameweek,
    winners_for,
)

NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)


def event(eid, deadline, finished=False, is_current=False):
    return {
        "id": eid,
        "deadline_time": deadline,
        "finished": finished,
        "is_current": is_current,
    }


# August is gameweeks 1-3, September 4-6, and so on.
AUGUST = {"name": "August", "start_event": 1, "stop_event": 3}
SEPTEMBER = {"name": "September", "start_event": 4, "stop_event": 6}
MAY = {"name": "May", "start_event": 36, "stop_event": 38}


class TestEventStarted(unittest.TestCase):
    def test_deadline_in_the_past_means_started(self):
        self.assertTrue(event_started(event(1, "2026-08-22T17:30:00Z"), NOW))

    def test_deadline_in_the_future_means_not_started(self):
        self.assertFalse(event_started(event(4, "2026-09-12T17:30:00Z"), NOW))

    def test_a_finished_gameweek_has_started(self):
        self.assertTrue(event_started(event(1, "2026-09-30T17:30:00Z", finished=True), NOW))

    def test_the_current_gameweek_has_started(self):
        self.assertTrue(event_started(event(1, "2026-09-30T17:30:00Z", is_current=True), NOW))

    def test_a_missing_or_undated_gameweek_has_not_started(self):
        self.assertFalse(event_started(None, NOW))
        self.assertFalse(event_started({"id": 4}, NOW))
        self.assertFalse(event_started(event(4, "not a date"), NOW))


class TestMonthIsOver(unittest.TestCase):
    def setUp(self):
        # GW1 played, the rest of August and all of September still to come.
        self.events = {
            1: event(1, "2026-08-22T17:30:00Z", is_current=True),
            2: event(2, "2026-08-28T17:30:00Z"),
            3: event(3, "2026-08-30T17:30:00Z"),
            4: event(4, "2026-09-12T17:30:00Z"),
            38: event(38, "2027-05-24T14:00:00Z"),
        }

    def test_august_is_not_over_while_august_is_still_being_played(self):
        self.assertFalse(month_is_over(AUGUST, SEPTEMBER, self.events, NOW))

    def test_august_is_not_over_the_moment_its_last_gameweek_finishes(self):
        # Every August gameweek done, but September has not kicked off, so the
        # month is not yet beyond change.
        for eid in (1, 2, 3):
            self.events[eid] = event(eid, "2026-08-01T00:00:00Z", finished=True)
        self.assertFalse(month_is_over(AUGUST, SEPTEMBER, self.events,
                                       datetime(2026, 9, 1, tzinfo=timezone.utc)))

    def test_august_is_over_once_septembers_first_gameweek_starts(self):
        later = datetime(2026, 9, 12, 18, tzinfo=timezone.utc)
        self.assertTrue(month_is_over(AUGUST, SEPTEMBER, self.events, later))

    def test_the_last_month_waits_for_its_own_final_gameweek(self):
        self.assertFalse(month_is_over(MAY, None, self.events, NOW))
        self.events[38] = event(38, "2027-05-24T14:00:00Z", finished=True)
        self.assertTrue(month_is_over(MAY, None, self.events, NOW))

    def test_a_month_whose_successor_is_missing_is_not_over(self):
        self.assertFalse(month_is_over(AUGUST, {"start_event": 99}, self.events, NOW))


class TestWinnersFor(unittest.TestCase):
    def setUp(self):
        self.entries = [
            {"entry_id": 1, "manager": "Ada", "team": "Ada FC"},
            {"entry_id": 2, "manager": "Bo", "team": "Bo FC"},
        ]
        self.histories = {
            1: {"current": [{"event": 1, "points": 47, "event_transfers_cost": 0}]},
            2: {"current": [{"event": 1, "points": 54, "event_transfers_cost": 8}]},
        }
        self.events = {
            1: event(1, "2026-08-22T17:30:00Z", is_current=True),
            4: event(4, "2026-09-12T17:30:00Z"),
        }

    def motm(self, now):
        cards, _ = winners_for(self.entries, [AUGUST, SEPTEMBER],
                               self.histories, self.events, now)
        return {c["month"]: c for c in cards}

    def test_a_month_in_progress_has_no_winner_but_is_marked_started(self):
        august = self.motm(NOW)["August"]
        self.assertIsNone(august["manager"])
        self.assertIsNone(august["points"])
        self.assertTrue(august["started"])
        self.assertFalse(august["settled"])

    def test_a_month_not_begun_is_neither_started_nor_settled(self):
        september = self.motm(NOW)["September"]
        self.assertIsNone(september["manager"])
        self.assertFalse(september["started"])
        self.assertFalse(september["settled"])

    def test_the_winner_appears_once_the_next_month_starts(self):
        august = self.motm(datetime(2026, 9, 12, 18, tzinfo=timezone.utc))["August"]
        # Bo scored 54 but took a -8, so Ada's 47 wins on net points.
        self.assertEqual(august["manager"], "Ada")
        self.assertEqual(august["points"], 47)
        self.assertTrue(august["settled"])

    def test_manager_of_the_week_is_unaffected_and_still_net_of_hits(self):
        # The weekly award does not wait for the month to end, and the hit is
        # what decides it: raw scores would have handed this to Bo on 54.
        _, motw = winners_for(self.entries, [AUGUST, SEPTEMBER],
                              self.histories, self.events, NOW)
        self.assertEqual([w["manager"] for w in motw], ["Ada"])
        self.assertEqual(motw[0]["points"], 47)

    def test_every_month_is_still_listed_while_unsettled(self):
        self.assertEqual(sorted(self.motm(NOW)), ["August", "September"])


ROWS = [
    {"entry_id": 1, "manager": "Ada", "team": "Ada FC"},
    {"entry_id": 2, "manager": "Bo", "team": "Bo Rovers"},
    {"entry_id": 3, "manager": "Cy", "team": "Cy United"},
]


def hist(current, chips=None):
    return {"current": current, "chips": chips or []}


def gw(event_id, value, bank=0):
    return {"event": event_id, "value": value, "bank": bank}


class TestTeamValues(unittest.TestCase):
    """
    FPL's `value` is the squad *and* the bank, not the squad alone -- confirmed
    against real league data before this was written. Adding the bank on top
    would count it twice, and a prize is decided on this number.
    """

    def test_richest_first_and_pounds_not_tenths(self):
        histories = {
            1: hist([gw(1, 1000), gw(2, 1002, bank=5)]),
            2: hist([gw(1, 1000), gw(2, 1007)]),
            3: hist([gw(1, 1000), gw(2, 999, bank=55)]),
        }
        out = team_values(ROWS, histories)
        self.assertEqual([m["manager"] for m in out], ["Bo", "Ada", "Cy"])
        self.assertEqual([m["value"] for m in out], [100.7, 100.2, 99.9])

    def test_the_bank_is_reported_but_never_added_on(self):
        # 99.9 with 5.5 in the bank is a 94.4 squad, not a 105.4 team.
        out = team_values([ROWS[2]], {3: hist([gw(2, 999, bank=55)])})
        self.assertEqual(out[0]["value"], 99.9)
        self.assertEqual(out[0]["bank"], 5.5)

    def test_change_is_the_move_since_last_gameweek(self):
        out = team_values([ROWS[0]], {1: hist([gw(1, 1000), gw(2, 1002)])})
        self.assertEqual(out[0]["change"], 0.2)

    def test_the_first_gameweek_has_nothing_to_compare_against(self):
        out = team_values([ROWS[0]], {1: hist([gw(1, 1000)])})
        self.assertIsNone(out[0]["change"])

    def test_a_manager_with_no_history_is_left_out_rather_than_shown_as_zero(self):
        out = team_values(ROWS, {1: hist([gw(1, 1000)]), 2: hist([])})
        self.assertEqual([m["manager"] for m in out], ["Ada"])

    def test_a_tie_is_broken_by_name_so_the_order_never_jitters(self):
        histories = {1: hist([gw(1, 1000)]), 2: hist([gw(1, 1000)]),
                     3: hist([gw(1, 1000)])}
        self.assertEqual([m["manager"] for m in team_values(ROWS, histories)],
                         ["Ada", "Bo", "Cy"])
        self.assertEqual([m["manager"] for m in team_values(ROWS[::-1], histories)],
                         ["Ada", "Bo", "Cy"])


class TestTransfersByGameweek(unittest.TestCase):
    def setUp(self):
        self.histories = {
            1: hist([gw(1, 1000)]),
            2: hist([gw(1, 1000)], chips=[{"name": "wildcard", "event": 2}]),
            3: hist([gw(1, 1000)], chips=[{"name": "bboost", "event": 2}]),
        }
        self.transfers = {
            1: [{"event": 2, "element_in": 11, "element_out": 22}],
            2: [{"event": 2, "element_in": 33, "element_out": 44},
                {"event": 2, "element_in": 55, "element_out": 66}],
            3: [],
        }

    def test_each_manager_gets_a_row_even_with_no_transfers(self):
        out = transfers_by_gameweek(ROWS, self.histories, self.transfers, [1, 2])
        self.assertEqual(sorted(out), ["1", "2"])
        self.assertEqual(len(out["2"]), 3)
        cy = [m for m in out["2"] if m["manager"] == "Cy"][0]
        self.assertEqual((cy["in"], cy["out"]), ([], []))

    def test_transfers_land_in_the_gameweek_they_were_made_for(self):
        out = transfers_by_gameweek(ROWS, self.histories, self.transfers, [1, 2])
        ada2 = [m for m in out["2"] if m["manager"] == "Ada"][0]
        ada1 = [m for m in out["1"] if m["manager"] == "Ada"][0]
        self.assertEqual((ada2["in"], ada2["out"]), ([11], [22]))
        self.assertEqual((ada1["in"], ada1["out"]), ([], []))

    def test_a_chip_is_carried_through(self):
        out = transfers_by_gameweek(ROWS, self.histories, self.transfers, [2])
        by_name = {m["manager"]: m for m in out["2"]}
        self.assertEqual(by_name["Bo"]["chip"], "wildcard")
        self.assertEqual(by_name["Cy"]["chip"], "bboost")
        self.assertIsNone(by_name["Ada"]["chip"])

    def test_a_gameweek_that_has_not_started_is_not_published(self):
        # FPL will report a transfer already made for next week. Publishing it
        # would let anyone watch their rivals plan before the deadline.
        pending = {1: [{"event": 3, "element_in": 99, "element_out": 88}]}
        out = transfers_by_gameweek(ROWS, self.histories, pending, [1, 2])
        self.assertNotIn("3", out)
        for week in out.values():
            for row in week:
                self.assertEqual((row["in"], row["out"]), ([], []))

    def test_a_player_who_went_both_ways_cancels_out(self):
        # FPL logs every move as it is made, not the net effect. Buying Palmer
        # and selling him again before the deadline is two rows in the feed and
        # nothing at all in the squad.
        churn = {1: [{"event": 2, "element_in": 11, "element_out": 22},
                     {"event": 2, "element_in": 33, "element_out": 11}]}
        out = transfers_by_gameweek(ROWS, self.histories, churn, [2])
        ada = [m for m in out["2"] if m["manager"] == "Ada"][0]
        self.assertEqual((ada["out"], ada["in"]), ([22], [33]))

    def test_netting_survives_a_wildcard_rebuild(self):
        # A real one: 37 moves in and 37 out, of which 26 were the manager
        # trying shapes. Only the players who ended up somewhere different
        # should show.
        moves = ([{"event": 2, "element_in": 10 + i, "element_out": 50 + i} for i in range(5)]
                 + [{"event": 2, "element_in": 50 + i, "element_out": 10 + i} for i in range(3)])
        out = transfers_by_gameweek([ROWS[0]], self.histories, {1: moves}, [2])
        row = out["2"][0]
        self.assertEqual(sorted(row["in"]), [13, 14])
        self.assertEqual(sorted(row["out"]), [53, 54])

    def test_a_player_bought_twice_and_sold_once_is_still_a_buy(self):
        moves = [{"event": 2, "element_in": 11, "element_out": 22},
                 {"event": 2, "element_in": 11, "element_out": 33},
                 {"event": 2, "element_in": 44, "element_out": 11}]
        out = transfers_by_gameweek([ROWS[0]], self.histories, {1: moves}, [2])
        row = out["2"][0]
        self.assertEqual(sorted(row["in"]), [11, 44])
        self.assertEqual(sorted(row["out"]), [22, 33])

    def test_a_manager_missing_from_the_transfer_feed_still_appears(self):
        out = transfers_by_gameweek(ROWS, self.histories, {}, [2])
        self.assertEqual(len(out["2"]), 3)
        self.assertTrue(all(r["in"] == [] and r["out"] == [] for r in out["2"]))


class Outage:
    """A stand-in for urlopen that fails for a while and then recovers.

    Counts in seconds of simulated clock rather than in attempts, because what
    decides whether a run survives a rollover is how long FPL is down, not how
    many times we happened to ask.
    """

    def __init__(self, seconds_down, payload=None, error=None):
        self.seconds_down = seconds_down
        self.payload = {"ok": True} if payload is None else payload
        self.error = error or OSError("HTTP Error 503: Service Unavailable")
        self.now = 0.0
        self.attempts = 0

    def sleep(self, seconds):
        self.now += seconds

    def urlopen(self, req, timeout=None):
        self.attempts += 1
        if self.now < self.seconds_down:
            raise self.error
        body = _json.dumps(self.payload).encode("utf-8")
        return mock.MagicMock(
            __enter__=lambda s: io.BytesIO(body), __exit__=lambda *a: False
        )


class TestFetchRetryBudget(unittest.TestCase):
    """The 503 window FPL opens while it rolls a gameweek over.

    On 4 September 2026 the hourly run asked for the High Stakes standings 43
    minutes after the GW3 deadline, got a 503, and gave up ten seconds later --
    leaving the site on GW2 until the next run, which the schedule does not
    promise for hours.
    """

    def setUp(self):
        fetch_fpl_data.reset_retry_budget()
        self.addCleanup(fetch_fpl_data.reset_retry_budget)

    def run_fetch(self, outage, budget=None):
        if budget is not None:
            fetch_fpl_data.reset_retry_budget(budget)
        with mock.patch.object(fetch_fpl_data.urllib.request, "urlopen",
                               outage.urlopen), \
             mock.patch.object(fetch_fpl_data.time, "sleep", outage.sleep):
            return fetch_fpl_data.fetch_json("http://example/api")

    def test_rides_out_the_rollover_outage(self):
        # A minute of 503s -- six times longer than the old three-tries-in-nine
        # -seconds gave up after -- is survived, and the caller never knows.
        outage = Outage(seconds_down=60)
        self.assertEqual(self.run_fetch(outage), {"ok": True})
        self.assertGreaterEqual(outage.now, 60)

    def test_survives_several_minutes_down(self):
        outage = Outage(seconds_down=300)
        self.assertEqual(self.run_fetch(outage), {"ok": True})

    def test_the_old_budget_would_not_have_survived_it(self):
        # Guards the fix itself: with the ten seconds the old code allowed,
        # the 4 September outage is still fatal. If someone shrinks the budget
        # back, this is the test that says what it costs.
        outage = Outage(seconds_down=60)
        with self.assertRaises(RuntimeError):
            self.run_fetch(outage, budget=10)

    def test_gives_up_once_the_budget_is_spent(self):
        # An outage longer than any run should wait for still ends in a clean
        # RuntimeError naming the URL, not an endless loop.
        outage = Outage(seconds_down=10 ** 6)
        with self.assertRaises(RuntimeError) as caught:
            self.run_fetch(outage, budget=120)
        self.assertIn("http://example/api", str(caught.exception))
        self.assertLessEqual(outage.now, 120)

    def test_backs_off_instead_of_hammering(self):
        # Waiting out five minutes must not mean hundreds of requests at FPL
        # while it is trying to recover.
        outage = Outage(seconds_down=300)
        self.run_fetch(outage)
        self.assertLess(outage.attempts, 20)

    def test_budget_is_shared_across_the_run(self):
        # Hundreds of URLs are fetched per run. A per-URL budget would let a
        # long outage hold the runner for hours, so the pool is spent once.
        fetch_fpl_data.reset_retry_budget(30)
        first = Outage(seconds_down=10 ** 6)
        with self.assertRaises(RuntimeError):
            with mock.patch.object(fetch_fpl_data.urllib.request, "urlopen",
                                   first.urlopen), \
                 mock.patch.object(fetch_fpl_data.time, "sleep", first.sleep):
                fetch_fpl_data.fetch_json("http://example/one")
        second = Outage(seconds_down=10 ** 6)
        with self.assertRaises(RuntimeError):
            with mock.patch.object(fetch_fpl_data.urllib.request, "urlopen",
                                   second.urlopen), \
                 mock.patch.object(fetch_fpl_data.time, "sleep", second.sleep):
                fetch_fpl_data.fetch_json("http://example/two")
        # The pool was emptied by the first URL, so the second only got its
        # one free attempt.
        self.assertEqual(second.attempts, 1)

    def test_a_working_api_is_not_slowed_down(self):
        outage = Outage(seconds_down=0)
        self.assertEqual(self.run_fetch(outage), {"ok": True})
        self.assertEqual(outage.attempts, 1)
        self.assertEqual(outage.now, 0)


def wk(entry_id, chip=None):
    return {"entry_id": entry_id, "manager": f"M{entry_id}", "team": f"T{entry_id}",
            "in": [], "out": [], "chip": chip}


class TestSquadChipWeeks(unittest.TestCase):
    """Which managers need their squad fetched either side of a chip."""

    def test_finds_wildcards_and_free_hits_only(self):
        published = {"main": {
            "2": [wk(1, "wildcard"), wk(2), wk(3, "bboost")],
            "3": [wk(1), wk(2, "freehit"), wk(3, "3xc")],
        }}
        self.assertEqual(squad_chip_weeks(published), [(1, 2), (2, 3)])

    def test_a_manager_in_both_leagues_is_asked_for_once(self):
        published = {
            "main": {"2": [wk(7, "wildcard")]},
            "high_stakes": {"2": [wk(7, "wildcard")]},
        }
        self.assertEqual(squad_chip_weeks(published), [(7, 2)])

    def test_nothing_to_fetch_when_no_squad_chip_was_played(self):
        published = {"main": {"2": [wk(1), wk(2, "bboost")]}}
        self.assertEqual(squad_chip_weeks(published), [])

    def test_only_published_gameweeks_are_reachable(self):
        # transfers_by_gameweek already drops gameweeks that have not started,
        # so a chip played for next week cannot pull a squad in early.
        published = {"main": {"2": [wk(1, "wildcard")]}}
        self.assertEqual(squad_chip_weeks(published), [(1, 2)])


class TestAttachSquads(unittest.TestCase):

    def test_the_squad_either_side_lands_on_the_row(self):
        published = {"main": {"3": [wk(1, "wildcard")]}}
        squads = {(1, 3): [10, 11, 12], (1, 2): [20, 21, 22]}
        attach_squads(published, squads)
        row = published["main"]["3"][0]
        self.assertEqual(row["squad_before"], [20, 21, 22])
        self.assertEqual(row["squad_after"], [10, 11, 12])

    def test_a_chip_in_the_opening_week_has_no_team_before(self):
        published = {"main": {"1": [wk(1, "wildcard")]}}
        attach_squads(published, {(1, 1): [10, 11]})
        row = published["main"]["1"][0]
        self.assertEqual(row["squad_after"], [10, 11])
        self.assertNotIn("squad_before", row)

    def test_a_failed_fetch_leaves_the_row_alone_rather_than_empty(self):
        published = {"main": {"3": [wk(1, "freehit")]}}
        attach_squads(published, {})
        row = published["main"]["3"][0]
        self.assertNotIn("squad_before", row)
        self.assertNotIn("squad_after", row)

    def test_rows_without_a_squad_chip_are_untouched(self):
        published = {"main": {"3": [wk(1), wk(2, "bboost")]}}
        attach_squads(published, {(1, 3): [1], (1, 2): [2], (2, 3): [3], (2, 2): [4]})
        for row in published["main"]["3"]:
            self.assertNotIn("squad_before", row)
            self.assertNotIn("squad_after", row)


if __name__ == "__main__":
    unittest.main()
