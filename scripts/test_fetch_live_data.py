"""
Tests for the parts of the live fetcher that are not pure arithmetic.

`build_fixture_cards` stitches the FPL API's several shapes together — teams by
id, players by id, stats by identifier, ownership by league — and that
stitching is where a fixture card goes wrong quietly: a count against the wrong
league, a scorer credited to the wrong side, a team id that never resolves.
Importing the module does no network of its own.

    python -m unittest discover -s scripts -p 'test_*.py'
"""

import unittest
from datetime import datetime, timezone

from fetch_live_data import (
    PUBLISH_WINDOW_MINUTES as WINDOW,
    build_fixture_cards,
    deadline_imminent,
    kickoff_imminent,
)

# Two teams, four players, split two apiece.
BOOTSTRAP = {
    "teams": [
        {"id": 1, "short_name": "ARS"},
        {"id": 2, "short_name": "CHE"},
        {"id": 3, "short_name": "TOT"},
    ],
    "elements": [
        {"id": 10, "web_name": "Saka", "team": 1},
        {"id": 11, "web_name": "Rice", "team": 1},
        {"id": 20, "web_name": "Palmer", "team": 2},
        {"id": 21, "web_name": "James", "team": 2},
        {"id": 30, "web_name": "Son", "team": 3},
    ],
}


def bps(*pairs):
    return [{"element": pid, "value": value} for pid, value in pairs]


def fixture(**kw):
    base = {
        "id": 1, "team_h": 1, "team_a": 2,
        "team_h_score": 2, "team_a_score": 1,
        "started": True, "finished": False, "finished_provisional": True,
        "minutes": 90, "kickoff_time": "2026-08-22T14:00:00Z", "stats": [],
    }
    base.update(kw)
    return base


class TestBuildFixtureCards(unittest.TestCase):
    def cards(self, fixtures, fielded=None):
        return build_fixture_cards(fixtures, BOOTSTRAP, fielded or {})

    def test_teams_resolve_to_their_short_names(self):
        card = self.cards([fixture()])[0]
        self.assertEqual((card["home"], card["away"]), ("ARS", "CHE"))
        self.assertEqual((card["home_score"], card["away_score"]), (2, 1))
        self.assertEqual(card["status"], "FT")

    def test_ownership_is_counted_per_league(self):
        # High Stakes fields both Arsenal players; the main league fields one
        # of them and a Spurs player who is not in this match at all.
        card = self.cards([fixture()], {
            "high_stakes": {10, 11},
            "main": {10, 30},
        })[0]
        self.assertEqual(card["owned"], {"high_stakes": 2, "main": 1})

    def test_both_sides_of_a_fixture_count_towards_it(self):
        card = self.cards([fixture()], {"high_stakes": {10, 20, 30}})[0]
        # Saka and Palmer are in the match; Son is not.
        self.assertEqual(card["owned"], {"high_stakes": 2})

    def test_a_league_with_nobody_in_the_match_counts_zero(self):
        card = self.cards([fixture()], {"high_stakes": {30}})[0]
        self.assertEqual(card["owned"], {"high_stakes": 0})

    def test_scorers_are_named_and_kept_on_their_own_side(self):
        card = self.cards([fixture(stats=[{
            "identifier": "goals_scored",
            "h": [{"element": 10, "value": 2}],
            "a": [{"element": 20, "value": 1}],
        }])])[0]
        self.assertEqual(card["scorers"]["home"],
                         [{"name": "Saka", "goals": 2, "og": False}])
        self.assertEqual(card["scorers"]["away"],
                         [{"name": "Palmer", "goals": 1, "og": False}])

    def test_an_own_goal_is_named_under_the_side_it_helped(self):
        card = self.cards([fixture(stats=[{
            "identifier": "own_goals", "h": [{"element": 11, "value": 1}], "a": [],
        }])])[0]
        self.assertEqual(card["scorers"]["home"], [])
        self.assertEqual(card["scorers"]["away"],
                         [{"name": "Rice", "goals": 1, "og": True}])

    def test_bonus_is_named_and_ordered_highest_first(self):
        card = self.cards([fixture(stats=[{
            "identifier": "bps", "h": bps((10, 40), (11, 33)), "a": bps((20, 28)),
        }])])[0]
        self.assertEqual([(b["name"], b["points"]) for b in card["bonus"]],
                         [("Saka", 3), ("Rice", 2), ("Palmer", 1)])

    def test_a_fixture_still_to_kick_off_carries_no_score_or_status(self):
        card = self.cards([fixture(started=False, finished_provisional=False,
                                   team_h_score=None, team_a_score=None)])[0]
        self.assertFalse(card["started"])
        self.assertIsNone(card["status"])
        self.assertIsNone(card["home_score"])
        self.assertEqual(card["bonus"], [])

    def test_fixtures_come_back_in_kick_off_order(self):
        late = fixture(id=2, kickoff_time="2026-08-22T16:30:00Z")
        early = fixture(id=1, kickoff_time="2026-08-22T11:30:00Z")
        self.assertEqual([c["id"] for c in self.cards([late, early])], [1, 2])

    def test_an_unknown_team_does_not_break_the_card(self):
        card = self.cards([fixture(team_a=99)])[0]
        self.assertEqual(card["away"], "?")


def at(hhmm):
    """A UTC moment on the day the fixtures below kick off."""
    hh, mm = hhmm.split(":")
    return datetime(2026, 8, 23, int(hh), int(mm), tzinfo=timezone.utc)


class KickoffImminentTests(unittest.TestCase):
    """
    Whether a run should stay awake for football that has not started yet.

    This is what stops a run waking shortly before a kick-off, finding nothing
    in play and exiting in seconds. GitHub's next scheduled run can be nearly
    two hours later, and that silence covers a whole half.
    """

    def setUp(self):
        self.one_oclock = [fixture(id=1, started=False, finished=False,
                                   finished_provisional=False,
                                   kickoff_time="2026-08-23T13:00:00Z")]

    def test_a_kick_off_inside_the_window_keeps_the_run_awake(self):
        self.assertTrue(kickoff_imminent(self.one_oclock, at("12:15"), WINDOW))

    def test_the_run_that_went_quiet_on_23_august_would_now_wait(self):
        # The scheduled runs that day landed at 09:56, 10:55, 11:46 and then
        # not again until 13:41. The 11:46 one found nothing in play, exited,
        # and nobody published the 13:00 kick-off or the half after it.
        #
        # At the window this repository now uses, every one of those morning
        # runs waits for the kick-off instead of exiting -- which is the whole
        # point of a wide window: any delivered run covers the afternoon.
        for landed in ("11:46", "10:55", "09:56"):
            with self.subTest(run=landed):
                self.assertTrue(kickoff_imminent(self.one_oclock, at(landed), WINDOW))

    def test_a_kick_off_beyond_the_window_does_not(self):
        # Overnight, with the football most of a day away, there is nothing
        # worth holding a runner open for.
        self.assertFalse(kickoff_imminent(self.one_oclock, at("01:31"), WINDOW))
        self.assertFalse(kickoff_imminent(self.one_oclock, at("07:00"), WINDOW))

    def test_the_edge_of_a_given_window_counts(self):
        # Exactly on the boundary, waiting is still the right answer.
        self.assertTrue(kickoff_imminent(self.one_oclock, at("12:05"), 55))
        self.assertFalse(kickoff_imminent(self.one_oclock, at("12:04"), 55))

    def test_a_kick_off_already_past_does_not_count(self):
        # Once the whistle has gone, match_in_progress keeps the run alive.
        # Counting the kick-off again would hold a run open all evening after
        # the last match of the day had finished.
        self.assertFalse(kickoff_imminent(self.one_oclock, at("13:01"), WINDOW))

    def test_a_match_already_under_way_is_not_imminent(self):
        started = [fixture(id=1, started=True, finished=False,
                           finished_provisional=False,
                           kickoff_time="2026-08-23T13:00:00Z")]
        self.assertFalse(kickoff_imminent(started, at("13:20"), WINDOW))

    def test_a_finished_match_is_not_imminent(self):
        done = [fixture(id=1, started=True, finished=True,
                        kickoff_time="2026-08-23T13:00:00Z")]
        self.assertFalse(kickoff_imminent(done, at("12:30"), WINDOW))

    def test_the_nearest_of_several_fixtures_decides(self):
        later = fixture(id=2, started=False, finished=False,
                        finished_provisional=False,
                        kickoff_time="2026-08-23T20:00:00Z")
        self.assertTrue(kickoff_imminent(self.one_oclock + [later], at("12:15"), WINDOW))
        # With only the evening game left, 12:15 is still too early to wait.
        self.assertFalse(kickoff_imminent([later], at("12:15"), WINDOW))
        # Closer in, the same evening game is worth waiting for.
        self.assertTrue(kickoff_imminent([later], at("16:00"), WINDOW))

    def test_a_missing_or_unreadable_kick_off_is_ignored(self):
        for bad in (None, "", "not a timestamp"):
            with self.subTest(kickoff=bad):
                fx = [fixture(id=1, started=False, finished=False,
                              finished_provisional=False, kickoff_time=bad)]
                self.assertFalse(kickoff_imminent(fx, at("12:15"), WINDOW))


def ev(deadline):
    return {"id": 1, "deadline_time": deadline}


class DeadlineImminentTests(unittest.TestCase):
    """
    Whether a run should stay awake for a gameweek deadline.

    Missing a deadline is worse than missing a kick-off. A kick-off missed
    still shows the right gameweek with an old score; a deadline missed shows
    the wrong gameweek entirely, and nothing later in the week corrects it.

    The times here are the real ones from 28 August 2026, when GW2's deadline
    passed at 17:30 and the site sat on GW1 for nearly two hours because the
    last scheduled run had been at 01:31.
    """

    def setUp(self):
        # GW2 has started; GW3's deadline is a week out.
        self.events = [ev("2026-08-21T17:30:00Z"),
                       ev("2026-08-28T17:30:00Z"),
                       ev("2026-09-04T17:30:00Z")]

    def at(self, hh, mm, day=28):
        return datetime(2026, 8, day, hh, mm, tzinfo=timezone.utc)

    def test_the_run_that_went_quiet_on_28_august_would_now_wait(self):
        # 13:31 is inside five hours of the 17:30 deadline, so a run landing
        # any time that afternoon now carries the site through the rollover.
        self.assertTrue(deadline_imminent(self.events, self.at(13, 31), WINDOW))
        self.assertTrue(deadline_imminent(self.events, self.at(17, 29), WINDOW))

    def test_a_run_far_from_any_deadline_still_exits(self):
        # 01:31, when the last run before the rollover actually landed. Sixteen
        # hours out: nothing to hold a runner open for.
        self.assertFalse(deadline_imminent(self.events, self.at(1, 31), WINDOW))

    def test_a_deadline_just_passed_is_not_imminent(self):
        # Otherwise every run for the rest of the week would hold open.
        self.assertFalse(deadline_imminent(self.events, self.at(17, 31), WINDOW))

    def test_the_window_edge_counts(self):
        self.assertTrue(deadline_imminent(self.events, self.at(12, 30), 300))
        self.assertFalse(deadline_imminent(self.events, self.at(12, 29), 300))

    def test_the_nearest_future_deadline_is_the_one_that_counts(self):
        # GW3 is a week out; that must not keep a runner alive all week.
        after = [ev("2026-08-21T17:30:00Z"), ev("2026-08-28T17:30:00Z")]
        self.assertFalse(deadline_imminent(after, self.at(18, 0), WINDOW))

    def test_no_events_and_unreadable_stamps_are_ignored(self):
        for bad in ([], None, [ev(None)], [ev("")], [ev("not a timestamp")]):
            with self.subTest(events=bad):
                self.assertFalse(deadline_imminent(bad, self.at(13, 31), WINDOW))

    def test_the_last_deadline_of_the_season_leaves_nothing_to_wait_for(self):
        self.assertFalse(deadline_imminent(
            [ev("2026-08-28T17:30:00Z")], self.at(20, 0), WINDOW))


if __name__ == "__main__":
    unittest.main()
