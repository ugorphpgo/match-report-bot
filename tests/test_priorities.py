"""Приоритеты в одной таблице: общий приоритет турнира и переопределения локали.

Проверяем отбор через его настоящие функции — score_match, local_leagues,
build_locale_message, get_category — на подменённой таблице приоритетов:
числа в тесте свои, а не из config/, иначе любая правка весов красила бы тест.

    python tests/test_priorities.py
"""
import os
import sys
import unittest
from datetime import date
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import match_report  # noqa: E402

CL, EPL, LOCAL_CUP, UNKNOWN, WORLD_CUP = 10, 20, 30, 99, 40

PRIORITIES = {CL: 1000, EPL: 480, WORLD_CUP: 2000}
GROUPS = {CL: "international_clubs", WORLD_CUP: "national_teams"}


def match(league_id, day="2026-09-25", hour_utc=15):
    return {
        "league": {"id": league_id, "name": f"L{league_id}"},
        "homeTeam": {"id": 1, "name": "A"},
        "awayTeam": {"id": 2, "name": "B"},
        "country": {"name": "England"},
        "date": f"{day}T{hour_utc:02d}:00:00Z",
    }


class Priorities(unittest.TestCase):
    def setUp(self):
        for name, value in (("PRIORITIES", PRIORITIES), ("GROUPS", GROUPS),
                            ("DEFAULT_PRIORITY", 1)):
            p = patch.object(match_report, name, value)
            p.start()
            self.addCleanup(p.stop)

    # --- score_match: переопределение > общий > по умолчанию ---

    def test_global_priority_without_override(self):
        self.assertEqual(match_report.score_match(match(EPL), {}), 480)

    def test_override_beats_global(self):
        self.assertEqual(match_report.score_match(match(EPL), {EPL: 520}), 520)

    def test_override_can_lower(self):
        self.assertEqual(match_report.score_match(match(CL), {CL: 100}), 100)

    def test_unknown_league_gets_default(self):
        self.assertEqual(match_report.score_match(match(UNKNOWN), {}), 1)

    def test_ranking_follows_effective_priority(self):
        ranked = match_report.rank_matches([match(CL), match(EPL), match(LOCAL_CUP)],
                                           {LOCAL_CUP: 1500, CL: 10})
        self.assertEqual([m["league"]["id"] for m in ranked], [LOCAL_CUP, EPL, CL])

    # --- «местные» в Telegram: только переопределения вверх ---

    def test_local_leagues_are_overrides_above_global(self):
        cfg = {"label": "X", "overrides": {LOCAL_CUP: 500,   # не было вовсе
                                           EPL: 520,         # поднят
                                           CL: 100}}         # опущен
        self.assertEqual(match_report.local_leagues(cfg), {LOCAL_CUP, EPL})

    def test_local_message_lists_raised_not_lowered(self):
        cfg = {"label": "Иран", "overrides": {EPL: 520, CL: 100}}
        text = match_report.build_locale_message(
            "iran", cfg, [match(EPL), match(CL)], date(2026, 9, 25), "25.09.2026")
        self.assertIn("L20", text)
        self.assertNotIn("L10", text)

    def test_global_is_global_even_with_overrides(self):
        # Раньше global узнавали по пустому списку местных лиг. Переопределение
        # в global не должно превращать общий топ в «местные матчи».
        cfg = {"label": "Global", "overrides": {EPL: 520}}
        text = match_report.build_locale_message(
            "global", cfg, [match(EPL), match(CL)], date(2026, 9, 25), "25.09.2026")
        self.assertIn("Топ ивенты", text)

    # --- подпись категории в Telegram ---

    def test_category_from_group(self):
        self.assertEqual(match_report.get_category(match(WORLD_CUP)), "International")
        self.assertEqual(match_report.get_category(match(CL)), "International Clubs")
        self.assertEqual(match_report.get_category(match(EPL)), "England")


class ResetEntry(unittest.TestCase):
    """Турнир, сброшенный во вкладке к умолчанию: запись без числа, но с group."""

    def test_entry_without_priority_is_default_but_keeps_group(self):
        priorities, groups = match_report.priority_tables({
            "40": {"name": "World Cup", "group": "national_teams"},
            "20": {"name": "Premier League", "priority": 480},
        })
        self.assertEqual(priorities, {20: 480})
        self.assertEqual(groups[40], "national_teams")


class ConfigShape(unittest.TestCase):
    """Настоящий config/league_weights.json читается и непротиворечив."""

    def test_every_override_names_a_locale_and_numbers_are_ints(self):
        self.assertIn("global", match_report.LOCALE_CONFIG)
        for key, cfg in match_report.LOCALE_CONFIG.items():
            for league_id, priority in cfg["overrides"].items():
                self.assertIsInstance(league_id, int, key)
                self.assertIsInstance(priority, int, key)
        for league_id, priority in match_report.PRIORITIES.items():
            self.assertIsInstance(priority, int, league_id)


if __name__ == "__main__":
    unittest.main()
