import datetime as dt
import unittest
from unittest.mock import patch

import cli


class WordTests(unittest.TestCase):
    def p(self, text):
        with patch.object(cli, "user_presets", return_value={"nook": {"rgb": [255, 135, 0], "kelvin": 0, "bright": 35}}):
            return cli.parse(text.split())

    def test_nothing_means_status(self):
        self.assertEqual(self.p(""), {"cmd": "status", "brief": True})

    def test_looks_in_any_order(self):
        self.assertEqual(self.p("ember"), {"cmd": "look", "rgb": [255, 42, 0], "bright": 20})
        self.assertEqual(self.p("ember 10"), {"cmd": "look", "rgb": [255, 42, 0], "bright": 10})
        self.assertEqual(self.p("10 ember"), {"cmd": "look", "rgb": [255, 42, 0], "bright": 10})
        self.assertEqual(self.p("2000k 25"), {"cmd": "look", "kelvin": 2000, "bright": 25})
        self.assertEqual(self.p("tungsten"), {"cmd": "look", "kelvin": 2700, "bright": 70})
        self.assertEqual(self.p("#FF8800"), {"cmd": "look", "rgb": [255, 136, 0]})
        self.assertEqual(self.p("ff8800"), {"cmd": "look", "rgb": [255, 136, 0]})
        self.assertEqual(self.p("nook"), {"cmd": "look", "rgb": [255, 135, 0], "bright": 35})

    def test_numbers(self):
        self.assertEqual(self.p("40"), {"cmd": "look", "bright": 40})
        self.assertEqual(self.p("40%"), {"cmd": "look", "bright": 40})
        self.assertEqual(self.p("0"), {"cmd": "look", "power": "off"})
        self.assertEqual(self.p("2200"), {"cmd": "look", "kelvin": 2200})

    def test_older_spellings_still_work(self):
        self.assertEqual(self.p("bright 40"), {"cmd": "look", "bright": 40})
        self.assertEqual(self.p("color #ff8800"), {"cmd": "look", "rgb": [255, 136, 0]})
        self.assertEqual(self.p("kelvin 2000"), {"cmd": "look", "kelvin": 2000})
        self.assertEqual(self.p("on"), {"cmd": "look", "power": "on"})
        self.assertEqual(self.p("off"), {"cmd": "look", "power": "off"})

    def test_modes(self):
        self.assertEqual(self.p("candle"), {"cmd": "candle"})
        self.assertEqual(self.p("candle 20 for 45m"), {"cmd": "candle", "bright": 20, "seconds": 2700.0})
        self.assertEqual(self.p("candle calm"), {"cmd": "candle", "depth": 0.35, "calm": 0.9})
        self.assertEqual(self.p("candle ember")["rgb"], [255, 42, 0])
        self.assertEqual(self.p("sunset"), {"cmd": "sunset", "seconds": 1800})
        self.assertEqual(self.p("sunset 45"), {"cmd": "sunset", "seconds": 2700.0})
        self.assertEqual(self.p("breathe 10"), {"cmd": "breathe", "seconds": 600.0})
        self.assertEqual(self.p("sunrise 30 in 8h"), {"cmd": "sunrise", "seconds": 1800.0, "wait": 28800.0})
        self.assertEqual(self.p("scene fire 40"), {"cmd": "scene", "name": "fire", "bright": 40})
        self.assertEqual(self.p("off in 30m"), {"cmd": "timer", "seconds": 1800.0})
        self.assertEqual(self.p("pulse fail")["rgb"], [255, 16, 0])
        self.assertEqual(self.p("ok")["cmd"], "pulse")

    def test_mistakes_get_a_sentence_and_send_nothing(self):
        for text in ("embr", "9000k", "off amber", "candle x", "save candle", "save Bad_Name", "scene nope",
                     "sunset 1s", "up 3", "off in 2s", "sunrise 20 at 25:00", "101", "pulse purple", "bright"):
            with self.assertRaises(cli.Usage, msg=text):
                self.p(text)

    def test_unknown_word_suggests_a_near_one(self):
        with self.assertRaisesRegex(cli.Usage, "Did you mean: ember"):
            self.p("embr")


class TimeTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(cli.as_seconds("30m"), 1800)
        self.assertEqual(cli.as_seconds("45min"), 2700)
        self.assertEqual(cli.as_seconds("1h30m"), 5400)
        self.assertEqual(cli.as_seconds("90s"), 90)
        self.assertIsNone(cli.as_seconds("30"))
        self.assertEqual(cli.as_seconds("30", bare_minutes=True), 1800)
        self.assertIsNone(cli.as_seconds("soon"))

    def test_clock_times_mean_the_next_one(self):
        now = dt.datetime(2026, 11, 14, 22, 0, 0)
        self.assertEqual(cli.until("23:30", now), 5400)
        self.assertEqual(cli.until("7:00", now), 9 * 3600)
        self.assertEqual(cli.until("7am", now), 9 * 3600)
        self.assertEqual(cli.until("11:30pm", now), 5400)
        self.assertEqual(cli.until("22:00", now), 24 * 3600)
        for bad in ("25:00", "7:99", "13pm", "noon"):
            with self.assertRaises(cli.Usage):
                cli.until(bad, now)


if __name__ == "__main__":
    unittest.main()
