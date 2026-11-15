"""The helper's behaviour against a stand-in bulb that acts like the real one:
any setting wakes it, white light is clamped to 2700K and up, and it can be
made to drop the link or disagree."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bleak.exc import BleakError

import agent
import light
import warm


class FakeLamp:
    def __init__(self):
        self.on, self.bright, self.mode = True, 70, bytes((0x0D, 255, 126, 0)).ljust(17, b"\x00")
        self.sent = []  # every frame, in order
        self.fail_next = 0  # raise a link error on the next N exchanges
        self.lie = False  # report a different brightness than was set
        self.dead_after = None  # stop answering after this many frames

    def drain(self):
        pass

    def _take(self, data):
        if self.fail_next:
            self.fail_next -= 1
            raise BleakError("link lost")
        if self.dead_after is not None and len(self.sent) >= self.dead_after:
            raise BleakError("link lost")
        self.sent.append(data)
        if data[0] != 0x33:
            return
        if data[1] == 1:
            self.on = bool(data[2])
        elif data[1] == 4:
            self.bright, self.on = data[2], True
        elif data[1] == 5:
            body = bytes(data[2:19])
            if body[0] == 0x0D and int.from_bytes(body[4:6], "big"):
                k = min(max(int.from_bytes(body[4:6], "big"), 2700), 6500)
                body = body[:4] + k.to_bytes(2, "big")
            self.mode, self.on = body, True

    async def send(self, data):
        self._take(data)

    async def command(self, opcode, *payload, check=None):
        self._take(light.frame(opcode, *payload))
        reply = (await self.status())[opcode]
        if check is not None:
            ok = check(reply)
        elif opcode == 5:
            ok = reply[2:19] == bytes(payload).ljust(17, b"\x00")
        else:
            ok = reply[2] == payload[0]
        if not ok or (self.lie and opcode == 4):
            raise RuntimeError(f"Bulb acknowledged command but state differs: {reply.hex()}")
        return reply

    async def status(self):
        if self.fail_next:
            self.fail_next -= 1
            raise BleakError("link lost")
        return {1: light.frame(1, int(self.on), head=0xAA), 4: light.frame(4, self.bright, head=0xAA),
                5: light.frame(5, *self.mode, head=0xAA)}

    async def kelvin(self, value):
        payload, white = warm.kelvin_payload(value)
        await self.command(5, *payload, check=(lambda r: int.from_bytes(r[6:8], "big") == value) if white else None)
        return white

    async def scene(self, name):
        code, _ = warm.SCENES[name]
        await self.command(5, *warm.scene_select_payload(code))


async def discover_none():
    return {}


class AgentCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(agent, "WORK", root), patch.object(agent, "RESUME", root / "resume.json"),
                        patch.object(agent, "PROBLEM", root / "last-problem.txt"),
                        patch.object(agent, "USER_PRESETS", root / "presets.json"), patch.object(agent, "FPS", 200)]
        for p in self.patches:
            p.start()
        (root / "resume.json").write_text("")
        self.a = agent.Agent()
        self.a._keep_awake = lambda: None  # no caffeinate in tests
        self.fake = FakeLamp()
        self.connects = 0

        async def connect():
            if self.a.lamp is None:
                self.connects += 1
                self.a.lamp, self.a.client = self.fake, SimpleNamespace(is_connected=True)

        async def disconnect():
            self.a.lamp = self.a.client = None

        self.a.connect, self.a.disconnect = connect, disconnect
        self.said = []

    async def asyncTearDown(self):
        await self.a.stop_mode(tidy=False)
        if self.a.timer:
            self.a.timer.cancel()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def do(self, **req):
        return await self.a.dispatch(req, self.said.append)

    async def settled(self, seconds=5):
        """Wait for the running mode to finish by itself."""
        for _ in range(int(seconds * 100)):
            if self.a.mode is None:
                return
            await asyncio.sleep(0.01)
        self.fail("the mode did not finish")

    def shows(self):
        """Sub-mode and colour the stand-in bulb is showing."""
        return bytes(self.fake.mode[:4])

    def colour_frames(self):
        return [f for f in self.fake.sent if f[:3] == bytes((0x33, 5, 0x0D))]


class LookTests(AgentCase):
    async def test_named_look_ends_verified_and_on(self):
        r = await self.do(cmd="look", rgb=[255, 42, 0], bright=20)
        self.assertTrue(r["ok"])
        self.assertEqual((self.fake.on, self.fake.bright, self.shows()), (True, 20, bytes((0x0D, 255, 42, 0))))
        self.assertEqual(r["text"], "on, 20%, #ff2a00")

    async def test_glide_passes_through_colours_in_between(self):
        await self.do(cmd="look", rgb=[255, 0, 0], bright=70)
        reds = {f[4] for f in self.colour_frames()}  # the green byte on the way from 126 to 0
        self.assertTrue(any(0 < g < 126 for g in reds))

    async def test_off_then_on_brings_the_same_look_back(self):
        await self.do(cmd="look", power="off")
        self.assertFalse(self.fake.on)
        r = await self.do(cmd="look", power="on")
        self.assertEqual(r["text"], "on, 70%, #ff7e00 (the 1800K colour mix)")

    async def test_off_works_even_if_the_lamp_was_changed_behind_our_back(self):
        await self.do(cmd="look", power="off")
        self.fake.on = True  # switched on at the wall or in the Govee app
        await self.do(cmd="look", power="off")
        self.assertFalse(self.fake.on)

    async def test_low_kelvin_is_a_colour_and_high_kelvin_is_white(self):
        await self.do(cmd="look", kelvin=2000)
        self.assertEqual(self.shows(), bytes((0x0D, 255, 0x8D, 0x0B)))
        r = await self.do(cmd="look", kelvin=3000)
        self.assertEqual(r["text"], "on, 70%, white 3000K")

    async def test_back_returns_to_the_look_before(self):
        await self.do(cmd="look", rgb=[255, 42, 0], bright=20)
        r = await self.do(cmd="back")
        self.assertEqual(r["text"], "on, 70%, #ff7e00 (the 1800K colour mix)")

    async def test_up_and_down_always_move(self):
        await self.do(cmd="look", bright=1)
        await self.do(cmd="nudge", what="up")
        self.assertEqual(self.fake.bright, 2)
        await self.do(cmd="look", bright=100)
        await self.do(cmd="nudge", what="up")
        self.assertEqual(self.fake.bright, 100)

    async def test_crossing_into_real_white_lowers_brightness_to_match(self):
        await self.do(cmd="look", kelvin=2400, bright=40)
        r = await self.do(cmd="nudge", what="cooler")
        self.assertEqual((self.a.look.kelvin, self.fake.bright), (2700, 8))
        self.assertIn("Brightness went from 40% to 8%", r["text"])

    async def test_save_writes_the_current_look(self):
        await self.do(cmd="look", kelvin=1900, bright=35)
        await self.do(cmd="save", name="nook")
        saved = json.loads(agent.USER_PRESETS.read_text())
        self.assertEqual(saved["nook"], {"rgb": [255, 0x87, 0], "kelvin": 0, "bright": 35})


class PanelTests(AgentCase):
    """What the menu bar panel relies on."""

    async def test_live_drag_follows_at_once_and_the_commit_is_verified(self):
        await self.do(cmd="status")
        before = len(self.fake.sent)
        await self.do(cmd="live", bright=41)
        self.assertEqual(self.fake.bright, 41)
        self.assertEqual(len(self.fake.sent), before + 1)  # one frame, no readback
        self.fake.lie = True  # the commit must notice a bulb that disagrees
        with self.assertRaisesRegex(RuntimeError, "state differs"):
            await self.do(cmd="look", bright=41)

    async def test_live_warmth_crosses_between_colour_mix_and_real_white(self):
        await self.do(cmd="live", kelvin=2000)
        self.assertEqual(self.shows(), bytes((0x0D, 255, 0x8D, 0x0B)))
        await self.do(cmd="live", kelvin=3000)
        self.assertEqual(int.from_bytes(self.fake.mode[4:6], "big"), 3000)
        self.assertEqual(self.a.snapshot()["white"], 3000)

    async def test_live_brightness_leaves_a_candle_running_but_warmth_stops_it(self):
        await self.do(cmd="candle", bright=30)
        await self.do(cmd="live", bright=20)
        self.assertEqual((self.a.mode_name, self.fake.bright), ("candle", 20))
        await self.do(cmd="live", kelvin=2200)
        self.assertIsNone(self.a.mode)

    async def test_snapshot_says_what_is_running_and_when_it_ends(self):
        await self.do(cmd="look", kelvin=1800, bright=70)
        snap = self.a.snapshot()
        self.assertEqual((snap["on"], snap["bright"], snap["mix"], snap["white"], snap["mode"]), (True, 70, 1800, 0, ""))
        await self.do(cmd="candle", seconds=600, feel="lively")
        snap = self.a.snapshot()
        self.assertEqual((snap["mode"], snap["feel"]), ("candle", "lively"))
        self.assertAlmostEqual(snap["mode_ends"] - time.time(), 600, delta=5)
        await self.do(cmd="stop")
        self.assertEqual((self.a.snapshot()["mode"], self.a.snapshot()["mode_ends"]), ("", 0))
        await self.do(cmd="timer", seconds=900)
        self.assertAlmostEqual(self.a.snapshot()["timer"] - time.time(), 900, delta=5)
        await self.do(cmd="stop")
        self.assertEqual(self.a.snapshot()["timer"], 0)

    async def test_candle_can_be_retuned_while_it_runs(self):
        await self.do(cmd="candle")
        r = await self.do(cmd="candle_tune", feel="calm")
        self.assertTrue(r["ok"])
        self.assertEqual((self.a.flame.depth, self.a.flame.calm, self.a.mode_name), (0.35, 0.9, "candle"))
        await self.do(cmd="stop")
        self.assertFalse((await self.do(cmd="candle_tune", feel="calm"))["ok"])

    async def test_looks_lists_built_in_then_saved_and_forget_removes(self):
        await self.do(cmd="look", kelvin=1900, bright=35)
        await self.do(cmd="save", name="nook")
        looks = (await self.do(cmd="looks"))["looks"]
        self.assertEqual([l["name"] for l in looks][:3], ["coals", "ember", "dusk"])
        self.assertEqual(looks[-1], {"name": "nook", "rgb": [255, 135, 0], "kelvin": 0, "bright": 35, "saved": True, "what": ""})
        self.assertTrue((await self.do(cmd="forget", name="nook"))["ok"])
        self.assertFalse((await self.do(cmd="forget", name="nook"))["ok"])
        self.assertNotIn("nook", [l["name"] for l in (await self.do(cmd="looks"))["looks"]])

    async def test_save_refuses_names_that_already_mean_something(self):
        for bad in ("amber", "Bad Name", "", "9lives"):
            self.assertFalse((await self.do(cmd="save", name=bad))["ok"], bad)

    async def test_find_lists_govee_lights_only_and_connects_to_nothing(self):
        mine = SimpleNamespace(address=light.ADDRESS, name=light.NAME)
        other = SimpleNamespace(address="11111111-2222-3333-4444-555555555555", name=None)
        phone = SimpleNamespace(address="99999999-2222-3333-4444-555555555555", name="Somebody's phone")
        heard = {
            mine.address: (mine, SimpleNamespace(local_name=light.NAME, rssi=-40)),
            other.address: (other, SimpleNamespace(local_name="GVH6008FFFF", rssi=-80)),
            phone.address: (phone, SimpleNamespace(local_name=None, rssi=-30)),
        }

        async def discover(timeout, return_adv):
            return heard

        with patch.object(agent.BleakScanner, "discover", discover):
            r = await self.do(cmd="find")
        lines = r["text"].splitlines()
        self.assertTrue(r["ok"])
        self.assertIn(light.NAME, lines[1])
        self.assertIn("the lamp this tool controls now", lines[1])
        self.assertIn("GVH6008FFFF", lines[2])
        self.assertNotIn("phone", r["text"])
        self.assertEqual((self.connects, self.fake.sent), (0, []))
        with patch.object(agent.BleakScanner, "discover", lambda timeout, return_adv: discover_none()):
            self.assertFalse((await self.do(cmd="find"))["ok"])

    async def test_peek_and_looks_do_not_touch_the_lamp(self):
        await self.do(cmd="peek")
        await self.do(cmd="looks")
        self.assertEqual((self.connects, self.fake.sent), (0, []))


class FailureTests(AgentCase):
    async def test_one_dropped_link_is_retried_once(self):
        await self.do(cmd="status")
        self.fake.fail_next = 1
        r = await self.do(cmd="look", bright=30)
        self.assertTrue(r["ok"])
        self.assertEqual((self.fake.bright, self.connects), (30, 2))

    async def test_a_link_that_stays_down_ends_with_a_plain_message(self):
        await self.do(cmd="status")
        self.fake.fail_next = 5
        with self.assertRaisesRegex(RuntimeError, "Lost the Bluetooth link"):
            await self.do(cmd="look", bright=30)

    async def test_a_bulb_that_disagrees_is_reported_not_retried(self):
        await self.do(cmd="status")
        self.fake.lie = True
        with self.assertRaisesRegex(RuntimeError, "state differs"):
            await self.do(cmd="look", bright=30)
        self.assertEqual(self.connects, 1)

    async def test_a_mode_that_loses_the_lamp_stops_and_leaves_a_note(self):
        await self.do(cmd="candle", bright=30)
        self.fake.dead_after = len(self.fake.sent) + 3
        for _ in range(200):
            await asyncio.sleep(0.01)
            if self.a.mode is None:
                break
        self.assertIsNone(self.a.mode)
        self.assertIn("candle stopped", agent.PROBLEM.read_text())
        self.fake.dead_after = None
        r = await self.do(cmd="status")
        self.assertIn("Note: candle stopped", r["text"])
        self.assertEqual(agent.PROBLEM.read_text(), "")  # shown once


class ModeTests(AgentCase):
    async def test_candle_flickers_until_stopped_then_is_steady(self):
        r = await self.do(cmd="candle", bright=30)
        self.assertIn("Candle running at 30%", r["text"])
        await asyncio.sleep(0.15)
        self.assertGreater(len({bytes(f[3:6]) for f in self.colour_frames()}), 5)
        r = await self.do(cmd="look", bright=20)  # brightness alone leaves the flame running
        self.assertEqual(r["text"], "Candle now at 20%")
        self.assertEqual(self.a.mode_name, "candle")
        r = await self.do(cmd="stop")
        self.assertEqual(r["text"], "Stopped candle. Now on, 20%, #ff780a")
        self.assertEqual(self.shows(), bytes((0x0D, 255, 120, 10)))

    async def test_a_new_look_replaces_a_running_mode(self):
        await self.do(cmd="candle")
        await self.do(cmd="look", rgb=[255, 168, 30])
        self.assertIsNone(self.a.mode)
        self.assertEqual(self.shows(), bytes((0x0D, 255, 168, 30)))

    async def test_timed_candle_ends_off_with_a_steady_colour_stored(self):
        await self.do(cmd="candle", seconds=0.2)
        await self.settled()
        self.assertIsNone(self.a.mode)
        self.assertFalse(self.fake.on)
        self.assertEqual(self.shows(), bytes((0x0D, 255, 120, 10)))

    async def test_sunset_ends_off_and_on_brings_back_the_look(self):
        r = await self.do(cmd="sunset", seconds=0.4)
        self.assertIn("Sunset over", r["text"])
        await self.settled()
        self.assertIsNone(self.a.mode)
        self.assertFalse(self.fake.on)
        self.assertEqual((self.fake.bright, self.shows()), (1, bytes((0x0D, 255, 20, 0))))
        brights = [f[2] for f in self.fake.sent if f[:2] == bytes((0x33, 4))]
        self.assertEqual(brights, sorted(brights, reverse=True))  # only ever dimmer
        r = await self.do(cmd="look", power="on")
        self.assertEqual(r["text"], "on, 70%, #ff7e00 (the 1800K colour mix)")
        self.assertEqual(agent.RESUME.read_text(), "")

    async def test_any_look_after_a_sunset_starts_from_the_look_before(self):
        await self.do(cmd="sunset", seconds=0.3)
        await self.settled()
        r = await self.do(cmd="look", bright=40)  # not the faint red the bulb remembers
        self.assertEqual(r["text"], "on, 40%, #ff7e00 (the 1800K colour mix)")

    async def test_breathe_after_a_sunset_returns_to_the_look_before(self):
        await self.do(cmd="sunset", seconds=0.3)
        await self.settled()
        await self.do(cmd="breathe", seconds=0.2)
        await self.settled()
        self.assertEqual((self.fake.on, self.fake.bright, self.shows()), (True, 70, bytes((0x0D, 255, 126, 0))))

    async def test_sunset_needs_the_lamp_on(self):
        await self.do(cmd="look", power="off")
        r = await self.do(cmd="sunset", seconds=1)
        self.assertFalse(r["ok"])
        self.assertFalse(self.fake.on)

    async def test_sunrise_ends_at_full_real_white(self):
        await self.do(cmd="look", power="off")
        await self.do(cmd="sunrise", seconds=0.5)
        await self.settled()
        self.assertIsNone(self.a.mode)
        self.assertEqual((self.fake.on, self.fake.bright), (True, 100))
        self.assertEqual(int.from_bytes(self.fake.mode[4:6], "big"), 2700)

    async def test_breathe_returns_to_the_look_before(self):
        await self.do(cmd="breathe", seconds=0.2)
        await self.settled()
        self.assertIsNone(self.a.mode)
        self.assertEqual((self.fake.bright, self.shows()), (70, bytes((0x0D, 255, 126, 0))))

    async def test_pulse_on_an_off_lamp_ends_off_with_its_colour_intact(self):
        await self.do(cmd="look", power="off")
        await self.do(cmd="pulse", rgb=[255, 176, 40], times=1, swell=0.05)
        await self.a.mode
        self.assertFalse(self.fake.on)
        self.assertEqual((self.fake.bright, self.shows()), (70, bytes((0x0D, 255, 126, 0))))

    async def test_off_timer_switches_off_and_stop_cancels_it(self):
        await self.do(cmd="timer", seconds=0.1)
        await asyncio.sleep(0.5)
        self.assertFalse(self.fake.on)
        await self.do(cmd="look", power="on")
        await self.do(cmd="timer", seconds=30)
        r = await self.do(cmd="stop")
        self.assertIn("Stopped the off timer", r["text"])
        self.assertIsNone(self.a.timer)

    async def test_check_restores_what_it_found(self):
        r = await self.do(cmd="check")
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "Restored: on, 70%, #ff7e00 (the 1800K colour mix)")
        self.assertIn("off: verified", self.said)


if __name__ == "__main__":
    unittest.main()
