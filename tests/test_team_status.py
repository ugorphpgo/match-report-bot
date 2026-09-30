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

LEAGUE, CUP, EUROCUP = 10, 20, 30
PRIORITIES = {LEAGUE: 500, CUP: 400, EUROCUP: 900}
GROUPS = {EUROCUP: "international_clubs"}

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
        own_status = {**STATUS, "teams": {**STATUS["teams"],
                      "15": {"name": "Own strong", "status": "strong", "by": "manual"}},
                      "locales": {"turkey": {"league_id": 600, "own_clubs": [GALA, 15]}}}
        with patch.object(match_report, "TEAM_STATUS", own_status):
            ranked = self.rank([match(EUROCUP, GALA, LYON), match(EUROCUP, 15, SPAIN)], "turkey")
        self.assertEqual(pairs(ranked), [(15, SPAIN), (GALA, LYON)])

    def test_unknown_team_is_regular(self):
        self.assertEqual(match_report.team_tier(9999), "regular")
        self.assertEqual(match_report.team_tier(SPAIN), "top")


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
