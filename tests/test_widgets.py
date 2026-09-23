"""Раскладка списка локали по виджетам: топ ивенты ≈14, топ матчи ≈12.

Размер виджета — цель, а не потолок: турнир между виджетами не делится и на
хвосте не обрезается, граница проходит между турнирами там, где итог ближе к
цели (при равенстве — с турниром), первый турнир виджета берётся всегда.
Проверяем через настоящие rank_matches и split_widgets на своей таблице
приоритетов — числа из config/ тест не красят.

    python tests/test_widgets.py
"""
import os
import sys
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import match_report  # noqa: E402


def tournament(league_id, size):
    return [{"league": {"id": league_id, "name": f"L{league_id}"},
             "homeTeam": {"id": 2 * i, "name": f"H{i}"},
             "awayTeam": {"id": 2 * i + 1, "name": f"A{i}"},
             "country": {"name": "England"},
             "date": "2026-09-25T15:00:00Z"} for i in range(size)]


def day(*sizes):
    """Турниры по убыванию приоритета: первый — сильнейший. id лиги = 1, 2, …"""
    matches, priorities = [], {}
    for n, size in enumerate(sizes, start=1):
        matches += tournament(n, size)
        priorities[n] = 1000 - n
    return matches, priorities


def widgets(matches, priorities, overrides=None):
    with patch.object(match_report, "PRIORITIES", priorities), \
         patch.object(match_report, "EXCLUDED", {}), \
         patch.object(match_report, "DEFAULT_PRIORITY", 1):
        ranked = match_report.rank_matches(matches, overrides or {})
        top_matches, top_events = match_report.split_widgets(ranked)
    return ([m["league"]["id"] for m in top_events],
            [m["league"]["id"] for m in top_matches])


class Widgets(unittest.TestCase):
    def test_single_match_tournaments_fill_targets_exactly(self):
        events, matches = widgets(*day(*[1] * 40))
        self.assertEqual((len(events), len(matches)), (14, 12))
        self.assertEqual(events, list(range(1, 15)), "топ ивенты — сильнейшие")

    def test_tournament_goes_to_the_nearer_boundary(self):
        # 12 одиночных, затем турнир на 5: без него 12 (на 2 от 14), с ним 17
        # (на 3) — он уходит в топ матчи целиком.
        events, matches = widgets(*day(*[1] * 12, 5, *[1] * 20))
        self.assertEqual(len(events), 12)
        self.assertEqual(matches.count(13), 5)

    def test_tie_keeps_tournament_in_the_stronger_widget(self):
        # 12 или 16 — оба на 2 от 14: турнир остаётся в топ ивентах.
        events, _ = widgets(*day(*[1] * 12, 4, *[1] * 20))
        self.assertEqual(len(events), 16)

    def test_first_tournament_taken_however_big(self):
        # «0 ближе к 14, чем 30» оставило бы топ ивенты пустыми.
        events, matches = widgets(*day(30, *[1] * 20))
        self.assertEqual(events, [1] * 30)
        self.assertEqual(len(matches), 12, "у топ матчей своя цель")

    def test_top_matches_have_own_target_after_grown_events(self):
        events, matches = widgets(*day(*[1] * 12, 4, *[1] * 20))
        self.assertEqual((len(events), len(matches)), (16, 12))

    def test_tail_tournament_not_cut(self):
        # Топ ивенты 14 одиночных; дальше 10 одиночных и турнир на 6:
        # 10 (на 2 от 12) против 16 (на 4) — хвост не берётся вовсе, а не
        # обрезается до двух матчей.
        events, matches = widgets(*day(*[1] * 14, *[1] * 10, 6, 1))
        self.assertEqual(len(matches), 10)
        self.assertNotIn(25, matches)

    def test_no_tournament_split_between_widgets(self):
        events, matches = widgets(*day(3, 4, 2, 5, 3, 6, 2, 4, 3, 5, 2))
        self.assertFalse(set(events) & set(matches))

    def test_equal_priority_tournaments_do_not_interleave(self):
        a, b = tournament(1, 3), tournament(2, 3)
        interleaved = [a[0], b[0], a[1], b[1], a[2], b[2]]
        events, _ = widgets(interleaved, {1: 500, 2: 500})
        self.assertEqual(events, [1, 1, 1, 2, 2, 2])

    def test_thin_day_all_in_top_events(self):
        events, matches = widgets(*day(2, 1, 2))
        self.assertEqual((len(events), matches), (5, []))

    def test_split_of_ranked_list_is_stable(self):
        # Телеграм делит уже обрезанный список той же split_widgets: граница
        # обязана совпасть с той, по которой список обрезан.
        matches, priorities = day(*[1] * 12, 4, 3, 5, *[1] * 9, 7, 2)
        with patch.object(match_report, "PRIORITIES", priorities), \
             patch.object(match_report, "EXCLUDED", {}):
            ranked = match_report.rank_matches(matches, {})
            once = match_report.split_widgets(ranked)
            twice = match_report.split_widgets(once[1] + once[0])
        self.assertEqual(once, twice)
        self.assertEqual(once[1] + once[0], ranked, "в виджеты уходит весь список")


if __name__ == "__main__":
    unittest.main()
