"""
Tests for the live-page arithmetic.

The FPL API is only reachable from the GitHub Actions runner, so these run
against hand-built fixtures instead. They cover the parts that are easy to get
subtly wrong and hard to notice afterwards: bonus ties, substitution legality
and the shape of the rank curve.

    python -m unittest discover -s scripts -p 'test_*.py'
"""

import unittest

from live_calc import (
    armband,
    bonus_from_bps,
    build_rank_curve,
    captain_counts,
    estimate_rank,
    fixtures_with_official_bonus,
    is_over,
    ownership_counts,
    predict_autosubs,
    provisional_bonus,
    sample_pages,
    score_squad,
    weekly_awards,
)


def bps(*pairs):
    return [{"element": pid, "value": value} for pid, value in pairs]


def player(pos, points=0, minutes=0, started=True, finished=True, name="X"):
    return {
        "name": name,
        "pos": pos,
        "points": points,
        "minutes": minutes,
        "started": started,
        "finished": finished,
        "team": "TST",
    }


def squad(ids_by_slot, captain=1, vice=2):
    """Build a 15-pick list from {slot: player id}."""
    picks = []
    for slot, pid in sorted(ids_by_slot.items()):
        picks.append({
            "id": pid,
            "slot": slot,
            "mult": (2 if pid == captain else 1) if slot <= 11 else 0,
            "captain": pid == captain,
            "vice": pid == vice,
        })
    return picks


class TestBonus(unittest.TestCase):
    def test_three_clear_scores(self):
        self.assertEqual(
            bonus_from_bps(bps((1, 40), (2, 33), (3, 28), (4, 20))),
            {1: 3, 2: 2, 3: 1},
        )

    def test_two_tied_at_the_top_share_three_and_next_takes_one(self):
        # FPL skips the 2 when two players share first place.
        self.assertEqual(
            bonus_from_bps(bps((1, 40), (2, 40), (3, 28), (4, 20))),
            {1: 3, 2: 3, 3: 1},
        )

    def test_three_tied_at_the_top_consume_every_award(self):
        self.assertEqual(
            bonus_from_bps(bps((1, 40), (2, 40), (3, 40), (4, 30))),
            {1: 3, 2: 3, 3: 3},
        )

    def test_two_tied_for_second_share_two_and_nobody_takes_one(self):
        self.assertEqual(
            bonus_from_bps(bps((1, 40), (2, 33), (3, 33), (4, 20))),
            {1: 3, 2: 2, 3: 2},
        )

    def test_two_tied_for_third_both_take_one(self):
        self.assertEqual(
            bonus_from_bps(bps((1, 40), (2, 33), (3, 28), (4, 28))),
            {1: 3, 2: 2, 3: 1, 4: 1},
        )

    def test_empty_table(self):
        self.assertEqual(bonus_from_bps([]), {})

    def test_malformed_rows_are_skipped(self):
        self.assertEqual(
            bonus_from_bps([{"element": 1, "value": 40}, {"nope": True}]),
            {1: 3},
        )


class TestFixtureOver(unittest.TestCase):
    def test_a_provisionally_finished_match_is_over(self):
        # This is the one that bit: FPL sets finished_provisional the moment a
        # match ends and `finished` only after the data check, so a counter
        # reading `finished` alone called six completed matches unfinished.
        self.assertTrue(is_over({"finished": False, "finished_provisional": True}))

    def test_a_checked_match_is_over(self):
        self.assertTrue(is_over({"finished": True, "finished_provisional": True}))

    def test_a_match_in_play_is_not_over(self):
        self.assertFalse(is_over({"started": True, "finished": False,
                                  "finished_provisional": False}))

    def test_a_match_that_has_not_kicked_off_is_not_over(self):
        self.assertFalse(is_over({}))


class TestOfficialBonusDetection(unittest.TestCase):
    def test_fixture_with_published_bonus_is_settled(self):
        live = [{
            "id": 1,
            "explain": [{"fixture": 7, "stats": [{"identifier": "bonus", "points": 3}]}],
        }]
        self.assertEqual(fixtures_with_official_bonus(live), {7})

    def test_zero_bonus_does_not_settle_a_fixture(self):
        live = [{
            "id": 1,
            "explain": [{"fixture": 7, "stats": [{"identifier": "bonus", "points": 0}]}],
        }]
        self.assertEqual(fixtures_with_official_bonus(live), set())

    def test_provisional_bonus_skips_settled_and_unstarted_fixtures(self):
        fixtures = [
            {"id": 1, "started": True,
             "stats": [{"identifier": "bps", "h": bps((10, 40)), "a": bps((11, 20))}]},
            {"id": 2, "started": True,
             "stats": [{"identifier": "bps", "h": bps((20, 55)), "a": []}]},
            {"id": 3, "started": False,
             "stats": [{"identifier": "bps", "h": bps((30, 60)), "a": []}]},
        ]
        # Fixture 2's bonus is already official, fixture 3 has not kicked off.
        live = [{
            "id": 20,
            "explain": [{"fixture": 2, "stats": [{"identifier": "bonus", "points": 3}]}],
        }]
        self.assertEqual(provisional_bonus(fixtures, live), {10: 3, 11: 2})

    def test_double_gameweek_accumulates(self):
        fixtures = [
            {"id": 1, "started": True,
             "stats": [{"identifier": "bps", "h": bps((10, 40)), "a": []}]},
            {"id": 2, "started": True,
             "stats": [{"identifier": "bps", "h": bps((10, 50)), "a": []}]},
        ]
        self.assertEqual(provisional_bonus(fixtures, []), {10: 6})


class TestAutosubs(unittest.TestCase):
    def setUp(self):
        # A 4-4-2: keeper, four defenders, four midfielders, two forwards,
        # then a bench of reserve keeper, defender, midfielder, forward.
        self.players = {
            1: player("GKP", minutes=90),
            2: player("DEF", minutes=90), 3: player("DEF", minutes=90),
            4: player("DEF", minutes=90), 5: player("DEF", minutes=90),
            6: player("MID", minutes=90), 7: player("MID", minutes=90),
            8: player("MID", minutes=90), 9: player("MID", minutes=90),
            10: player("FWD", minutes=90), 11: player("FWD", minutes=90),
            12: player("GKP", minutes=90, points=6),
            13: player("DEF", minutes=90, points=5),
            14: player("MID", minutes=90, points=8),
            15: player("FWD", minutes=90, points=2),
        }
        self.picks = squad({i: i for i in range(1, 16)})

    def test_no_subs_when_everyone_played(self):
        self.assertEqual(predict_autosubs(self.picks, self.players), [])

    def test_blank_midfielder_is_replaced_by_first_eligible_bench_player(self):
        self.players[9] = player("MID", minutes=0)
        # 13 is a defender, which would make it a 5-3-2 -- legal, so he comes
        # on first by bench order.
        self.assertEqual(predict_autosubs(self.picks, self.players), [(9, 13)])

    def test_keeper_is_only_replaced_by_the_other_keeper(self):
        self.players[1] = player("GKP", minutes=0)
        self.assertEqual(predict_autosubs(self.picks, self.players), [(1, 12)])

    def test_outfielder_never_replaced_by_the_reserve_keeper(self):
        self.players[10] = player("FWD", minutes=0)
        off, on = predict_autosubs(self.picks, self.players)[0]
        self.assertEqual(off, 10)
        self.assertNotEqual(on, 12)

    def test_formation_floor_is_respected(self):
        # Three defenders blank. Only one defender sits on the bench, so the
        # third replacement cannot be another defender -- and dropping below
        # three defenders is illegal, so the side has to fill from elsewhere.
        for pid in (3, 4, 5):
            self.players[pid] = player("DEF", minutes=0)
        swaps = predict_autosubs(self.picks, self.players)
        final = {p["id"] for p in self.picks if p["slot"] <= 11}
        for off, on in swaps:
            final.discard(off)
            final.add(on)
        counts = {}
        for pid in final:
            counts[self.players[pid]["pos"]] = counts.get(self.players[pid]["pos"], 0) + 1
        self.assertEqual(counts.get("GKP"), 1)
        self.assertGreaterEqual(counts.get("DEF", 0), 3)
        self.assertGreaterEqual(counts.get("MID", 0), 2)
        self.assertGreaterEqual(counts.get("FWD", 0), 1)
        self.assertEqual(len(final), 11)

    def test_bench_player_who_has_not_played_cannot_come_on(self):
        self.players[9] = player("MID", minutes=0)
        self.players[13] = player("DEF", minutes=0)
        self.assertEqual(predict_autosubs(self.picks, self.players), [(9, 14)])

    def test_player_still_to_play_is_not_substituted_out(self):
        self.players[9] = player("MID", minutes=0, finished=False, started=False)
        self.assertEqual(predict_autosubs(self.picks, self.players), [])

    def test_bench_boost_makes_no_substitutions(self):
        self.players[9] = player("MID", minutes=0)
        self.assertEqual(predict_autosubs(self.picks, self.players, bench_boost=True), [])


class TestArmband(unittest.TestCase):
    def setUp(self):
        self.players = {i: player("MID", minutes=90) for i in range(1, 16)}
        self.picks = squad({i: i for i in range(1, 16)}, captain=1, vice=2)

    def test_captain_keeps_the_armband_when_he_plays(self):
        self.assertEqual(armband(self.picks, self.players), (1, 2, False))

    def test_vice_takes_over_when_the_captain_blanks(self):
        self.players[1] = player("MID", minutes=0, finished=True)
        self.assertEqual(armband(self.picks, self.players), (2, 2, True))

    def test_armband_waits_while_the_captain_still_has_a_match(self):
        self.players[1] = player("MID", minutes=0, finished=False, started=False)
        self.assertEqual(armband(self.picks, self.players), (1, 2, False))

    def test_triple_captain_is_worth_three(self):
        self.assertEqual(armband(self.picks, self.players, chip="3xc")[1], 3)


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.players = {i: player("MID", points=2, minutes=90) for i in range(1, 16)}
        self.players[1] = player("GKP", points=2, minutes=90)
        for pid in (2, 3, 4, 5):
            self.players[pid] = player("DEF", points=2, minutes=90)
        for pid in (10, 11):
            self.players[pid] = player("FWD", points=2, minutes=90)
        self.players[12] = player("GKP", points=3, minutes=90)
        self.players[13] = player("DEF", points=7, minutes=90)
        self.players[14] = player("MID", points=9, minutes=90)
        self.players[15] = player("FWD", points=1, minutes=90)
        self.picks = squad({i: i for i in range(1, 16)}, captain=6, vice=7)

    def test_official_score_is_the_eleven_as_picked(self):
        out = score_squad(self.picks, self.players)
        # Ten players on 2, plus the captain doubled.
        self.assertEqual(out["official"], 10 * 2 + 2 * 2)
        self.assertEqual(out["projected"], out["official"])
        self.assertEqual(out["bench_points"], 3 + 7 + 9 + 1)

    def test_pending_bonus_lifts_the_projection_only(self):
        out = score_squad(self.picks, self.players, bonus={6: 3})
        self.assertEqual(out["official"], 24)
        # The captain's bonus is doubled along with everything else of his.
        self.assertEqual(out["pending_bonus"], 6)
        self.assertEqual(out["projected"], 30)

    def test_substitution_shows_up_as_a_gain(self):
        self.players[9] = player("MID", points=0, minutes=0)
        out = score_squad(self.picks, self.players)
        self.assertEqual(out["official"], 22)
        self.assertEqual(out["swaps"], [(9, 13)])
        self.assertEqual(out["sub_gain"], 7)
        self.assertEqual(out["projected"], 29)

    def test_vice_captain_picks_up_the_doubling(self):
        self.players[6] = player("MID", points=0, minutes=0)
        out = score_squad(self.picks, self.players)
        self.assertTrue(out["captain_changed"])
        self.assertEqual(out["captain"], 7)

    def test_bench_boost_counts_all_fifteen_and_substitutes_nobody(self):
        self.players[9] = player("MID", points=0, minutes=0)
        picks = squad({i: i for i in range(1, 16)}, captain=6, vice=7)
        for p in picks:
            if p["slot"] > 11:
                p["mult"] = 1
        out = score_squad(picks, self.players, bench_boost=True)
        self.assertEqual(out["swaps"], [])
        self.assertEqual(out["official"], out["projected"])


class TestRankCurve(unittest.TestCase):
    def test_a_higher_score_never_means_a_worse_rank(self):
        curve = build_rank_curve([
            (80, 1), (70, 1000), (60, 100000), (50, 2000000), (40, 8000000),
        ])
        ranks = [estimate_rank(curve, total) for total in (45, 55, 65, 75)]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_interpolation_lands_between_its_neighbours(self):
        curve = build_rank_curve([(80, 100), (60, 10000)])
        mid = estimate_rank(curve, 70)
        self.assertGreater(mid, 100)
        self.assertLess(mid, 10000)

    def test_beyond_the_sample_clamps_to_its_ends(self):
        curve = build_rank_curve([(80, 100), (60, 10000)])
        self.assertEqual(estimate_rank(curve, 200), 100)
        self.assertEqual(estimate_rank(curve, 10), 10000)

    def test_too_few_samples_gives_no_curve(self):
        self.assertEqual(build_rank_curve([(50, 1)]), [])
        self.assertIsNone(estimate_rank([], 50))

    def test_shared_scores_collapse_to_the_better_rank(self):
        curve = build_rank_curve([(70, 5), (70, 900), (60, 4000)])
        self.assertEqual(dict(curve)[70], 5)


class TestSamplePages(unittest.TestCase):
    # The depths the fetcher actually samples the global field at.
    RANKS = [1, 3, 10, 30, 100, 300, 1_000, 3_000, 10_000, 30_000, 100_000,
             300_000, 700_000, 1_500_000, 3_000_000, 5_000_000, 7_000_000,
             9_000_000]

    def test_ranks_map_to_their_page(self):
        self.assertEqual(sample_pages([1, 50, 51, 100], 1_000_000), [1, 2])

    def test_ranks_sharing_a_page_are_asked_for_once(self):
        # The first version asked for page 1 four times over -- ranks 1, 3, 10
        # and 30 all live on it -- and threw three of the four responses away,
        # which is what left the curve half as thick as intended.
        pages = sample_pages(self.RANKS, 9_299_773)
        self.assertEqual(len(pages), len(set(pages)))
        self.assertEqual(pages.count(1), 1)

    def test_every_page_is_reachable_within_the_field(self):
        total = 9_299_773
        pages = sample_pages(self.RANKS, total)
        self.assertTrue(all(1 <= p <= (total // 50) + 1 for p in pages))

    def test_pages_past_the_end_of_the_field_are_dropped(self):
        # A field of 1,000 has twenty pages; nothing deeper is worth a request.
        self.assertEqual(sample_pages([1, 500, 900, 50_000], 1_000), [1, 10, 18])

    def test_no_field_size_keeps_everything(self):
        self.assertEqual(sample_pages([1, 50_000], 0), [1, 1000])

    def test_pages_come_back_in_order_without_duplicates(self):
        pages = sample_pages([9_000_000, 1, 300, 1, 300], 9_299_773)
        self.assertEqual(pages, sorted(set(pages)))

    def test_nonsense_ranks_are_ignored(self):
        self.assertEqual(sample_pages([0, -5, 100], 1_000_000), [2])


class TestAwards(unittest.TestCase):
    def setUp(self):
        self.players = {
            100: player("MID", points=12, name="Salah"),
            200: player("FWD", points=2, name="Haaland"),
            300: player("DEF", points=9, name="Gabriel"),
        }
        base_picks = [{"id": 100, "slot": 1, "mult": 2, "captain": True, "vice": False},
                      {"id": 200, "slot": 2, "mult": 1, "captain": False, "vice": True}]
        self.managers = [
            {"entry": 1, "manager": "Ada", "team": "Ada FC", "gw_points": 54,
             "bench_points": 4, "chip": "bboost", "value": 100.5, "hit": 0,
             "captain": 100, "rank_change": 3, "picks": base_picks},
            {"entry": 2, "manager": "Bo", "team": "Bo FC", "gw_points": 38,
             "bench_points": 24, "chip": None, "value": 101.2, "hit": 4,
             "captain": 200, "rank_change": -5,
             "picks": base_picks + [{"id": 300, "slot": 3, "mult": 1,
                                     "captain": False, "vice": False}]},
            {"entry": 3, "manager": "Cy", "team": "Cy FC", "gw_points": 9,
             "bench_points": 2, "chip": None, "value": 99.8, "hit": 0,
             "captain": 200, "rank_change": -1, "picks": base_picks},
        ]
        self.cards = weekly_awards(
            self.managers, self.players, ownership={100: 3, 200: 3, 300: 1}
        )
        self.by_key = {c["key"]: c for c in self.cards}

    def test_every_award_is_present_even_when_unwon(self):
        expected = {
            "top_gun", "tough_week", "rank_riser", "rank_crasher", "chip_master",
            "no_chip_warrior", "value_king", "bench_disaster", "captain_marvel",
            "armband_fail", "hit_man", "differential_king",
        }
        self.assertEqual(set(self.by_key), expected)

    def test_top_and_bottom(self):
        self.assertEqual(self.by_key["top_gun"]["manager"], "Ada")
        self.assertEqual(self.by_key["top_gun"]["value"], "54 pts")
        self.assertEqual(self.by_key["tough_week"]["manager"], "Cy")

    def test_chip_award_names_the_chip_and_excludes_chipless_managers(self):
        self.assertEqual(self.by_key["chip_master"]["manager"], "Ada")
        self.assertIn("BB", self.by_key["chip_master"]["subtitle"])
        self.assertEqual(self.by_key["no_chip_warrior"]["manager"], "Bo")

    def test_bench_disaster_needs_twenty_points_and_no_bench_boost(self):
        self.assertEqual(self.by_key["bench_disaster"]["manager"], "Bo")

    def test_bench_disaster_goes_unawarded_below_the_threshold(self):
        for m in self.managers:
            m["bench_points"] = 5
        card = {c["key"]: c for c in weekly_awards(self.managers, self.players)}
        self.assertIsNone(card["bench_disaster"]["manager"])
        self.assertIsNone(card["bench_disaster"]["value"])

    def test_rank_movement_awards_only_fire_in_their_own_direction(self):
        self.assertEqual(self.by_key["rank_riser"]["manager"], "Ada")
        self.assertEqual(self.by_key["rank_riser"]["value"], "+3")
        self.assertEqual(self.by_key["rank_crasher"]["manager"], "Bo")
        self.assertEqual(self.by_key["rank_crasher"]["value"], "-5")

    def test_no_riser_when_everybody_fell(self):
        for m in self.managers:
            m["rank_change"] = -2
        card = {c["key"]: c for c in weekly_awards(self.managers, self.players)}
        self.assertIsNone(card["rank_riser"]["manager"])

    def test_captain_awards_use_the_delivered_points(self):
        # Ada tripled nothing but captained Salah on 12, doubled to 24.
        self.assertEqual(self.by_key["captain_marvel"]["manager"], "Ada")
        self.assertEqual(self.by_key["captain_marvel"]["value"], "24 pts")
        self.assertIn("Salah", self.by_key["captain_marvel"]["note"])
        self.assertEqual(self.by_key["armband_fail"]["value"], "4 pts")

    def test_hit_man_only_counts_managers_who_took_one(self):
        self.assertEqual(self.by_key["hit_man"]["manager"], "Bo")
        self.assertEqual(self.by_key["hit_man"]["value"], "-4")

    def test_differential_king_names_the_player_and_its_ownership(self):
        card = self.by_key["differential_king"]
        self.assertEqual(card["manager"], "Bo")
        self.assertEqual(card["value"], "9 pts")
        self.assertIn("Gabriel", card["note"])

    def test_empty_league_produces_no_cards(self):
        self.assertEqual(weekly_awards([], self.players), [])


class TestCounts(unittest.TestCase):
    def test_ownership_ignores_the_bench_unless_boosted(self):
        managers = [
            {"chip": None, "picks": [{"id": 1, "slot": 1}, {"id": 2, "slot": 12}]},
            {"chip": "bboost", "picks": [{"id": 1, "slot": 1}, {"id": 2, "slot": 12}]},
        ]
        self.assertEqual(ownership_counts(managers), {1: 2, 2: 1})

    def test_captain_counts(self):
        managers = [{"captain": 5}, {"captain": 5}, {"captain": 9}, {"captain": None}]
        self.assertEqual(captain_counts(managers), {5: 2, 9: 1})


if __name__ == "__main__":
    unittest.main()
