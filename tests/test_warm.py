import unittest
from unittest.mock import AsyncMock

import light
import warm


class KelvinTests(unittest.TestCase):
    def test_white_frame_at_and_above_2700(self):
        payload, white = warm.kelvin_payload(2700)
        self.assertTrue(white)
        # 0d, tint, Kelvin big endian (0x0a8c = 2700), tint again
        self.assertEqual(bytes(payload).hex(), "0dffae540a8cffae54")

    def test_colour_mix_below_2700(self):
        for kelvin, colour in ((1800, "ff8200"), (2000, "ff8d0b"), (2200, "ff9829")):
            payload, white = warm.kelvin_payload(kelvin)
            self.assertFalse(white)
            self.assertEqual(bytes(payload).hex(), "0d" + colour)

    def test_rounds_to_the_table_and_rejects_out_of_range(self):
        self.assertEqual(warm.tint(1849), warm.tint(1800))
        self.assertEqual(warm.tint(1850), warm.tint(1800))  # banker's rounding of 18.5
        self.assertEqual(warm.tint(1851), warm.tint(1900))
        for bad in (999, 6501):
            with self.assertRaises(ValueError):
                warm.kelvin_payload(bad)


class SceneTests(unittest.TestCase):
    def test_upload_frames_reassemble_to_the_data(self):
        for name, (code, data) in warm.SCENES.items():
            raw = bytes.fromhex(data)
            if not raw:
                continue
            frames = warm.scene_upload_frames(raw)
            self.assertGreaterEqual(len(frames), 2, name)
            self.assertTrue(all(light.valid_frame(f) and f[0] == 0xA3 for f in frames), name)
            self.assertEqual([f[1] for f in frames], [*range(len(frames) - 1), 0xFF], name)
            stream = b"".join(f[2:19] for f in frames)
            self.assertEqual(stream[:3], bytes((1, len(frames), 1)), name)
            self.assertEqual(stream[3:3 + len(raw)], raw, name)
            self.assertFalse(any(stream[3 + len(raw):]), name)

    def test_short_data_still_gets_an_end_frame(self):
        frames = warm.scene_upload_frames(b"\x01\x02")
        self.assertEqual([f[1] for f in frames], [0, 0xFF])

    def test_select_is_little_endian(self):
        self.assertEqual(warm.scene_select_payload(4024), (0x04, 0xB8, 0x0F))

    def test_presets_are_sendable(self):
        for name, (_, look, bright) in warm.PRESETS.items():
            self.assertTrue(1 <= bright <= 100, name)
            if isinstance(look, int):
                warm.kelvin_payload(look)
            else:
                light.rgb(look)


class FlameTests(unittest.TestCase):
    def test_levels_stay_in_range_and_move(self):
        flame = warm.Flame(seed=7)
        levels = [flame.level(0.125) for _ in range(8 * 600)]  # ten minutes at 8 per second
        self.assertTrue(all(0.2 <= v <= 1.0 for v in levels))
        self.assertGreater(max(levels) - min(levels), 0.15)  # it does flicker
        mean = sum(levels) / len(levels)
        self.assertTrue(0.75 < mean < 0.98, mean)  # mostly near full height
        steps = sorted(abs(a - b) for a, b in zip(levels, levels[1:]))
        self.assertLess(steps[len(steps) // 2], 0.05)  # the usual step is small

    def test_zero_depth_is_steady(self):
        flame = warm.Flame(depth=0, seed=1)
        self.assertEqual({flame.next(0.125) for _ in range(200)}, {(255, 120, 10)})

    def test_dimmer_is_redder_and_bytes_are_valid(self):
        flame = warm.Flame(seed=3)
        full, low = flame.color(1.0), flame.color(0.4)
        self.assertEqual(full, (255, 120, 10))
        self.assertLess(low[1] / low[0], full[1] / full[0])
        for _ in range(2000):
            self.assertTrue(all(0 <= c <= 255 for c in flame.next(0.125)))


class LampTests(unittest.IsolatedAsyncioTestCase):
    def lamp(self, state):
        lamp = light.Lamp(None)
        lamp.send = AsyncMock()
        lamp.receive = AsyncMock(return_value=light.frame(5, 0))
        lamp.query = AsyncMock(return_value=state)
        return lamp

    async def test_white_kelvin_checks_the_reported_kelvin_not_the_tint(self):
        # The bulb reports the Kelvin but not the second tint slot.
        lamp = self.lamp(light.frame(5, 13, 255, 174, 84, 0x0A, 0x8C, head=0xAA))
        self.assertTrue(await lamp.kelvin(2700))
        lamp.query.return_value = light.frame(5, 13, 255, 174, 84, 0x0B, 0xB8, head=0xAA)  # says 3000
        with self.assertRaisesRegex(RuntimeError, "state differs"):
            await lamp.kelvin(2700)

    async def test_mixed_kelvin_is_a_verified_colour(self):
        lamp = self.lamp(light.frame(5, 13, 255, 0x8D, 0x0B, head=0xAA))
        self.assertFalse(await lamp.kelvin(2000))
        lamp.send.assert_awaited_once_with(light.frame(5, 13, 255, 0x8D, 0x0B))

    async def test_scene_uploads_then_selects_and_checks_the_mode(self):
        lamp = self.lamp(light.frame(5, 4, 0xB8, 0x0F, head=0xAA))
        await lamp.scene("candlelight")
        sent = [c.args[0] for c in lamp.send.await_args_list]
        self.assertTrue(all(f[0] == 0xA3 for f in sent[:-1]))
        self.assertEqual(sent[-1], light.frame(5, 4, 0xB8, 0x0F))
        lamp.query.return_value = light.frame(5, 13, 255, 0, 0, head=0xAA)  # still plain colour
        with self.assertRaisesRegex(RuntimeError, "state differs"):
            await lamp.scene("candlelight")

    def test_status_names_a_scene(self):
        state = {1: light.frame(1, 1, head=0xAA), 4: light.frame(4, 30, head=0xAA),
                 5: light.frame(5, 4, 0xB8, 0x0F, head=0xAA)}
        self.assertEqual(light.format_status(state), "on, brightness 30%, scene candlelight")


if __name__ == "__main__":
    unittest.main()
