import argparse
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import light


class ProtocolTests(unittest.TestCase):
    def test_reference_cipher_vector(self):
        # AES: NIST AES-128 vector. RC4 tail: independently checked with OpenSSL.
        session = light.Session(bytes(range(16)))
        plain = bytes.fromhex("00112233445566778899aabbccddeeff00000000")
        expected = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55ae99c40f9")
        self.assertEqual(session.wrap(plain), expected)
        self.assertEqual(session.unwrap(expected), plain)
        self.assertEqual(session.wrap(plain), expected)  # RC4 restarts per frame

    def test_power_wire_frame(self):
        self.assertEqual(light.frame(1, 1).hex(), "3301010000000000000000000000000000000033")
        with self.assertRaises(ValueError):
            light.frame(1, *range(18))

    def test_advertisement_encryption_without_version_characteristic(self):
        for company, expected in ((0x8841, True), (0x8801, False)):
            adv = SimpleNamespace(manufacturer_data={company: bytes.fromhex("ec000101")})
            self.assertIs(light.advertisement_mode(adv), expected)

    def test_missing_or_ambiguous_advertisement_fails(self):
        for data in ({}, {0x8841: b"\xec"}, {0x8841: b"\xec\0\1\1", 0x8801: b"\xec\0\1\1"}):
            with self.assertRaises(RuntimeError):
                light.advertisement_mode(SimpleNamespace(manufacturer_data=data))

    def test_inputs(self):
        self.assertEqual(light.rgb("#ff8800"), (255, 136, 0))
        self.assertEqual(light.percent("40"), 40)
        for value in ("-1", "101", "nan", "40.5"):
            with self.assertRaises(argparse.ArgumentTypeError):
                light.percent(value)
        for value in ("ff8800", "#000", "#gg0000"):
            with self.assertRaises(argparse.ArgumentTypeError):
                light.rgb(value)


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_dispatch_brightness_and_color(self):
        adv = SimpleNamespace(manufacturer_data={0x8841: bytes.fromhex("ec000101")}, local_name=light.NAME)
        device = SimpleNamespace(address=light.ADDRESS, name=light.NAME)
        async def find(matches, timeout):
            self.assertTrue(matches(device, adv))
            return device
        for argv, expected in ((["bright", "40"], (4, 40)), (["color", "#ff8800"], (5, 13, 255, 136, 0)), (["bright", "0"], (1, 0))):
            client, lamp = AsyncMock(), AsyncMock()
            with patch.object(light.BleakScanner, "find_device_by_filter", find), patch.object(light, "BleakClient", return_value=client), patch.object(light, "Lamp", return_value=lamp), patch("builtins.print"):
                await light.run(light.arguments(argv))
            lamp.setup.assert_awaited_once_with(True)
            lamp.command.assert_awaited_once_with(*expected)
            client.disconnect.assert_awaited_once()

    async def test_ack_is_not_state_confirmation(self):
        lamp = light.Lamp(None)
        lamp.send = AsyncMock()
        lamp.receive = AsyncMock(return_value=light.frame(1, 0))
        lamp.query = AsyncMock(return_value=light.frame(1, 0, head=0xAA))
        with self.assertRaisesRegex(RuntimeError, "state differs"):
            await lamp.command(1, 1)

    async def test_rejected_ack_fails_without_query(self):
        lamp = light.Lamp(None)
        lamp.send = AsyncMock()
        lamp.receive = AsyncMock(return_value=light.frame(4, 1))
        lamp.query = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            await lamp.command(4, 40)
        lamp.query.assert_not_awaited()

    async def test_color_checks_submode_and_rgb(self):
        lamp = light.Lamp(None)
        lamp.send = AsyncMock()
        lamp.receive = AsyncMock(return_value=light.frame(5, 0))
        lamp.query = AsyncMock(return_value=light.frame(5, 13, 255, 136, 0, head=0xAA))
        await lamp.command(5, 13, 255, 136, 0)
        lamp.query.return_value = light.frame(5, 2, 255, 136, 0, head=0xAA)
        with self.assertRaisesRegex(RuntimeError, "state differs"):
            await lamp.command(5, 13, 255, 136, 0)
        lamp.query.return_value = light.frame(5, 13, 255, 136, 0, 10, 140, head=0xAA)
        with self.assertRaisesRegex(RuntimeError, "state differs"):
            await lamp.command(5, 13, 255, 136, 0)

    async def test_corruption_and_unrelated_frames_are_ignored(self):
        lamp = light.Lamp(None)
        valid = light.frame(1, 1, head=0xAA)
        lamp.notified(None, valid[:-1] + bytes((valid[-1] ^ 1,)))
        self.assertTrue(lamp.replies.empty())
        lamp.notified(None, light.frame(4, 40, head=0xAA))
        lamp.notified(None, valid)
        self.assertEqual(await lamp.receive(0xAA, 1), valid)

    async def test_missing_reply_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, "result unconfirmed"):
            await light.Lamp(None).receive(0xAA, 1, timeout=0.001)

    async def test_two_step_handshake_before_command(self):
        session_key = bytes(range(16))
        psk = light.Session(light.PSK)
        client = SimpleNamespace()
        client.services = SimpleNamespace(get_characteristic=lambda uuid: None if uuid == light.VERSION else object())
        lamp = light.Lamp(client)
        client.start_notify = AsyncMock()
        seen = []
        async def write(uuid, data, response):
            self.assertEqual(uuid, light.WRITE)
            self.assertFalse(response)
            decoded = psk.unwrap(data)
            self.assertTrue(light.valid_frame(decoded))
            self.assertEqual(decoded[0], 0xE7)
            seen.append(decoded[1])
            payload = session_key if decoded[1] == 1 else b""
            lamp.notified(None, psk.wrap(light.frame(decoded[1], *payload, head=0xE7)))
        client.write_gatt_char = write
        await lamp.setup(True)
        self.assertEqual(seen, [1, 2])
        self.assertEqual(lamp.session.key, session_key)


if __name__ == "__main__":
    unittest.main()
