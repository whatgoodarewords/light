"""Local H6008 control. Protocol reference: govee-ble-local 1.10.0 (MIT).
https://pypi.org/project/govee-ble-local/1.10.0/
"""
from __future__ import annotations
import argparse
import asyncio
import fcntl
import functools
import operator
import os
from pathlib import Path
import re
import sys
from bleak import BleakClient, BleakScanner
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4

NAME = "GVH6008DBA0"
ADDRESS = "6D60E3AC-795E-3D7D-980A-FBBB1E7652B3"  # this bulb on this Mac
BASE = "00010203-0405-0607-0809-0a0b0c0d"
SERVICE, WRITE, NOTIFY, VERSION = (BASE + s for s in ("1910", "2b11", "2b10", "2b12"))
PSK = b"MakingLifeSmarte"  # published vendor protocol constant


def frame(opcode, *args, head=0x33):
    data = bytes((head, opcode, *args))
    if len(data) > 19:
        raise ValueError("Command is too long")
    data = data.ljust(19, b"\x00")
    return data + bytes((functools.reduce(operator.xor, data, 0),))


def valid_frame(data):
    return len(data) == 20 and functools.reduce(operator.xor, data, 0) == 0


class Session:
    def __init__(self, key):
        if len(key) != 16:
            raise ValueError("Expected a 16-byte session key")
        self.key = key

    def transform(self, data, encrypt):
        if len(data) != 20:
            raise ValueError("Expected a 20-byte AES/RC4 packet")
        cipher = Cipher(algorithms.AES(self.key), modes.ECB())
        aes = cipher.encryptor() if encrypt else cipher.decryptor()
        # Vendor format restarts RC4 on the final four bytes for each frame.
        rc4 = Cipher(ARC4(self.key), mode=None).encryptor()
        return aes.update(data[:16]) + aes.finalize() + rc4.update(data[16:]) + rc4.finalize()

    def wrap(self, data):
        return self.transform(data, True)

    def unwrap(self, data):
        return self.transform(data, False)


def advertisement_mode(adv):
    flags = {bool(company & 0x40) for company, data in adv.manufacturer_data.items()
             if company >> 8 == 0x88 and company & 0x0F and len(data) >= 4 and data[0] == 0xEC}
    if len(flags) != 1:
        raise RuntimeError("Advertisement does not identify one encryption mode; no command sent")
    return flags.pop()


class Lamp:
    def __init__(self, client, verbose=False):
        self.client, self.verbose = client, verbose
        self.session = None
        self.replies = asyncio.Queue()
        self.handshake = asyncio.Queue()
        self.handshaking = False
        self.handshake_echoes = set()

    def log(self, message):
        if self.verbose:
            print(message, file=sys.stderr, flush=True)

    def notified(self, _sender, payload):
        raw = bytes(payload)
        if raw in self.handshake_echoes:
            return
        if self.handshaking:
            self.handshake.put_nowait(raw)
            return
        try:
            data = self.session.unwrap(raw) if self.session else raw
        except ValueError:
            return
        if valid_frame(data) and data[0] in (0x33, 0xAA, 0xEE):
            self.log(f"Received: {data.hex()}")
            self.replies.put_nowait(data)

    async def raw_write(self, data):
        await asyncio.wait_for(self.client.write_gatt_char(WRITE, data, response=False), 5)

    async def setup(self, encrypted):
        for uuid in (WRITE, NOTIFY):
            if self.client.services.get_characteristic(uuid) is None:
                raise RuntimeError("Expected Govee control characteristic is absent")
        version_char = self.client.services.get_characteristic(VERSION)
        if version_char:
            version = bytes(await asyncio.wait_for(self.client.read_gatt_char(version_char), 5))
            # This bulb has no BGC characteristic. Refuse a different revision.
            raise RuntimeError(f"Unexpected protocol-version characteristic ({version.hex()}); no command sent")
        await asyncio.wait_for(self.client.start_notify(NOTIFY, self.notified), 5)
        await asyncio.sleep(0.2)
        if not encrypted:
            self.log("Protocol: plaintext (advertisement)")
            return
        self.handshaking = True
        psk = Session(PSK)
        key = None
        for step in (1, 2):
            await self.raw_write(psk.wrap(frame(step, *os.urandom(17), head=0xE7)))
            try:
                async with asyncio.timeout(6):
                    while True:
                        raw = await self.handshake.get()
                        if len(raw) != 20:
                            continue
                        reply = psk.unwrap(raw)
                        if valid_frame(reply) and reply[:2] == bytes((0xE7, step)):
                            self.handshake_echoes.add(raw)
                            if step == 1:
                                key = reply[2:18]
                            break
            except TimeoutError:
                raise RuntimeError(f"AES/RC4 handshake step {step} timed out; no light command sent") from None
        self.session = Session(key)
        self.handshaking = False
        self.log("Protocol: AES-ECB/RC4; both handshake replies verified")

    def drain(self):
        while not self.replies.empty():
            self.replies.get_nowait()

    async def send(self, data):
        self.log(f"Sent: {data.hex()}")
        await self.raw_write(self.session.wrap(data) if self.session else data)

    async def receive(self, head, opcode, timeout=4):
        try:
            async with asyncio.timeout(timeout):
                while True:
                    reply = await self.replies.get()
                    if reply[:2] == bytes((head, opcode)):
                        return reply
        except TimeoutError:
            raise RuntimeError(f"No bulb reply for {head:02x} {opcode:02x}; result unconfirmed") from None

    async def query(self, opcode):
        self.drain()
        await self.send(frame(opcode, head=0xAA))
        return await self.receive(0xAA, opcode)

    async def command(self, opcode, *payload):
        self.drain()
        await self.send(frame(opcode, *payload))
        ack = await self.receive(0x33, opcode, timeout=6)
        if ack[2] != 0:
            raise RuntimeError(f"Bulb rejected command {opcode:02x}: status {ack[2]}")
        reply = await self.query(opcode)
        expected = bytes(payload)
        if opcode in (1, 4):
            matches = reply[2] == payload[0]
        elif opcode == 5:
            matches = reply[2:19] == expected.ljust(17, b"\x00")
        else:
            raise ValueError("Unsupported command")
        if not matches:
            raise RuntimeError(f"Bulb acknowledged command but state differs: {reply.hex()}")
        return reply

    async def status(self):
        return {opcode: await self.query(opcode) for opcode in (1, 4, 5)}


def format_status(state):
    power = "on" if state[1][2] else "off"
    color = state[5]
    kelvin = int.from_bytes(color[6:8], "big")
    mode = f"white {kelvin}K" if color[2] == 13 and kelvin else f"color #{color[3:6].hex()}"
    return f"{power}, brightness {state[4][2]}%, {mode}"


def percent(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Brightness must be a whole number from 0 to 100") from None
    if not 0 <= number <= 100:
        raise argparse.ArgumentTypeError("Brightness must be 0 to 100")
    return number


def rgb(value):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise argparse.ArgumentTypeError('Use a quoted hex color such as "#ff8800"')
    return tuple(bytes.fromhex(value[1:]))


def arguments(argv=None):
    parser = argparse.ArgumentParser(prog="light", description=f"Local Bluetooth control of {NAME}. Every change requires an ACK and matching state readback.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show protocol and command frames (no session keys)")
    parser.add_argument("--timeout", type=float, default=20, help="Discovery/connection timeout in seconds (default 20)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("on", "off", "status"):
        sub.add_parser(name)
    sub.add_parser("test", help="Test power, brightness and color, then restore original settings")
    sub.add_parser("bright", help="Set brightness 1-100; 0 switches off").add_argument("value", type=percent)
    sub.add_parser("color").add_argument("value", type=rgb)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 120:
        parser.error("Timeout must be between 1 and 120 seconds")
    return args


async def exercise(lamp):
    original = await lamp.status()
    print(f"Original: {format_status(original)}", flush=True)
    try:
        await lamp.command(1, 0)
        print("Verified: off", flush=True)
        await asyncio.sleep(3)
        await lamp.command(1, 1)
        print("Verified: on", flush=True)
        await lamp.command(4, 40)
        print("Verified: brightness 40%", flush=True)
        await asyncio.sleep(3)
        await lamp.command(5, 13, 255, 136, 0)
        print("Verified: color #ff8800", flush=True)
        await asyncio.sleep(3)
    finally:
        errors = []
        for opcode, payload in ((5, original[5][2:19]), (4, original[4][2:3]), (1, original[1][2:3])):
            try:
                await lamp.command(opcode, *payload)
            except Exception as exc:
                errors.append(f"{opcode:02x}: {exc}")
        if errors:
            raise RuntimeError("Could not fully restore original settings: " + "; ".join(errors))
        print(f"Restored: {format_status(await lamp.status())}", flush=True)


async def run(args):
    advertisement = None
    def matches(device, adv):
        nonlocal advertisement
        if device.address.upper() != ADDRESS or (adv.local_name != NAME and device.name != NAME):
            return False
        advertisement = adv
        return True
    if args.verbose:
        print(f"Finding {NAME}...", file=sys.stderr, flush=True)
    device = await BleakScanner.find_device_by_filter(matches, timeout=args.timeout)
    if device is None:
        raise RuntimeError(f"{NAME} not found. Check the lamp has power and is nearby, then retry.")
    encrypted = advertisement_mode(advertisement)
    client = BleakClient(device, services=[SERVICE], timeout=args.timeout)
    try:
        await asyncio.wait_for(client.connect(), args.timeout)
        lamp = Lamp(client, args.verbose)
        await lamp.setup(encrypted)
        if args.command == "status":
            print(format_status(await lamp.status()), flush=True)
        elif args.command == "test":
            await exercise(lamp)
        elif args.command in ("on", "off"):
            await lamp.command(1, int(args.command == "on"))
            print(f"Verified: {args.command}", flush=True)
        elif args.command == "bright":
            if args.value:
                await lamp.command(4, args.value)
                print(f"Verified: brightness {args.value}%", flush=True)
            else:
                await lamp.command(1, 0)
                print("Verified: off", flush=True)
        elif args.command == "color":
            await lamp.command(5, 13, *args.value)
            print(f"Verified: color #{bytes(args.value).hex()}", flush=True)
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), 5)
        except Exception as exc:
            print(f"light: disconnect warning: {exc}", file=sys.stderr)


def main():
    args = arguments()
    try:
        lock_path = Path(__file__).resolve().parent / "work" / "command.lock"
        lock_path.parent.mkdir(exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another light command is running; retry when it finishes") from None
            asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        message = str(exc) or "Bluetooth operation timed out; result unconfirmed"
        print(f"light: {message}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
