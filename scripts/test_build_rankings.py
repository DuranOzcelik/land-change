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


CITY = {"name": "Testkent", "country": "TR", "continent": "europe", "lat": "39.9", "lon": "32.8", "experimental": "0"}


class Summary(unittest.TestCase):
    def test_fields(self):
        row = br.summarize(CITY, compare())
        self.assertEqual(row["built_change_pts"], 10.0)
        self.assertEqual(row["built_km2"], 12.5)
        self.assertEqual(row["unusable_share"], [0.0, 0.0])
        self.assertEqual((row["before_date"], row["after_date"]), ("2016-07-24", "2025-07-20"))
        self.assertFalse(row["experimental"])
        self.assertEqual(row["continent"], "europe")

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
        # Cities over a limit stay in the table, marked; only missing images make a city unavailable.
        self.assertEqual([(r["name"], r.get("warning")) for r in ranked], [("C", "unusable"), ("B", None), ("A", None)])
        self.assertEqual([(r["name"], r["reason"]) for r in unavailable], [("D", "no_imagery")])


class LastSummer(unittest.TestCase):
    def test_rule(self):
        self.assertEqual(br.last_summer(dt.date(2026, 7, 31)), 2025)
        self.assertEqual(br.last_summer(dt.date(2026, 8, 1)), 2026)


class DatePick(unittest.TestCase):
    def fake(self, data):
        import datepick
        original = datepick.available
        datepick.available = lambda api, lat, lon, year: [dict(d) for d in data.get(str(year), [])]
        self.addCleanup(setattr, datepick, "available", original)
        return datepick

    def test_clear_dates_near_the_defaults(self):
        dp = self.fake({
            "2016": [{"date": "2016-05-15", "cloud": 0.0}, {"date": "2016-08-03", "cloud": 30.0}, {"date": "2016-08-20", "cloud": 4.0}],
            "2026": [{"date": "2026-06-07", "cloud": 0.0}, {"date": "2026-07-12", "cloud": 1.0}],
        })
        # The clear May image is 80 days from the default 2016-08-03, so it is never used, and
        # 2026-06-07 is 74 days from the only usable 2016 image, so it cannot pair with it.
        self.assertEqual(dp.pairs("api", 0, 0, "2016-08-03", "2026-07-12"), [("2016-08-20", "2026-07-12")])

    def test_southern_summer_may_start_the_year_before(self):
        dp = self.fake({
            "2015": [{"date": "2015-12-22", "cloud": 2.0}],
            "2016": [{"date": "2016-01-15", "cloud": 9.0}, {"date": "2016-12-30", "cloud": 0.0}],
            "2025": [{"date": "2025-01-20", "cloud": 0.0}],
        })
        anchor = dp.mid_summer("2016", -15)
        self.assertEqual(anchor, "2016-01-20")
        # 2016-12-30 is the clearest 2016 date but belongs to the next summer.
        self.assertEqual(dp.pairs("api", -15, 0, anchor, dp.mid_summer("2025", -15)), [("2015-12-22", "2025-01-20")])

    def test_any_season_keeps_both_images_in_one_season(self):
        dp = self.fake({
            "2016": [{"date": "2016-03-10", "cloud": 1.0}, {"date": "2016-07-20", "cloud": 40.0}],
            "2017": [{"date": "2017-11-02", "cloud": 0.0}, {"date": "2017-02-01", "cloud": 9.0}],
            "2025": [{"date": "2025-03-20", "cloud": 2.0}, {"date": "2025-07-15", "cloud": 50.0}],
            "2026": [{"date": "2026-08-30", "cloud": 0.0}, {"date": "2027-01-01", "cloud": 0.0}],
        })
        got = dp.any_season_pairs("api", 0, 0, [2016, 2017], [2025, 2026], limit=2)
        # March pair first (clearest pair in one season); the clear November 2017 image has no
        # partner within 45 days, and 2027 dates are outside the years asked for.
        self.assertEqual(got, [("2016-03-10", "2025-03-20"), ("2016-07-20", "2026-08-30")])
        for b, a in got:
            self.assertLessEqual(dp.season_gap(b, a), dp.MAX_SEASON_GAP)

    def test_gap_wraps_around_new_year(self):
        import datepick
        self.assertEqual(datepick.season_gap("2016-12-30", "2026-01-02"), 3)


if __name__ == "__main__":
    unittest.main()
