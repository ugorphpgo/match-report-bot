"""Статус команды: порядок матчей внутри турнира и пометка топа в файле списка.

Топ-матч — пара, где обе команды не ниже «сильной»; плюс свой клуб локали в
международном клубном турнире. Внутри турнира: сначала топ-матчи, затем
остальные; в каждой части — по статусам пары (старший статус, младший), при
равенстве ранний матч выше. Порядок турниров и деление на виджеты не
меняются. Настоящие rank_matches / rank_reserve / write_locale_lists на
своей таблице статусов — числа из config/ тест не красят.

    python tests/test_team_status.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import match_report  # noqa: E402

LEAGUE, CUP, EUROCUP, EUROPA = 10, 20, 30, 31
PRIORITIES = {LEAGUE: 500, CUP: 400, EUROCUP: 900, EUROPA: 800}
GROUPS = {EUROCUP: "international_clubs", EUROPA: "international_clubs"}

# team_id -> статус
SPAIN, GERMANY, CROATIA, CZECHIA, ANDORRA, FAROE = 1, 2, 3, 4, 5, 6
GALA, AJAX, PSV, LYON = 11, 12, 13, 14
STATUS = {
    "teams": {
        str(SPAIN): {"name": "Spain", "status": "top", "by": "rating"},
        str(GERMANY): {"name": "Germany", "status": "top", "by": "rating"},
        str(CROATIA): {"name": "Croatia", "status": "strong", "by": "rating"},
        str(CZECHIA): {"name": "Czechia", "status": "strong", "by": "rating"},
        str(AJAX): {"name": "Ajax", "status": "top", "by": "manual"},
    },
    # У каждого международного клубного турнира — свой список.
    "eurocups": {str(EUROCUP): {
        str(SPAIN): {"name": "Spain", "status": "top", "by": "manual"},
        str(GERMANY): {"name": "Germany", "status": "top", "by": "manual"},
        str(AJAX): {"name": "Ajax", "status": "top", "by": "manual"},
    }},
    "locales": {"turkey": {"league_id": 600, "own_clubs": [GALA]}},
    "rating": {"top": 20, "strong": 50},
}


def match(league_id, home, away, hour=15, day="2026-09-25"):
    return {"league": {"id": league_id, "name": f"L{league_id}"},
            "homeTeam": {"id": home, "name": f"T{home}"},
            "awayTeam": {"id": away, "name": f"T{away}"},
            "country": {"name": "England"},
            "date": f"{day}T{hour:02d}:00:00Z"}


def pairs(ranked):
    return [(m["homeTeam"]["id"], m["awayTeam"]["id"]) for m in ranked]


class StatusCase(unittest.TestCase):
    status = STATUS

    def setUp(self):
        for name, value in (("PRIORITIES", PRIORITIES), ("GROUPS", GROUPS),
                            ("DEFAULT_PRIORITY", 1), ("EXCLUDED", {}),
                            ("TEAM_STATUS", self.status)):
            p = patch.object(match_report, name, value)
            p.start()
            self.addCleanup(p.stop)

    def rank(self, matches, locale="global"):
        return match_report.rank_matches(matches, {}, locale)


class OrderInsideTournament(StatusCase):
    def test_top_matches_first_then_the_rest(self):
        ranked = self.rank([match(LEAGUE, FAROE, ANDORRA), match(LEAGUE, SPAIN, GERMANY)])
        self.assertEqual(pairs(ranked), [(SPAIN, GERMANY), (FAROE, ANDORRA)])

    def test_pair_order_is_senior_status_then_junior(self):
        ranked = self.rank([
            match(LEAGUE, FAROE, ANDORRA),        # обычная + обычная
            match(LEAGUE, CROATIA, CZECHIA),      # сильная + сильная
            match(LEAGUE, SPAIN, ANDORRA),        # топ + обычная
            match(LEAGUE, SPAIN, CROATIA),        # топ + сильная
            match(LEAGUE, SPAIN, GERMANY),        # топ + топ
            match(LEAGUE, CROATIA, FAROE),        # сильная + обычная
        ])
        self.assertEqual(pairs(ranked), [
            (SPAIN, GERMANY), (SPAIN, CROATIA), (CROATIA, CZECHIA),
            (SPAIN, ANDORRA), (CROATIA, FAROE), (FAROE, ANDORRA)])

    def test_home_and_away_do_not_matter(self):
        ranked = self.rank([match(LEAGUE, ANDORRA, SPAIN), match(LEAGUE, CROATIA, SPAIN)])
        self.assertEqual(pairs(ranked), [(CROATIA, SPAIN), (ANDORRA, SPAIN)])

    def test_equal_pairs_earlier_kickoff_first(self):
        # Highlightly отдаёт по убыванию времени — наоборот прежнему порядку.
        ranked = self.rank([match(LEAGUE, 50, 51, hour=19), match(LEAGUE, 52, 53, hour=13),
                            match(LEAGUE, 54, 55, hour=16)])
        self.assertEqual(pairs(ranked), [(52, 53), (54, 55), (50, 51)])

    def test_kickoff_compared_as_time_not_as_text(self):
        early = {**match(LEAGUE, 50, 51), "date": "2026-09-25T09:00:00.000Z"}
        late = {**match(LEAGUE, 52, 53), "date": "2026-09-25T15:00:00Z"}
        self.assertEqual(pairs(self.rank([late, early])), [(50, 51), (52, 53)])

    def test_tournaments_keep_priority_order_and_stay_together(self):
        ranked = self.rank([match(CUP, SPAIN, GERMANY), match(LEAGUE, FAROE, ANDORRA),
                            match(CUP, FAROE, ANDORRA), match(LEAGUE, SPAIN, GERMANY)])
        self.assertEqual([m["league"]["id"] for m in ranked], [LEAGUE, LEAGUE, CUP, CUP])
        self.assertEqual(pairs(ranked), [(SPAIN, GERMANY), (FAROE, ANDORRA)] * 2)

    def test_reserve_uses_the_same_order(self):
        matches = [match(LEAGUE, FAROE, ANDORRA), match(LEAGUE, SPAIN, GERMANY)]
        with patch.object(match_report, "_list_end", lambda ordered: 0):
            reserve = match_report.rank_reserve(matches, {}, "global")
        self.assertEqual(pairs(reserve), [(SPAIN, GERMANY), (FAROE, ANDORRA)])


class TournamentStaysWholeForWidgets(StatusCase):
    def test_top_and_rest_land_in_one_widget(self):
        matches = []
        for n in range(1, 41):
            matches.append(match(100 + n, 2 * n + 100, 2 * n + 101))
        matches += [match(LEAGUE, FAROE, ANDORRA), match(LEAGUE, SPAIN, GERMANY),
                    match(LEAGUE, 60, 61)]
        prios = {LEAGUE: 300, **{100 + n: 200 for n in range(1, 41)}}
        with patch.object(match_report, "PRIORITIES", prios):
            ranked = match_report.rank_matches(matches, {}, "global")
            rest, events = match_report.split_widgets(ranked)
        where = [("events" if m in events else "matches", m["homeTeam"]["id"])
                 for m in ranked if m["league"]["id"] == LEAGUE]
        self.assertEqual({w for w, _ in where}, {"events"})
        self.assertEqual([h for _, h in where][0], SPAIN)


EPL, LALIGA, BUNDES, SERIEA, LIGUE1 = 41, 42, 43, 44, 45
BIG5 = {EPL: 480, LALIGA: 475, BUNDES: 470, SERIEA: 465, LIGUE1: 460}
ARS, LIV, CHE, VIL, EVE, BRE = 71, 72, 73, 74, 75, 76
RMA, BAR, SEV, BET = 81, 82, 83, 84
BAY, BVB, WOB, AUG = 91, 92, 93, 94
INT, ATA, LEC, CAG = 101, 102, 103, 104
PSG, LIL, NAN, AUX = 111, 112, 113, 114


def tiers(**by_status):
    return {str(t): {"name": f"T{t}", "status": status, "by": "manual"}
            for status, ids in by_status.items() for t in ids}


class TopBonus(StatusCase):
    """Надбавка к приоритету топ-части турнира: первые пресеты дня — топ-матчи
    разных чемпионатов, а не один чемпионат подряд."""
    status = {
        "teams": tiers(top=[ARS, LIV, CHE, RMA, BAR, BAY, BVB, INT, PSG],
                       strong=[VIL, SEV, ATA, LIL]),
        "eurocups": {}, "locales": {"turkey": {"league_id": 600, "own_clubs": [GALA]}},
        "rating": {}, "top_bonus": {"top_pair": 60, "top": 30},
    }

    def setUp(self):
        super().setUp()
        p = patch.object(match_report, "PRIORITIES", {**PRIORITIES, **BIG5})
        p.start()
        self.addCleanup(p.stop)

    def day(self):
        return [
            match(EPL, ARS, VIL), match(EPL, EVE, BRE), match(EPL, CHE, 77),
            match(LALIGA, RMA, BAR), match(LALIGA, BET, 85),
            match(BUNDES, BAY, BVB), match(BUNDES, WOB, AUG),
            match(SERIEA, INT, ATA), match(SERIEA, LEC, CAG),
            match(LIGUE1, PSG, LIL), match(LIGUE1, NAN, AUX),
        ]

    def test_top_matches_of_five_leagues_go_first(self):
        ranked = self.rank(self.day())
        self.assertEqual(pairs(ranked[:5]), [
            (RMA, BAR),    # 475 + 60
            (BAY, BVB),    # 470 + 60
            (ARS, VIL),    # 480 + 30
            (INT, ATA),    # 465 + 30
            (PSG, LIL),    # 460 + 30
        ])
        # Остатки турниров — по приоритету, каждый целиком.
        self.assertEqual([m["league"]["id"] for m in ranked[5:]],
                         [EPL, EPL, LALIGA, BUNDES, SERIEA, LIGUE1])

    def test_strongest_match_sets_the_bonus_of_the_whole_top_part(self):
        day = self.day() + [match(EPL, LIV, CHE)]   # топ + топ в АПЛ
        ranked = self.rank(day)
        self.assertEqual(pairs(ranked[:3]), [(LIV, CHE), (ARS, VIL), (RMA, BAR)])  # 540, 540, 535

    def test_without_bonus_order_is_as_before(self):
        without = {**self.status, "top_bonus": {}}
        with patch.object(match_report, "TEAM_STATUS", without):
            ranked = self.rank(self.day())
        self.assertEqual([m["league"]["id"] for m in ranked],
                         [EPL] * 3 + [LALIGA] * 2 + [BUNDES] * 2 + [SERIEA] * 2 + [LIGUE1] * 2)
        self.assertEqual(pairs(ranked[:1]), [(ARS, VIL)])

    def test_bad_numbers_count_as_zero(self):
        broken = {**self.status, "top_bonus": {"top_pair": -100, "top": True}}
        with patch.object(match_report, "TEAM_STATUS", broken):
            ranked = self.rank(self.day())
        self.assertEqual([m["league"]["id"] for m in ranked[:3]], [EPL] * 3)

    def test_equal_sum_top_part_above_ordinary_tournament(self):
        # Топ-часть Ла Лиги 475 + 30 = 505 — выше кубка с тем же приоритетом.
        with patch.object(match_report, "PRIORITIES", {**PRIORITIES, **BIG5, CUP: 505}):
            ranked = self.rank([match(CUP, 60, 61), match(LALIGA, RMA, SEV), match(LALIGA, BET, 85)])
        self.assertEqual(pairs(ranked), [(RMA, SEV), (60, 61), (BET, 85)])

    def test_lifted_top_part_is_its_own_widget_block(self):
        filler = [match(200 + n, 300 + 2 * n, 301 + 2 * n) for n in range(13)]
        prios = {**PRIORITIES, **BIG5, **{200 + n: 520 for n in range(13)}}
        day = [match(LALIGA, RMA, BAR)] + [match(LALIGA, 400 + n, 450 + n) for n in range(10)] + filler
        with patch.object(match_report, "PRIORITIES", prios):
            ranked = match_report.rank_matches(day, {}, "global")
            rest, events = match_report.split_widgets(ranked)
        self.assertEqual(pairs(events[:1]), [(RMA, BAR)])            # 535 — выше всех
        self.assertTrue(all(m["league"]["id"] != LALIGA for m in events[1:]))
        self.assertEqual({m["league"]["id"] for m in rest}, {LALIGA})  # остаток — целиком ниже

    def test_top_part_next_to_its_rest_stays_one_block(self):
        # Топ-часть поднялась, но соседей между ней и остатком нет — турнир целый.
        ranked = self.rank([match(LALIGA, RMA, BAR), match(LALIGA, BET, 85), match(CUP, 60, 61)])
        self.assertEqual([m["league"]["id"] for m in ranked], [LALIGA, LALIGA, CUP])

    def test_own_club_gets_the_big_bonus(self):
        with patch.object(match_report, "PRIORITIES", {**PRIORITIES, **BIG5, EUROCUP: 470}):
            ranked = self.rank([match(EUROCUP, GALA, LYON), match(EUROCUP, 60, 61),
                                match(EPL, ARS, VIL), match(EPL, EVE, BRE)], "turkey")
        self.assertEqual(pairs(ranked[:2]), [(GALA, LYON), (ARS, VIL)])  # 530 > 510

    def test_file_marks_are_unchanged(self):
        record = match_report.locale_record(match(LALIGA, RMA, BAR), "global")
        self.assertEqual((record["top"], record["top_reason"]), (True, "pair"))


class TopMark(StatusCase):
    def mark(self, m, locale="global"):
        return match_report.top_mark(m, locale)

    def test_both_at_least_strong_is_top(self):
        for home, away in ((SPAIN, GERMANY), (SPAIN, CROATIA), (CROATIA, CZECHIA)):
            self.assertEqual(self.mark(match(LEAGUE, home, away)), "pair", (home, away))

    def test_top_with_regular_is_not(self):
        self.assertIsNone(self.mark(match(LEAGUE, SPAIN, ANDORRA)))
        self.assertIsNone(self.mark(match(LEAGUE, FAROE, ANDORRA)))

    def test_own_club_in_international_club_tournament_is_top_against_anyone(self):
        self.assertEqual(self.mark(match(EUROCUP, GALA, LYON), "turkey"), "own_club")
        self.assertEqual(self.mark(match(EUROCUP, LYON, GALA), "turkey"), "own_club")

    def test_own_club_is_own_only_for_its_locale(self):
        self.assertIsNone(self.mark(match(EUROCUP, GALA, LYON), "brazil"))
        self.assertIsNone(self.mark(match(EUROCUP, GALA, LYON), "global"))

    def test_own_club_rule_is_not_for_league_or_domestic_cups(self):
        self.assertIsNone(self.mark(match(LEAGUE, GALA, LYON), "turkey"))
        self.assertIsNone(self.mark(match(CUP, GALA, LYON), "turkey"))

    def test_own_club_beats_pair_as_reason_and_stands_first(self):
        top_pair = match(EUROCUP, AJAX, PSV)          # ajax — «топ», psv — обычная
        strong = match(EUROCUP, SPAIN, GERMANY)       # топ + топ
        own = match(EUROCUP, GALA, LYON)
        ranked = self.rank([strong, own, top_pair], "turkey")
        self.assertEqual(pairs(ranked)[0], (GALA, LYON))
        self.assertEqual(self.mark(own, "turkey"), "own_club")
        self.assertEqual(self.mark(strong, "turkey"), "pair")

    def test_several_own_club_matches_go_by_the_general_rule(self):
        own_status = {**STATUS, "eurocups": {str(EUROCUP): {**STATUS["eurocups"][str(EUROCUP)],
                      "15": {"name": "Own strong", "status": "strong", "by": "manual"}}},
                      "locales": {"turkey": {"league_id": 600, "own_clubs": [GALA, 15]}}}
        with patch.object(match_report, "TEAM_STATUS", own_status):
            ranked = self.rank([match(EUROCUP, GALA, LYON), match(EUROCUP, 15, SPAIN)], "turkey")
        self.assertEqual(pairs(ranked), [(15, SPAIN), (GALA, LYON)])

    def test_unknown_team_is_regular(self):
        self.assertEqual(match_report.team_tier(9999), "regular")
        self.assertEqual(match_report.team_tier(SPAIN), "top")


SHAKHTAR, DYNAMO, REAL, INTER, NAPOLI, ROMA = 21, 22, 23, 24, 25, 26


class HomeAndEurocup(StatusCase):
    """Статус клуба дома — в турнирах его страны, в еврокубке — список этого
    турнира: «Шахтёр» — топ в чемпионате Украины и обычный в Лиге чемпионов,
    «Рома» — топ в Лиге Европы."""
    status = {
        "teams": {str(t): {"name": n, "status": "top", "by": "manual"}
                  for t, n in ((SHAKHTAR, "Shakhtar"), (DYNAMO, "Dynamo"), (REAL, "Real"),
                               (INTER, "Inter"), (NAPOLI, "Napoli"))},
        "eurocups": {
            str(EUROCUP): {str(t): {"name": n, "status": "top", "by": "manual"}
                           for t, n in ((REAL, "Real"), (INTER, "Inter"))},
            str(EUROPA): {str(ROMA): {"name": "Roma", "status": "top", "by": "manual"},
                          str(NAPOLI): {"name": "Napoli", "status": "strong", "by": "manual"}},
        },
        "locales": {}, "rating": {"top": 20, "strong": 50},
    }

    def test_home_status_in_own_league_and_cups(self):
        self.assertEqual(match_report.top_mark(match(LEAGUE, SHAKHTAR, DYNAMO), "global"), "pair")
        self.assertEqual(match_report.top_mark(match(CUP, SHAKHTAR, DYNAMO), "global"), "pair")

    def test_eurocup_list_in_international_club_tournament(self):
        self.assertIsNone(match_report.top_mark(match(EUROCUP, SHAKHTAR, REAL), "global"))
        self.assertEqual(match_report.top_mark(match(EUROCUP, INTER, REAL), "global"), "pair")

    def test_top_at_home_is_not_top_in_eurocups_by_default(self):
        # Наполи дома топ, в еврокубковом списке его нет — «обычная».
        self.assertEqual(match_report.team_tier(NAPOLI, LEAGUE), "top")
        self.assertEqual(match_report.team_tier(NAPOLI, EUROCUP), "regular")
        self.assertIsNone(match_report.top_mark(match(EUROCUP, NAPOLI, REAL), "global"))

    def test_each_eurocup_has_its_own_list(self):
        self.assertEqual(match_report.team_tier(ROMA, EUROPA), "top")
        self.assertEqual(match_report.team_tier(ROMA, EUROCUP), "regular")
        self.assertEqual(match_report.team_tier(INTER, EUROPA), "regular")
        self.assertEqual(match_report.top_mark(match(EUROPA, ROMA, NAPOLI), "global"), "pair")

    def test_order_inside_eurocup_follows_eurocup_list(self):
        ranked = self.rank([match(EUROCUP, SHAKHTAR, DYNAMO, hour=13),
                            match(EUROCUP, SHAKHTAR, REAL, hour=16),
                            match(EUROCUP, INTER, REAL, hour=19)])
        self.assertEqual(pairs(ranked), [(INTER, REAL), (SHAKHTAR, REAL), (SHAKHTAR, DYNAMO)])

    def test_file_statuses_are_per_tournament(self):
        record = match_report.locale_record(match(EUROCUP, SHAKHTAR, REAL), "global")
        self.assertEqual((record["home_tier"], record["away_tier"]), ("regular", "top"))
        record = match_report.locale_record(match(LEAGUE, SHAKHTAR, DYNAMO), "global")
        self.assertEqual((record["home_tier"], record["away_tier"]), ("top", "top"))

    def test_old_file_without_eurocups_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "team_status.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"teams": {"1": {"name": "Spain", "status": "top", "by": "rating"}}}, f)
            self.assertEqual(match_report.load_team_status(path)["eurocups"], {})

    def test_only_eurocup_list_is_enough_to_mark(self):
        only = {"teams": {}, "eurocups": {str(EUROCUP): {str(REAL): {"name": "Real", "status": "top",
                                                                   "by": "manual"}}},
                "locales": {}, "rating": {}}
        with patch.object(match_report, "TEAM_STATUS", only):
            record = match_report.locale_record(match(EUROCUP, REAL, INTER), "global")
        self.assertEqual(record["home_tier"], "top")


class EmptyStatusFile(StatusCase):
    status = {"teams": {}, "locales": {}, "rating": {"top": 20, "strong": 50}}

    def test_order_is_by_time_and_nothing_is_marked(self):
        ranked = self.rank([match(LEAGUE, 50, 51, hour=19), match(LEAGUE, SPAIN, GERMANY, hour=16),
                            match(LEAGUE, 52, 53, hour=13)])
        self.assertEqual(pairs(ranked), [(52, 53), (SPAIN, GERMANY), (50, 51)])
        self.assertIsNone(match_report.top_mark(ranked[0], "global"))

    def test_records_are_as_before(self):
        record = match_report.locale_record(match(LEAGUE, SPAIN, GERMANY), "global")
        self.assertEqual(record, match_report.match_to_dict(match(LEAGUE, SPAIN, GERMANY)))

    def test_missing_file_is_empty(self):
        self.assertEqual(match_report.load_team_status("нет/такого/файла.json")["teams"], {})


class RecordsInFile(StatusCase):
    def test_top_record_carries_mark_and_statuses(self):
        record = match_report.locale_record(match(LEAGUE, CROATIA, SPAIN), "global")
        self.assertEqual((record["top"], record["top_reason"], record["home_tier"],
                          record["away_tier"]), (True, "pair", "strong", "top"))

    def test_other_record_has_statuses_and_no_mark(self):
        record = match_report.locale_record(match(LEAGUE, FAROE, ANDORRA), "global")
        self.assertNotIn("top", record)
        self.assertEqual((record["home_tier"], record["away_tier"]), ("regular", "regular"))

    def test_own_club_reason_per_locale(self):
        m = match(EUROCUP, GALA, LYON)
        self.assertEqual(match_report.locale_record(m, "turkey")["top_reason"], "own_club")
        self.assertNotIn("top", match_report.locale_record(m, "brazil"))


class ListsFile(StatusCase):
    def test_every_section_is_marked_and_ordered(self):
        pool = [match(LEAGUE, FAROE, ANDORRA, hour=12), match(LEAGUE, SPAIN, GERMANY, hour=18)]
        national = {**match(LEAGUE, GALA, SPAIN), "homeTeam": {"id": GALA, "name": "Turkey"}}
        with tempfile.TemporaryDirectory() as out, \
             patch.object(match_report, "LOCALE_NATIONS", {"turkey": ["Turkey"]}):
            cfg = {"label": "Turkey", "overrides": {}}
            match_report.write_locale_lists(out, "turkey", cfg, "2026-09-26",
                                            pool + [national], {})
            with open(os.path.join(out, "turkey_2026-09-26.json"), encoding="utf-8") as f:
                data = json.load(f)
        rows = data["top_events"] + data["top_matches"]
        self.assertEqual([(r["home_team_id"], r["away_team_id"]) for r in rows],
                         [(SPAIN, GERMANY), (GALA, SPAIN), (FAROE, ANDORRA)])
        spain = next(r for r in rows if r["home_team_id"] == SPAIN)
        self.assertTrue(spain["top"])
        for section in ("top_events", "top_matches", "reserve", "national"):
            for r in data[section]:
                self.assertIn("home_tier", r, section)
        self.assertEqual(data["national"][0]["home_tier"], "regular")


class TelegramFollowsTheOrder(StatusCase):
    def test_message_lists_matches_in_new_order(self):
        ranked = self.rank([match(LEAGUE, FAROE, ANDORRA), match(LEAGUE, SPAIN, GERMANY)])
        text = match_report.format_list(ranked, __import__("datetime").date(2026, 9, 25))
        self.assertLess(text.index(f"T{SPAIN} — T{GERMANY}"), text.index(f"T{FAROE} — T{ANDORRA}"))
        self.assertNotIn("top", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
