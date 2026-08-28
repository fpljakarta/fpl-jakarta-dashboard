"""
Tests for the season-long picture, chiefly when a month's award is safe to give.

The FPL API is only reachable from the GitHub Actions runner, so these run
against hand-built fixtures. Importing fetch_fpl_data does no network on its
own; everything under test here is a pure function over data already in hand.

    python -m unittest discover -s scripts -p 'test_*.py'
"""

import unittest
from datetime import datetime, timezone

from fetch_fpl_data import (
    event_started,
    month_is_over,
    team_values,
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

    def test_a_manager_missing_from_the_transfer_feed_still_appears(self):
        out = transfers_by_gameweek(ROWS, self.histories, {}, [2])
        self.assertEqual(len(out["2"]), 3)
        self.assertTrue(all(r["in"] == [] and r["out"] == [] for r in out["2"]))


if __name__ == "__main__":
    unittest.main()
