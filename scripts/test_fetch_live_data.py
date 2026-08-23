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

from fetch_live_data import build_fixture_cards

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


if __name__ == "__main__":
    unittest.main()
