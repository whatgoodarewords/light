"""Background helper for the `light` command. Runs inside Light.app.

Why it exists: macOS grants Bluetooth to an app, not to a terminal command.
Light.app carries that permission, keeps the Bluetooth link open between
commands so they answer at once, and runs the animations (candle, sunset...).
It disconnects and exits by itself after a short idle time.

cli.py sends one JSON request per connection over a socket in work/ and
reads JSON lines back. Every lasting change to the lamp is verified the way
light.py does it: the bulb's ACK, then a state readback that must match.
Animation frames in between are sent without waiting, so motion stays smooth.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason, BleakError

import light as proto
import warm

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
SOCKET_NAME = "agent.sock"  # bound relative to WORK: socket paths are limited to 104 bytes
USER_PRESETS = ROOT / "presets.json"
RESUME = WORK / "resume.json"
PROBLEM = WORK / "last-problem.txt"
IDLE_SECONDS = 45
FPS = 10

log = logging.getLogger("light")


def code_version() -> str:
    """Changes whenever a source file changes, so an old helper is replaced."""
    return str(max(int(p.stat().st_mtime) for p in ROOT.glob("*.py")))


@dataclass(frozen=True)
class Look:
    """What the lamp is showing. kelvin > 0 means the white LEDs; scene > 0 a stored scene."""

    on: bool = True
    bright: int = 100
    rgb: tuple = (255, 255, 255)
    kelvin: int = 0
    scene: int = 0

    @staticmethod
    def from_status(state) -> "Look":
        mode = state[5]
        look = Look(on=bool(state[1][2]), bright=state[4][2])
        if mode[2] == 0x04:
            return replace(look, scene=int.from_bytes(mode[3:5], "little"))
        return replace(look, rgb=tuple(mode[3:6]), kelvin=int.from_bytes(mode[6:8], "big") if mode[2] == 0x0D else 0)

    @staticmethod
    def from_json(data) -> "Look":
        return Look(bool(data["on"]), int(data["bright"]), tuple(data["rgb"]), int(data["kelvin"]), int(data["scene"]))

    def what(self) -> str:
        if self.scene:
            names = [n for n, (c, _) in warm.SCENES.items() if c == self.scene]
            return f"scene {names[0] if names else self.scene}"
        if self.kelvin:
            return f"white {self.kelvin}K"
        k = warm.kelvin_of(self.rgb)
        return f"#{bytes(self.rgb).hex()}" + (f" (the {k}K colour mix)" if k else "")

    def describe(self) -> str:
        return f"{'on' if self.on else 'off'}, {self.bright}%, {self.what()}"


class Agent:
    def __init__(self):
        self.client: BleakClient | None = None
        self.lamp: proto.Lamp | None = None
        self.io = asyncio.Lock()  # one exchange with the bulb at a time
        self.cmd = asyncio.Lock()  # one request at a time
        self.look: Look | None = None  # last state read from, or sent to, the bulb
        self.previous: Look | None = None  # for `light back`
        self.mode: asyncio.Task | None = None
        self.mode_name = ""
        self.mode_note = ""
        self.mode_tidy = True  # whether a stopped mode should put things in order
        self._mode_coro = None
        self.mode_starts = 0.0  # wall-clock times for the panel: when a waiting mode begins,
        self.mode_ends = 0.0  # and when a mode ends by itself (0 = runs until stopped)
        self.flame: warm.Flame | None = None
        self.feel = "normal"  # calm | normal | lively
        self.timer_due = 0.0
        self.timer: asyncio.Task | None = None
        self.timer_note = ""
        self.awake: subprocess.Popen | None = None
        self.last_io = time.monotonic()
        self.last_request = time.monotonic()
        self.done = asyncio.Event()

    # ------------------------------------------------------------ the link

    def _dropped(self, _client) -> None:
        log.info("link dropped")
        self.lamp = None

    async def connect(self) -> None:
        """Connect to the one pinned lamp.

        First by its known identifier: macOS can connect to a lamp it has met
        before without hearing an advertisement, which takes one to three
        seconds. In this building, listening for advertisements missed the lamp
        for half a minute at a time (measured), so listening is only the
        fallback, for a Mac that has no record of the lamp yet.
        """
        if self.lamp is not None and self.client is not None and self.client.is_connected:
            return
        await self.disconnect()
        try:
            device = await asyncio.wait_for(known_device(), 40)
        except asyncio.TimeoutError:
            # Looking up a known lamp is instant; only an unanswered permission prompt takes this long.
            raise RuntimeError(
                "macOS has not allowed Light to use Bluetooth yet. Answer the prompt on screen, or allow Light "
                "under System Settings, Privacy & Security, Bluetooth. Then try again."
            ) from None
        except BleakBluetoothNotAvailableError as exc:
            if exc.reason == BleakBluetoothNotAvailableReason.POWERED_OFF:
                raise RuntimeError("Bluetooth is off on this Mac. Turn it on and try again.") from None
            raise RuntimeError(
                "Light is not allowed to use Bluetooth. Allow it under System Settings, Privacy & Security, "
                "Bluetooth. Then try again."
            ) from None
        # This bulb's measured mode. If a firmware update ever changed it, the handshake fails and says so.
        encrypted = True
        client = await self._open(device, 12) if device is not None else None
        if client is None:
            log.info("no connection by known identifier; listening for the lamp instead")
            seen = {}

            def matches(dev, adv):
                if dev.address.upper() != proto.ADDRESS or (adv.local_name != proto.NAME and dev.name != proto.NAME):
                    return False
                seen["adv"] = adv
                return True

            device = await BleakScanner.find_device_by_filter(matches, timeout=15)
            if device is not None:
                encrypted = proto.advertisement_mode(seen["adv"])
                client = await self._open(device, 15)
        if client is None:
            raise RuntimeError(
                f"{proto.NAME} did not answer. Check the lamp has power and is nearby. "
                "If the Govee app is connected to it, close the app, then try again."
            )
        self.client = client
        lamp = proto.Lamp(client)
        await lamp.setup(encrypted)
        self.lamp = lamp
        log.info("connected")

    async def _open(self, device, seconds: float) -> BleakClient | None:
        """One connection attempt, bounded. None if the lamp did not take it."""
        client = BleakClient(device, services=[proto.SERVICE], timeout=seconds, disconnected_callback=self._dropped)
        try:
            await asyncio.wait_for(client.connect(), seconds + 3)
            return client
        except (asyncio.TimeoutError, BleakError, OSError) as exc:
            log.info("connection attempt failed: %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 5)
            return None

    async def disconnect(self) -> None:
        client, self.client, self.lamp = self.client, None, None
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 5)
            log.info("disconnected")

    async def with_lamp(self, fn):
        """Run one exchange with the bulb. If the link has gone, reconnect and
        try once more; a second failure is reported. An answer from the bulb
        that says no (rejected, or state differs) is never retried."""
        for attempt in (1, 2):
            try:
                await self.connect()
                async with self.io:
                    self.last_io = time.monotonic()
                    return await fn(self.lamp)
            except RuntimeError as exc:
                text = str(exc)
                final = any(s in text for s in ("rejected", "state differs", "did not answer", "Advertisement", "Unexpected",
                                                 "not allowed", "Bluetooth is off"))
                if final or attempt == 2:
                    raise
                log.info("retrying after: %s", text)
                await self.disconnect()
            except (BleakError, OSError, asyncio.TimeoutError, AttributeError) as exc:
                log.info("link problem: %s: %s", type(exc).__name__, exc)
                await self.disconnect()
                if attempt == 2:
                    raise RuntimeError("Lost the Bluetooth link to the lamp and could not get it back. Try again.") from exc

    # ----------------------------------------------- verified single changes

    async def read(self) -> Look:
        self.look = Look.from_status(await self.with_lamp(lambda lamp: lamp.status()))
        return self.look

    async def current(self) -> Look:
        return self.look if self.look is not None else await self.read()

    async def set_power(self, on: bool) -> None:
        await self.with_lamp(lambda lamp: lamp.command(1, int(on)))
        self.look = replace(await self.current(), on=on)

    async def set_bright(self, value: int) -> None:
        value = max(1, min(100, int(value)))
        await self.with_lamp(lambda lamp: lamp.command(4, value))
        self.look = replace(await self.current(), bright=value, on=True)  # any setting wakes this bulb

    async def set_rgb(self, rgb) -> None:
        rgb = tuple(rgb)
        await self.with_lamp(lambda lamp: lamp.command(5, 13, *rgb))
        self.look = replace(await self.current(), rgb=rgb, kelvin=0, scene=0, on=True)

    async def set_kelvin(self, kelvin: int) -> None:
        if kelvin < warm.WHITE_MIN_K:
            await self.set_rgb(warm.tint(kelvin))
            return
        await self.with_lamp(lambda lamp: lamp.kelvin(kelvin))
        self.look = replace(await self.current(), rgb=warm.tint(kelvin), kelvin=kelvin, scene=0, on=True)

    async def set_scene(self, name: str) -> None:
        await self.with_lamp(lambda lamp: lamp.scene(name))
        self.look = replace(await self.current(), scene=warm.SCENES[name][0], kelvin=0, on=True)

    # ------------------------------------------- unverified animation frames

    async def _fast(self, data: bytes) -> None:
        async def go(lamp):
            lamp.drain()
            await lamp.send(data)
        await self.with_lamp(go)

    async def paint(self, rgb) -> None:
        rgb = tuple(max(0, min(255, int(c))) for c in rgb)
        if self.look is not None and self.look.rgb == rgb and not self.look.kelvin and not self.look.scene and self.look.on:
            return
        await self._fast(proto.frame(5, 13, *rgb))
        self.look = replace(self.look or Look(), rgb=rgb, kelvin=0, scene=0, on=True)

    async def paint_white(self, kelvin: int) -> None:
        """Real white at `kelvin` (2700 and up), unverified, for a slider being dragged."""
        if self.look is not None and self.look.kelvin == kelvin and not self.look.scene and self.look.on:
            return
        payload, _ = warm.kelvin_payload(kelvin)
        await self._fast(proto.frame(5, *payload))
        self.look = replace(self.look or Look(), rgb=warm.tint(kelvin), kelvin=kelvin, scene=0, on=True)

    async def dim(self, value: int) -> None:
        value = max(1, min(100, int(round(value))))
        if self.look is not None and self.look.bright == value and self.look.on:
            return
        await self._fast(proto.frame(4, value))
        self.look = replace(self.look or Look(), bright=value, on=True)

    # ------------------------------------------------------- moving to a look

    async def apply(self, target: Look, glide: float = 0.6) -> None:
        """Bring the lamp to `target` with a short soft transition where the
        bulb allows one, and finish with verified commands."""
        cur = await self.current()
        if not target.on:
            if cur.on:
                await self.set_power(False)
            return
        if target.scene:
            names = [n for n, (c, _) in warm.SCENES.items() if c == target.scene]
            if not names:
                raise RuntimeError("That scene is not one this tool knows how to send")
            await self.set_scene(names[0])
            if self.look.bright != target.bright:
                await self.set_bright(target.bright)
            await self.set_power(True)
            return
        plain = not target.kelvin  # a colour, which can be glided to
        if not cur.on:
            # Wake low and rise, instead of snapping on at full.
            await self.set_bright(min(target.bright, 2))
            await (self.set_kelvin(target.kelvin) if target.kelvin else self.set_rgb(target.rgb))
            await self._ramp_bright(self.look.bright, target.bright, 0.8)
        elif plain and not cur.kelvin and not cur.scene and glide:
            steps = max(2, int(glide * FPS))
            for i in range(1, steps):
                f = warm.ease(i / steps)
                await self.paint(warm.mix(cur.rgb, target.rgb, f))
                await self.dim(cur.bright + (target.bright - cur.bright) * f)
                await asyncio.sleep(1 / FPS)
        elif target.bright < cur.bright:
            await self.set_bright(target.bright)  # dim first so the new colour never shows too bright
        if target.kelvin:
            if self.look.kelvin != target.kelvin or self.look.scene:
                await self.set_kelvin(target.kelvin)
        else:
            await self.set_rgb(target.rgb)
        # Always verified, even if a glide frame already asked for this value: glide frames are not checked.
        await self.set_bright(target.bright)
        await self.set_power(True)

    async def _ramp_bright(self, start: int, end: int, seconds: float) -> None:
        steps = max(1, int(seconds * FPS))
        for i in range(1, steps):
            await self.dim(warm.geometric(max(1, start), max(1, end), i / steps))
            await asyncio.sleep(1 / FPS)

    # ---------------------------------------------------------------- modes

    def _keep_awake(self) -> None:
        """Stop the Mac idling to sleep while a mode or timer is pending."""
        busy = self.mode is not None or self.timer is not None
        if busy and self.awake is None:
            with contextlib.suppress(OSError):
                self.awake = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
        elif not busy and self.awake is not None:
            self.awake.terminate()
            self.awake = None

    async def start_mode(self, name: str, note: str, coro, starts: float = 0.0, ends: float = 0.0) -> None:
        await self.stop_mode(tidy=False)
        self.mode_starts, self.mode_ends = starts, ends

        async def runner():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reported to the person through `light status`
                log.exception("mode %s failed", name)
                self.problem(f"{name} stopped at {time.strftime('%H:%M')}: {exc}")
            finally:
                if self.mode is asyncio.current_task():
                    self.mode, self.mode_name, self.mode_note = None, "", ""
                    self._keep_awake()

        self.mode_tidy = True
        self.mode_name, self.mode_note = name, note
        self._mode_coro = coro
        self.mode = asyncio.create_task(runner())
        self._keep_awake()

    async def stop_mode(self, tidy: bool) -> str:
        """Stop the running mode. tidy=True lets it leave the lamp in order
        (steady flame colour, or the look from before); tidy=False is used when
        a new look is about to replace it anyway."""
        task, name = self.mode, self.mode_name
        if task is None:
            return ""
        self.mode_tidy = tidy
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self._mode_coro.close()  # a mode replaced before its first step never ran; do not leave it dangling
        self.mode, self.mode_name, self.mode_note = None, "", ""
        self._keep_awake()
        await asyncio.sleep(0.25)  # let the last animation frames' ACKs arrive before verified commands
        return name

    def problem(self, text: str) -> None:
        log.warning("problem: %s", text)
        with contextlib.suppress(OSError):
            PROBLEM.write_text(text + "\n")

    async def candle(self, flame: warm.Flame, seconds, then_off) -> None:
        base = flame.base
        end = time.monotonic() + seconds if seconds else None
        finished = False
        try:
            while end is None or time.monotonic() < end:
                await self.paint(flame.next(1 / FPS))
                await asyncio.sleep(1 / FPS)
            finished = True
        finally:
            if finished and then_off:
                await asyncio.sleep(0.3)
                await self.set_rgb(base)  # so the lamp comes back steady, not mid-flicker
                await self.set_power(False)
            elif finished or self.mode_tidy:
                await asyncio.sleep(0.3)
                await self.set_rgb(base)

    async def breathe(self, before: Look, base, seconds: float) -> None:
        start = time.monotonic()
        try:
            while time.monotonic() - start < seconds:
                await self.paint(warm.scale(base, warm.breath_level(time.monotonic() - start)))
                await asyncio.sleep(1 / FPS)
        finally:
            if self.mode_tidy:
                await asyncio.sleep(0.3)
                await self.apply(before)

    async def sunset(self, before: Look, seconds: float, turn_off: bool = True, resume: Look | None = None) -> None:
        """Fade from `before` (a plain colour) to deep red. `resume` is the look
        that `light on` should bring back afterwards."""
        colors = warm.sunset_colors(before.rgb)
        start = time.monotonic()
        tick = max(0.1, min(2.0, seconds / 400))
        finished = False
        try:
            while True:
                f = (time.monotonic() - start) / seconds
                if f >= 1:
                    break
                await self.paint(warm.along(colors, f))
                await self.dim(warm.geometric(before.bright, 1, f))
                await asyncio.sleep(tick)
            finished = True
        finally:
            if finished and turn_off:
                await asyncio.sleep(0.3)
                await self.set_rgb(colors[-1])
                await self.set_bright(1)
                await self.set_power(False)
                # The bulb now remembers a faint red. `light on` brings back the look from before.
                RESUME.write_text(json.dumps(asdict(replace(resume or before, on=True))))
            elif finished:
                await asyncio.sleep(0.3)
                await self.set_rgb(colors[-1])

    async def sunrise(self, wait: float, seconds: float, white_at: int) -> None:
        if wait > 0:
            await self._sleep_until(time.time() + wait)
        colors = list(reversed(warm.SUNSET_PATH))
        await self.set_bright(1)
        await self.set_rgb(colors[0])
        start = time.monotonic()
        tick = max(0.1, min(2.0, seconds / 400))
        colour_part = 0.8  # the last fifth is real white light
        while True:
            f = (time.monotonic() - start) / seconds
            if f >= colour_part:
                break
            await self.paint(warm.along(colors, f / colour_part))
            await self.dim(warm.geometric(1, 100, f / colour_part))
            await asyncio.sleep(tick)
        await asyncio.sleep(0.3)
        await self.set_bright(white_at)
        await self.set_kelvin(warm.WHITE_MIN_K)
        while True:
            f = (time.monotonic() - start) / seconds
            if f >= 1:
                break
            await self.dim(warm.geometric(white_at, 100, (f - colour_part) / (1 - colour_part)))
            await asyncio.sleep(tick)
        await asyncio.sleep(0.3)
        await self.set_bright(100)
        RESUME.write_text("")

    async def pulse(self, before: Look, color, times: int, swell: float) -> None:
        low = warm.scale(color, 0.12)
        start = before.rgb if before.on and not before.kelvin and not before.scene else low
        try:
            if not before.on or before.kelvin or before.scene:
                await self.paint(low)
            for n in range(times):
                steps = max(2, int(swell * FPS))
                origin = start if n == 0 else low
                for i in range(1, steps + 1):
                    await self.paint(warm.mix(origin, color, warm.ease(i / steps)))
                    await asyncio.sleep(1 / FPS)
                for i in range(1, steps + 1):
                    await self.paint(warm.mix(color, low, warm.ease(i / steps)))
                    await asyncio.sleep(1 / FPS)
        finally:
            await asyncio.sleep(0.3)
            # Always back to the look from before, also when stopped, so the bulb never remembers the dark end.
            await self.apply(replace(before, on=True), glide=0.4)
            if not before.on:
                await self.set_power(False)

    async def tour(self, before: Look, say) -> None:
        try:
            for name in ("coals", "ember", "dusk", "amber", "gold", "glow", "lamp", "tungsten"):
                what, look, bright = warm.PRESETS[name]
                say(f"{name}: {what}, {bright}%")
                await self.apply(preset_look(look, bright), glide=1.2)
                await asyncio.sleep(2.2)
            say("candle: a flame driven from this Mac")
            await self.apply(Look(True, 35, (255, 120, 10)), glide=1.0)
            flame = warm.Flame(depth=0.6, calm=0.3)
            end = time.monotonic() + 12
            while time.monotonic() < end:
                await self.paint(flame.next(1 / FPS))
                await asyncio.sleep(1 / FPS)
            say("breathe: in for 4 seconds, out for 6")
            start = time.monotonic()
            while time.monotonic() - start < 10:
                await self.paint(warm.scale((255, 110, 10), warm.breath_level(time.monotonic() - start)))
                await asyncio.sleep(1 / FPS)
            say("sunset: a half-hour wind-down, here in 12 seconds")
            await asyncio.sleep(0.3)
            await self.apply(Look(True, 60, (255, 168, 30)), glide=0.8)
            await self.sunset(self.look, 12, turn_off=False)
            await asyncio.sleep(1.0)
        finally:
            say("back to the look from before")
            await asyncio.sleep(0.3)
            await self.apply(replace(before, on=True), glide=1.0)
            if not before.on:
                await self.set_power(False)

    async def _sleep_until(self, due: float) -> None:
        """Wait for a wall-clock time, so a Mac that slept acts late rather than never.
        The link is dropped during long waits; reconnect shortly before the time so the
        action starts on time even when the first scan misses the lamp."""
        ready = False
        while time.time() < due:
            left = due - time.time()
            if not ready and left < 35:
                ready = True
                with contextlib.suppress(Exception):
                    await self.connect()
                    self.last_io = time.monotonic()
            await asyncio.sleep(min(5, max(0.1, left)))

    async def off_later(self, seconds: float) -> None:
        try:
            await self._sleep_until(time.time() + seconds)
            async with self.cmd:
                await self.stop_mode(tidy=True)
                await self.set_power(False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.problem(f"the timed off at {time.strftime('%H:%M')} did not work: {exc}")
        finally:
            if self.timer is asyncio.current_task():
                self.timer, self.timer_note = None, ""
                self._keep_awake()

    # ------------------------------------------------------------- requests

    async def remember(self) -> Look:
        """Read the lamp's real state (it may have been changed at the wall or in the
        Govee app since the last command) and note it so `light back` can return to it."""
        cur = await self.read() if self.mode is None else await self.current()
        if self.mode is None:
            self.previous = cur
        return cur

    def snapshot(self) -> dict:
        """Everything the menu bar panel shows, from what the helper already knows (no Bluetooth)."""
        look = self.look
        snap: dict = {"known": look is not None}
        if look is not None:
            scene = next((n for n, (c, _) in warm.SCENES.items() if c == look.scene), "") if look.scene else ""
            mix = 0 if look.kelvin or look.scene else (warm.kelvin_of(look.rgb) or 0)
            snap.update(on=look.on, bright=look.bright, rgb=list(look.rgb), white=look.kelvin, mix=mix, scene=scene, what=look.what())
        running = self.mode is not None
        snap.update(mode=self.mode_name if running else "", mode_starts=self.mode_starts if running else 0,
                    mode_ends=self.mode_ends if running else 0, feel=self.feel,
                    timer=self.timer_due if self.timer is not None else 0)
        return snap

    def status_text(self) -> str:
        parts = [self.look.describe() if self.look else "state not read yet"]
        if self.mode_name:
            parts.append(f"{self.mode_name} running" + (f" ({self.mode_note})" if self.mode_note else ""))
        if self.timer_note:
            parts.append(self.timer_note)
        return "; ".join(parts)

    async def dispatch(self, req: dict, say) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            if self.mode is None:
                await self.read()
            else:
                await self.current()
            note = ""
            with contextlib.suppress(OSError):
                note = PROBLEM.read_text().strip()
                if note:
                    PROBLEM.write_text("")
            return {"ok": True, "text": self.status_text() + (f"\nNote: {note}" if note else ""), "look": asdict(self.look)}

        if cmd == "find":
            # For setting the tool up with another bulb: list nearby Govee lights with the two
            # values light.py pins (NAME and ADDRESS). Listens only; connects to nothing.
            try:
                found = await asyncio.wait_for(BleakScanner.discover(timeout=10, return_adv=True), 50)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "macOS has not allowed Light to use Bluetooth yet. Answer the prompt on screen, or allow Light "
                    "under System Settings, Privacy & Security, Bluetooth. Then try again."
                ) from None
            rows = []
            for device, adv in found.values():
                name = adv.local_name or device.name or ""
                if name.startswith(("GVH", "Govee", "ihoment", "GBK")):
                    rows.append((adv.rssi, name, device.address))
            if not rows:
                return {"ok": False, "text": "No Govee light answered in 10 seconds. Check that it has power and is close, then try again."}
            lines = [f"  {name:<22} {address}   signal {rssi}" + ("   (the lamp this tool controls now)" if address.upper() == proto.ADDRESS else "")
                     for rssi, name, address in sorted(rows, reverse=True)]
            return {"ok": True, "text": "Govee lights heard nearby, strongest signal first:\n" + "\n".join(lines)
                    + "\nTo control one, put its name and identifier in NAME and ADDRESS at the top of light.py."}

        if cmd == "peek":  # what the helper knows, without touching the lamp
            return {"ok": True, "text": ""}

        if cmd == "looks":
            looks = []
            for name, (what, look, bright) in warm.PRESETS.items():
                lk = preset_look(look, bright)
                looks.append({"name": name, "rgb": list(lk.rgb), "kelvin": look if isinstance(look, int) else 0, "bright": bright, "saved": False, "what": what})
            for name, v in sorted(load_user_presets().items()):
                with contextlib.suppress(KeyError, TypeError, ValueError):
                    looks.append({"name": name, "rgb": [int(c) for c in v["rgb"]], "kelvin": int(v.get("kelvin") or 0), "bright": int(v["bright"]), "saved": True, "what": ""})
            return {"ok": True, "text": "", "looks": looks}

        if cmd == "forget":
            data = load_user_presets()
            if req.get("name") not in data:
                return {"ok": False, "text": f"There is no saved look called {req.get('name')}"}
            del data[req["name"]]
            USER_PRESETS.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            return {"ok": True, "text": f"Forgot {req['name']}"}

        if cmd == "live":
            # A slider being dragged: follow it at once, unverified. The panel sends an ordinary
            # `look` request when the drag ends, and that one is verified.
            await self.current()
            colour = req.get("kelvin") or req.get("rgb")
            if self.mode is not None and (colour or self.mode_name != "candle"):
                await self.stop_mode(tidy=False)
            if req.get("kelvin"):
                k = max(warm.MIX_MIN_K, min(warm.WHITE_MAX_K, int(req["kelvin"])))
                await (self.paint_white(k) if k >= warm.WHITE_MIN_K else self.paint(warm.tint(k)))
            elif req.get("rgb"):
                await self.paint(req["rgb"])
            if req.get("bright") is not None:
                await self.dim(req["bright"])
            return {"ok": True, "text": ""}

        if cmd == "candle_tune":
            feel = req.get("feel")
            if feel not in FEELS or self.mode_name != "candle" or self.flame is None:
                return {"ok": False, "text": "No candle is running"}
            self.feel = feel
            self.flame.depth, self.flame.calm = FEELS[feel]
            return {"ok": True, "text": f"Candle is now {feel}"}

        if cmd == "stop":
            name = await self.stop_mode(tidy=True)
            had_timer = self.timer is not None
            if self.timer is not None:
                self.timer.cancel()
                self.timer, self.timer_note = None, ""
                self._keep_awake()
            if not name and not had_timer:
                return {"ok": True, "text": "Nothing was running. " + (await self.read()).describe()}
            what = " and ".join(x for x in (name, "the off timer" if had_timer else "") if x)
            return {"ok": True, "text": f"Stopped {what}. Now {(await self.read()).describe()}"}

        if cmd == "quit":
            await self.stop_mode(tidy=True)
            self.done.set()
            return {"ok": True, "text": "Bluetooth link released. The next light command reconnects."}

        if cmd == "look":
            # A brightness change alone does not interrupt a candle: the flame only drives colour.
            only_bright = req.get("bright") is not None and not any(req.get(k) for k in ("rgb", "kelvin", "power"))
            if only_bright and self.mode_name == "candle":
                await self.set_bright(req["bright"])
                self.mode_note = re.sub(r"^\d+%", f"{req['bright']}%", self.mode_note)
                return {"ok": True, "text": f"Candle now at {req['bright']}%"}
            await self.stop_mode(tidy=False)
            cur = await self.remember()
            if req.get("power") == "off":
                await self.apply(replace(cur, on=False))
                return {"ok": True, "text": "off", "look": asdict(self.look)}
            # After a sunset the bulb itself remembers a faint red. Waking it, by any
            # look command, starts from the look saved before the sunset instead.
            resume = self._take_resume()
            target = resume if resume is not None and not cur.on else replace(cur, on=True)
            if req.get("rgb"):
                target = replace(target, rgb=tuple(req["rgb"]), kelvin=0, scene=0)
            if req.get("kelvin"):
                k = int(req["kelvin"])
                target = replace(target, kelvin=k if k >= warm.WHITE_MIN_K else 0, rgb=warm.tint(k), scene=0)
            if req.get("bright") is not None:
                target = replace(target, bright=int(req["bright"]))
            await self.apply(target)
            return {"ok": True, "text": self.look.describe(), "look": asdict(self.look)}

        if cmd == "toggle":
            cur = await self.read()
            return await self.dispatch({"cmd": "look", "power": "off" if cur.on else "on"}, say)

        if cmd == "nudge":
            cur = await self.current() if self.mode_name == "candle" else await self.read()
            what = req["what"]
            if what in ("up", "down"):
                factor = 1.4 if what == "up" else 1 / 1.4
                value = max(1, min(100, round(cur.bright * factor) if round(cur.bright * factor) != cur.bright else cur.bright + (1 if what == "up" else -1)))
                return await self.dispatch({"cmd": "look", "bright": value}, say)
            now_k = cur.kelvin or warm.kelvin_of(cur.rgb)
            new_k = warm.step_kelvin(None if cur.scene else now_k, what == "warmer")
            step = {"cmd": "look", "kelvin": new_k}
            # Real white is about five times stronger than the colour mixes at the same
            # brightness number (measured), so crossing 2700K adjusts brightness to match.
            was_white, now_white = bool(cur.kelvin), new_k >= warm.WHITE_MIN_K
            if cur.on and not cur.scene and was_white != now_white:
                step["bright"] = max(1, min(100, round(cur.bright / 5 if now_white else cur.bright * 5)))
            reply = await self.dispatch(step, say)
            if "bright" in step and reply.get("ok"):
                reply["text"] += f"\nBrightness went from {cur.bright}% to {step['bright']}% to match: real white is about 5 times stronger than the colour mixes."
            return reply

        if cmd == "back":
            await self.stop_mode(tidy=False)
            if self.previous is None:
                return {"ok": False, "text": "There is no earlier look to go back to yet"}
            target, self.previous = self.previous, await self.current()
            await self.apply(target)
            return {"ok": True, "text": self.look.describe(), "look": asdict(self.look)}

        if cmd == "scene":
            await self.stop_mode(tidy=False)
            await self.remember()
            RESUME.write_text("")
            await self.set_scene(req["name"])
            if req.get("bright") is not None:
                await self.set_bright(req["bright"])
            await self.set_power(True)
            return {"ok": True, "text": self.look.describe(), "look": asdict(self.look)}

        if cmd == "candle":
            await self.stop_mode(tidy=False)
            cur = await self.remember()
            RESUME.write_text("")
            base = tuple(req.get("rgb") or (255, 120, 10))
            bright = req.get("bright") or (cur.bright if cur.on else 30)
            await self.apply(Look(True, bright, base))
            seconds = float(req.get("seconds") or 0)
            note = f"{bright}%" + (f", then off at {time.strftime('%H:%M', time.localtime(time.time() + seconds))}" if seconds else "")
            feel = req.get("feel") if req.get("feel") in FEELS else None
            depth, calm = FEELS[feel] if feel else (req.get("depth", 0.5), req.get("calm", 0.5))
            self.feel = feel or next((n for n, v in FEELS.items() if v == (depth, calm)), "normal")
            self.flame = warm.Flame(base=base, depth=depth, calm=calm)  # made here so it can be retuned at once
            await self.start_mode("candle", note, self.candle(self.flame, seconds, bool(seconds)), ends=time.time() + seconds if seconds else 0)
            return {"ok": True, "text": f"Candle running at {note}. Stop it with: light stop"}

        if cmd == "breathe":
            await self.stop_mode(tidy=False)
            cur = await self.remember()
            resume = self._take_resume()
            if not cur.on:  # breathe from, and return to, the look the lamp would wake to
                cur = resume if resume is not None else replace(cur, on=True)
                await self.apply(cur)
            base = cur.rgb if not cur.kelvin and not cur.scene else (255, 110, 10)
            before = cur
            if cur.kelvin or cur.scene:
                await self.apply(Look(True, cur.bright, base))
            seconds = float(req.get("seconds") or 300)
            await self.start_mode("breathe", f"until {time.strftime('%H:%M', time.localtime(time.time() + seconds))}", self.breathe(before, base, seconds), ends=time.time() + seconds)
            return {"ok": True, "text": f"Breathing light for {fmt_span(seconds)}: in while it rises (4 s), out while it falls (6 s). Stop it with: light stop"}

        if cmd == "sunset":
            await self.stop_mode(tidy=False)
            cur = await self.remember()
            if not cur.on:
                return {"ok": False, "text": "The lamp is off. Turn it on first, then start the sunset."}
            start = cur
            if cur.kelvin or cur.scene:  # move from white light or a scene to a plain colour first
                start = replace(cur, rgb=warm.tint(cur.kelvin) if cur.kelvin else (255, 168, 30), kelvin=0, scene=0)
                await self.apply(start)
            seconds = float(req.get("seconds") or 1800)
            ends = time.strftime("%H:%M", time.localtime(time.time() + seconds))
            await self.start_mode("sunset", f"off at {ends}", self.sunset(start, seconds, resume=cur), ends=time.time() + seconds)
            return {"ok": True, "text": f"Sunset over {fmt_span(seconds)}: fading to deep red, off at {ends}. Afterwards, light on brings back today's look. Stop it with: light stop"}

        if cmd == "sunrise":
            await self.stop_mode(tidy=False)
            await self.remember()
            seconds = float(req.get("seconds") or 1200)
            wait = float(req.get("wait") or 0)
            starts = time.strftime("%H:%M", time.localtime(time.time() + wait))
            full = time.strftime("%H:%M", time.localtime(time.time() + wait + seconds))
            await self.start_mode("sunrise", f"starts {starts}, full light {full}", self.sunrise(wait, seconds, int(req.get("white_at", 20))),
                                  starts=time.time() + wait, ends=time.time() + wait + seconds)
            text = f"Sunrise over {fmt_span(seconds)}: deep red to warm white, full light at {full}."
            if wait > 60:
                text += " This Mac has to stay awake and in range until then (lid open, plugged in)."
            return {"ok": True, "text": text + " Stop it with: light stop"}

        if cmd == "pulse":
            await self.stop_mode(tidy=True)
            cur = await self.read()
            await self.start_mode("pulse", "", self.pulse(cur, tuple(req["rgb"]), int(req.get("times", 2)), float(req.get("swell", 0.5))))
            return {"ok": True, "text": "", "follow": True}

        if cmd == "tour":
            await self.stop_mode(tidy=True)
            cur = await self.read()
            self.previous = cur
            await self.start_mode("tour", "", self.tour(cur, say))
            return {"ok": True, "text": f"Tour finished. {'' if cur.on else 'Lamp is off again. '}", "follow": True}

        if cmd == "check":
            await self.stop_mode(tidy=True)
            before = await self.read()
            say(f"start: {before.describe()}")
            try:
                await self.set_power(False)
                say("off: verified")
                await asyncio.sleep(1.5)
                await self.set_power(True)
                say("on: verified")
                await self.set_bright(40)
                say("brightness 40%: verified")
                await asyncio.sleep(1)
                await self.set_rgb((255, 136, 0))
                say("colour #ff8800: verified")
                await asyncio.sleep(1)
                await self.set_kelvin(2700)
                say("white 2700K: verified")
                await asyncio.sleep(1)
            finally:
                await self.apply(replace(before, on=True), glide=0)
                if not before.on:
                    await self.set_power(False)
            after = await self.read()
            same = (after.on, after.bright, after.kelvin, after.scene) == (before.on, before.bright, before.kelvin, before.scene) and (before.kelvin or before.scene or after.rgb == before.rgb)
            return {"ok": bool(same), "text": ("Restored: " if same else "Could not fully restore. Now: ") + after.describe()}

        if cmd == "timer":
            had = self.timer is not None
            if self.timer is not None:
                self.timer.cancel()
                self.timer, self.timer_note = None, ""
                self._keep_awake()
            seconds = float(req["seconds"])
            if seconds <= 0:  # the panel's "No timer"
                return {"ok": True, "text": "Off timer cancelled" if had else "No off timer was set"}
            at = time.strftime("%H:%M", time.localtime(time.time() + seconds))
            self.timer_note = f"off at {at}"
            self.timer_due = time.time() + seconds
            self.timer = asyncio.create_task(self.off_later(seconds))
            self._keep_awake()
            return {"ok": True, "text": f"The lamp goes off at {at} (in {fmt_span(seconds)}). This Mac has to stay awake and in range. Cancel with: light stop"}

        if cmd == "save":
            name = str(req.get("name", ""))
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", name) or name in warm.PRESETS:
                return {"ok": False, "text": "Use a name of your own made of small letters, digits and dashes, starting with a letter"}
            cur = await self.read()
            if cur.scene:
                return {"ok": False, "text": "A scene cannot be saved as a look. Use: light scene NAME"}
            data = load_user_presets()
            data[req["name"]] = {"rgb": list(cur.rgb), "kelvin": cur.kelvin, "bright": cur.bright}
            USER_PRESETS.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            return {"ok": True, "text": f"Saved {req['name']}: {cur.bright}%, {cur.what()}. Use it with: light {req['name']}"}

        return {"ok": False, "text": f"Unknown request {cmd!r}"}

    def _take_resume(self) -> Look | None:
        try:
            text = RESUME.read_text().strip()
            RESUME.write_text("")
            return Look.from_json(json.loads(text)) if text else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # --------------------------------------------------------------- server

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.last_request = time.monotonic()

        def send(obj) -> None:
            with contextlib.suppress(Exception):
                writer.write((json.dumps(obj) + "\n").encode())

        reply: dict
        try:
            req = json.loads(await asyncio.wait_for(reader.readline(), 5))
            log.info("request %s", {k: v for k, v in req.items() if k != "version"})
            if req.get("version") != code_version():
                async with self.cmd:
                    await self.stop_mode(tidy=True)
                send({"restart": True})
                self.done.set()
                return
            async with self.cmd:
                reply = await self.dispatch(req, lambda text: send({"say": text}))
                follow = self.mode if reply.pop("follow", False) else None
            if follow is not None:
                # Stay with a short foreground mode until it ends; if the person
                # presses Ctrl-C the connection closes and the mode is stopped.
                gone = asyncio.create_task(reader.read(1))
                await asyncio.wait({follow, gone}, return_when=asyncio.FIRST_COMPLETED)
                if not follow.done():
                    async with self.cmd:
                        await self.stop_mode(tidy=True)
                    reply = {"ok": True, "text": "Stopped. "}
                elif follow.cancelled():
                    reply = {"ok": True, "text": "Stopped by another light command. "}
                gone.cancel()
                problem = ""
                with contextlib.suppress(OSError):
                    problem = PROBLEM.read_text().strip()
                if problem:
                    PROBLEM.write_text("")
                    reply = {"ok": False, "text": problem}
                elif self.look is not None and req.get("cmd") != "pulse":  # a pulse in a script stays quiet
                    reply["text"] = (reply.get("text", "") + "Now " + self.look.describe()).strip()
        except RuntimeError as exc:
            reply = {"ok": False, "text": str(exc)}
        except (asyncio.TimeoutError, ValueError) as exc:
            reply = {"ok": False, "text": f"light and its helper are out of step ({type(exc).__name__}). Try again."}
        except Exception as exc:  # noqa: BLE001 - last resort: say it and log the details
            log.exception("request failed")
            reply = {"ok": False, "text": f"Unexpected problem ({type(exc).__name__}: {exc}). Details are in work/agent.log"}
        log.info("reply ok=%s %s", reply.get("ok"), str(reply.get("text", ""))[:120])
        reply["ui"] = self.snapshot()
        send(reply)
        with contextlib.suppress(Exception):
            await writer.drain()
            writer.close()
        self.last_request = time.monotonic()

    async def watchdog(self) -> None:
        """Let go of the link when nothing is happening, and exit when idle."""
        while not self.done.is_set():
            await asyncio.sleep(2)
            now = time.monotonic()
            if self.cmd.locked():
                continue
            quiet = now - max(self.last_io, self.last_request)
            if quiet > IDLE_SECONDS:
                if self.mode is None and self.timer is None:
                    self.done.set()
                elif self.lamp is not None:
                    await self.disconnect()  # waiting for a timer: free the bulb for the Govee app meanwhile

    async def main(self) -> int:
        WORK.mkdir(exist_ok=True)
        lock = (WORK / "agent.lock").open("a")
        for attempt in range(60):  # an older helper may still be finishing; wait up to 15 seconds for it
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if os.path.exists(WORK / SOCKET_NAME) and attempt > 8:
                    pass
                await asyncio.sleep(0.25)
        else:
            return 0  # another helper is serving
        os.chdir(WORK)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(SOCKET_NAME)
        server = await asyncio.start_unix_server(self.serve, path=SOCKET_NAME)
        os.chmod(SOCKET_NAME, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.done.set)
        log.info("helper started, version %s", code_version())
        watch = asyncio.create_task(self.watchdog())
        await self.done.wait()
        server.close()
        watch.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.stop_mode(tidy=True), 10)
        if self.timer is not None:
            self.timer.cancel()
        if self.awake is not None:
            self.awake.terminate()
        await self.disconnect()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(SOCKET_NAME)
        log.info("helper stopped")
        return 0


async def known_device():
    """The pinned lamp as macOS already knows it, without scanning. None if macOS
    has no record of it or the record carries a different name."""
    from bleak.backends.corebluetooth.CentralManagerDelegate import CentralManagerDelegate
    from bleak.backends.device import BLEDevice
    from Foundation import NSUUID, NSArray

    manager = CentralManagerDelegate()
    await manager.wait_until_ready()
    ident = NSUUID.alloc().initWithUUIDString_(proto.ADDRESS)
    found = manager.central_manager.retrievePeripheralsWithIdentifiers_(NSArray.arrayWithObject_(ident))
    if not found or len(found) == 0 or str(found[0].name() or "") != proto.NAME:
        return None
    return BLEDevice(proto.ADDRESS, proto.NAME, (found[0], manager))


# How a candle feels: (depth, calm) for warm.Flame. The command line uses the same three.
FEELS = {"calm": (0.35, 0.9), "normal": (0.5, 0.5), "lively": (0.8, 0.1)}


def preset_look(look, bright: int) -> Look:
    """A preset's colour ("#rrggbb") or Kelvin (int) as a Look."""
    if isinstance(look, int):
        return Look(True, bright, warm.tint(look), look if look >= warm.WHITE_MIN_K else 0)
    return Look(True, bright, tuple(bytes.fromhex(look.lstrip("#"))))


def load_user_presets() -> dict:
    try:
        data = json.loads(USER_PRESETS.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def fmt_span(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} minutes"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d} min"


def run() -> int:
    WORK.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(WORK / "agent.log", maxBytes=512_000, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("bleak").setLevel(logging.WARNING)

    async def go() -> int:
        return await Agent().main()

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(run())
