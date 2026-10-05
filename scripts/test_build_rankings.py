"""Tests for the pure parts of build_rankings.py. Run: python -m unittest scripts/test_build_rankings.py
(pytest also collects them). No network."""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_rankings as br  # noqa: E402


def layer(date, built, unusable=0.0):
    return {"date": date, "unusable_share": unusable,
            "stats": {"groups": [{"key": "agriculture", "share": 50.0}, {"key": "built", "share": built}]}}


def compare(built_before=20.0, built_after=30.0, unusable=(0.0, 0.0), gap=3):
    return {"before": layer("2016-07-24", built_before, unusable[0]),
            "after": layer("2025-07-20", built_after, unusable[1]),
            "built_km2": 12.5, "season_gap_days": gap}


CITY = {"name": "Testkent", "country": "TR", "lat": "39.9", "lon": "32.8", "experimental": "0"}


class Summary(unittest.TestCase):
    def test_fields(self):
        row = br.summarize(CITY, compare())
        self.assertEqual(row["built_change_pts"], 10.0)
        self.assertEqual(row["built_km2"], 12.5)
        self.assertEqual(row["unusable_share"], [0.0, 0.0])
        self.assertEqual((row["before_date"], row["after_date"]), ("2016-07-24", "2025-07-20"))
        self.assertFalse(row["experimental"])

    def test_rounding(self):
        self.assertEqual(br.summarize(CITY, compare(29.3, 28.3))["built_change_pts"], -1.0)


class Exclusion(unittest.TestCase):
    def test_limits_are_inclusive(self):
        self.assertIsNone(br.exclusion(br.summarize(CITY, compare(unusable=(5.0, 5.0), gap=45))))

    def test_cloudy_either_date(self):
        self.assertEqual(br.exclusion(br.summarize(CITY, compare(unusable=(0.0, 5.1)))), "unusable")
        self.assertEqual(br.exclusion(br.summarize(CITY, compare(unusable=(7.0, 0.0)))), "unusable")

    def test_season_gap(self):
        self.assertEqual(br.exclusion(br.summarize(CITY, compare(gap=46))), "season")


class Split(unittest.TestCase):
    def test_sorted_and_separated(self):
        a = br.summarize(CITY | {"name": "A"}, compare(20, 25))
        b = br.summarize(CITY | {"name": "B"}, compare(20, 35))
        c = br.summarize(CITY | {"name": "C"}, compare(20, 40, unusable=(9, 0)))
        err = {"name": "D", "country": "TR", "experimental": False, "error": "no_imagery"}
        ranked, unavailable = br.split_results([a, b, c, err])
        self.assertEqual([r["name"] for r in ranked], ["B", "A"])
        self.assertEqual([(r["name"], r["reason"]) for r in unavailable], [("C", "unusable"), ("D", "no_imagery")])


class LastSummer(unittest.TestCase):
    def test_rule(self):
        self.assertEqual(br.last_summer(dt.date(2026, 7, 31)), 2025)
        self.assertEqual(br.last_summer(dt.date(2026, 8, 1)), 2026)


if __name__ == "__main__":
    unittest.main()
