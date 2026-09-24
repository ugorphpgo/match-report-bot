"""Режим --lists-only: добыча списков для coupon-filler без ежедневного отчёта.

Проверяем поведение через main(argv): какие файлы появились в --out, ушло ли
что-нибудь в Telegram и тронута ли data/ самого бота. Highlightly подменён —
тест не тратит квоту и работает без ключа.

    python tests/test_lists_only.py
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

LOCALES = sorted(match_report.LOCALE_CONFIG)


def match(day, hour_utc, league_id=39, home=1, away=2):
    return {
        "league": {"id": league_id, "name": f"League {league_id}"},
        "homeTeam": {"id": home, "name": f"Team {home}"},
        "awayTeam": {"id": away, "name": f"Team {away}"},
        "date": f"{day}T{hour_utc:02d}:00:00Z",
    }


class ListsOnly(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.days = {}          # что «вернёт Highlightly» на дату
        self.telegram = []
        self.cleaned = []
        self.patches = [
            patch.object(match_report, "fetch_matches",
                         lambda day: self.days.get(day, [])),
            patch.object(match_report, "send_telegram", self.telegram.append),
            patch.object(match_report, "cleanup_old_data", self.cleaned.append),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_main(self, *argv):
        with self.assertRaises(SystemExit) as caught:
            match_report.main(["--lists-only", "--out", self.out, *argv])
        return caught.exception.code or 0

    def written(self):
        return sorted(os.listdir(self.out))

    def test_writes_every_locale_for_the_date_and_nothing_else(self):
        self.days["2026-09-25"] = [match("2026-09-25", 15)]

        code = self.run_main("--start", "2026-09-25")

        self.assertEqual(code, 0)
        self.assertEqual(self.written(),
                         sorted(f"{loc}_2026-09-25.json" for loc in LOCALES))
        self.assertEqual(self.telegram, [])
        self.assertEqual(self.cleaned, [])

    def test_own_national_team_published_even_beyond_the_cut(self):
        # Матч своей сборной локали coupon-filler ставит первым пресетом Top
        # Events, где бы он ни оказался в ранжировании: поэтому он в файле
        # всегда, отдельным полем. 40 одиночных турниров идут раньше него
        # (равный приоритет, id лиги меньше) — в список и запас он не влезает.
        brazil = match("2026-09-25", 19, league_id=9999, home=500, away=501)
        brazil["homeTeam"]["name"] = "Brazil"
        youth = match("2026-09-25", 16, league_id=9998, home=502, away=503)
        youth["awayTeam"]["name"] = "Brazil U20"
        self.days["2026-09-25"] = [match("2026-09-25", 15, league_id=n, home=2 * n,
                                         away=2 * n + 1) for n in range(1000, 1040)]
        self.days["2026-09-25"] += [brazil, youth]

        self.run_main("--start", "2026-09-25")

        def national(locale):
            with open(os.path.join(self.out, f"{locale}_2026-09-25.json"), encoding="utf-8") as f:
                data = json.load(f)
            listed = data["top_events"] + data["top_matches"] + data["reserve"]
            return [m["home_team_name"] for m in data["national"]], listed

        names, listed = national("brazil")
        self.assertEqual(names, ["Brazil"])
        self.assertNotIn(9999, {m["league_id"] for m in listed}, "он и правда за обрезом")
        self.assertEqual(national("global")[0], [], "у Global своей сборной нет")
        self.assertEqual(national("mexico")[0], [])

    def test_file_carries_reserve_and_widget_targets(self):
        # Запас и цели — для замены в coupon-filler матчей, которых нет в
        # админке: там список с запасом делится на виджеты заново.
        self.days["2026-09-25"] = [match("2026-09-25", 15, league_id=n, home=2 * n,
                                         away=2 * n + 1) for n in range(1000, 1040)]

        self.run_main("--start", "2026-09-25")

        with open(os.path.join(self.out, "global_2026-09-25.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["widget_targets"],
                         {"top_events": match_report.TOP_EVENTS_SIZE,
                          "top_matches": match_report.TOP_MATCHES_SIZE})
        listed = {m["league_id"] for m in data["top_events"] + data["top_matches"]}
        self.assertTrue(data["reserve"], "сорок одиночных турниров — запасу есть откуда взяться")
        self.assertFalse(listed & {m["league_id"] for m in data["reserve"]},
                         "запас не повторяет список")

    def test_range_covers_every_day_inclusive(self):
        for day in ("2026-09-25", "2026-09-26", "2026-09-27"):
            self.days[day] = [match(day, 15)]

        code = self.run_main("--start", "2026-09-25", "--end", "2026-09-27")

        self.assertEqual(code, 0)
        self.assertEqual({name.rsplit("_", 1)[1] for name in self.written()},
                         {"2026-09-25.json", "2026-09-26.json", "2026-09-27.json"})

    def test_empty_day_writes_nothing_and_says_so_by_exit_code(self):
        # 26-го матчей нет, 25-го есть: 25-е пишется, 26-е — нет, и выход
        # отличается от успеха, чтобы вызывающий мог сказать «матчей нет».
        self.days["2026-09-25"] = [match("2026-09-25", 15)]

        code = self.run_main("--start", "2026-09-25", "--end", "2026-09-26")

        self.assertEqual(code, match_report.EXIT_NO_MATCHES)
        self.assertTrue(all("2026-09-25" in name for name in self.written()))
        self.assertEqual(len(self.written()), len(LOCALES))

    def test_only_early_matches_count_as_empty(self):
        # 02:00 UTC = 05:00 по Минску, раньше MIN_START_HOUR.
        self.days["2026-09-25"] = [match("2026-09-25", 2)]

        code = self.run_main("--start", "2026-09-25")

        self.assertEqual(code, match_report.EXIT_NO_MATCHES)
        self.assertEqual(self.written(), [])

    def test_selection_matches_daily_report(self):
        # Одна и та же дата через --lists-only и через ежедневный прогон
        # обязана дать одинаковые файлы: иначе купон разойдётся с чатом.
        tomorrow = (match_report.datetime.now(match_report.LOCAL_TZ)
                    + match_report.timedelta(days=1)).date().isoformat()
        self.days[tomorrow] = [match(tomorrow, 15, league_id=39, home=1, away=2),
                               match(tomorrow, 18, league_id=140, home=3, away=4)]

        self.run_main("--start", tomorrow)
        daily_dir = tempfile.mkdtemp()
        with patch.object(match_report, "DATA_DIR", daily_dir):
            match_report.main([])

        for name in self.written():
            with open(os.path.join(self.out, name), encoding="utf-8") as a, \
                 open(os.path.join(daily_dir, name), encoding="utf-8") as b:
                self.assertEqual(json.load(a), json.load(b), name)
        self.assertEqual(len(self.telegram), 1)   # отчёт ушёл одним сообщением
        self.assertEqual(len(self.cleaned), 1)


if __name__ == "__main__":
    unittest.main()
