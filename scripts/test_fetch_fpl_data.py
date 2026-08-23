"""
Tests for the season-long picture, chiefly when a month's award is safe to give.

The FPL API is only reachable from the GitHub Actions runner, so these run
against hand-built fixtures. Importing fetch_fpl_data does no network on its
own; everything under test here is a pure function over data already in hand.

    python -m unittest discover -s scripts -p 'test_*.py'
"""

import unittest
from datetime import datetime, timezone

from fetch_fpl_data import event_started, month_is_over, winners_for

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


if __name__ == "__main__":
    unittest.main()
