"""Live check: runs the real `light` command against the real lamp, end to end.

Each step checks three things where they apply: the command's exit code, what
the bulb reports back (`light status --json`), and the MacBook's ambient light
sensor, which is independent evidence that the room actually changed. The
sensor part is skipped, and said so, when the room is too bright for the lamp
to register or the Mac has no sensor.

The lamp changes visibly for about five minutes. The look found at the start
is put back at the end.

  .venv/bin/python tests/live_check.py            about five minutes
  .venv/bin/python tests/live_check.py --long     adds two timed waits, about eight minutes
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lux  # noqa: E402

LIGHT = str(ROOT / "light")
LONG = "--long" in sys.argv  # also test waits long enough for the helper to disconnect and reconnect
results: list[tuple[bool, str, str]] = []
sensor = lux.open_sensor()
use_sensor = sensor is not None


def run(*words: str, timeout: float = 150) -> tuple[int, str]:
    p = subprocess.run([LIGHT, *words], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def look() -> dict:
    code, out = run("status", "--json")
    return json.loads(out)["look"] if code == 0 else {}


def level(seconds: float = 0.7) -> float:
    """Broadband sensor channel, averaged."""
    if not use_sensor:
        return -1.0
    time.sleep(0.5)
    vals = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r = lux.read(sensor)
        if r:
            vals.append(r[1][3])
        time.sleep(0.1)
    return sum(vals) / len(vals) if vals else -1.0


def trace(seconds: float) -> list[float]:
    vals = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r = lux.read(sensor) if use_sensor else None
        if r:
            vals.append(r[1][3])
        time.sleep(0.1)
    return vals


def lit_by_something_else(t: list[float], dark: float) -> bool:
    """True if the room never got near dark during a trace that should have dipped:
    another light came on since the start, so the sensor cannot judge this step."""
    if t and min(t) > dark + 40:
        print(f"      sensor step skipped: another light is on in the room (lowest reading {min(t):.0f}, dark was {dark:.0f})", flush=True)
        return True
    return False


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(("pass  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""), flush=True)
    return bool(ok)


def helper_running() -> bool:
    lock = (ROOT / "work" / "agent.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        lock.close()


def restore(before: dict) -> None:
    if before.get("scene"):
        import warm
        name = next((n for n, (c, _) in warm.SCENES.items() if c == before["scene"]), None)
        if name:
            run("scene", name, str(before["bright"]))
    elif before.get("kelvin"):
        run(f"{before['kelvin']}k", str(before["bright"]))
    else:
        run(bytes(before["rgb"]).hex(), str(before["bright"]))
    if not before.get("on"):
        run("off")


def main() -> int:
    global use_sensor
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    code, out = run("status")
    if not check("the lamp answers", code == 0, out):
        print("\nThe lamp did not answer, so nothing else was tried.")
        return 1
    before = look()
    print(f"      found: {out}")
    try:
        # -- power, and whether the sensor can see this lamp at all
        code, out = run("off")
        dark = level()
        check("off: bulb reports off", code == 0 and look().get("on") is False, out)
        code, out = run("tungsten", "100")
        white = level()
        check("tungsten 100: bulb reports white 2700K at 100%", code == 0 and look().get("kelvin") == 2700 and look().get("bright") == 100, out)
        if use_sensor and white - dark < 10:
            use_sensor = False
            print(f"      light sensor skipped: the lamp barely registers (off {dark:.0f}, full white {white:.0f})")
        if use_sensor:
            check("sensor: the room is brighter with the lamp on", white > dark + 10, f"off {dark:.0f}, on {white:.0f}")
            run("50")
            half = level()
            check("sensor: 50% sits between off and 100%", dark < half < white, f"{dark:.0f} < {half:.0f} < {white:.0f}")

        # -- looks
        code, out = run("amber")
        amber = level()
        lk = look()
        check("amber: bulb reports #ff7a00 at 45%", code == 0 and lk.get("rgb") == [255, 122, 0] and lk.get("bright") == 45, out)
        if use_sensor:
            check("sensor: amber is dimmer than white and brighter than off", dark < amber < white, f"{amber:.0f}")
        code, out = run("2000k", "30")
        check("2000k 30: bulb reports the 2000K colour mix at 30%", code == 0 and look().get("rgb") == [255, 141, 11] and "2000K colour mix" in out, out)
        code, out = run("#ff8800", "40")
        check('"#ff8800" 40: bulb reports that colour', code == 0 and look().get("rgb") == [255, 136, 0], out)
        run("up")
        up = look().get("bright")
        run("down")
        run("down")
        down = look().get("bright")
        check("up and down move the brightness", up > 40 > down, f"40 -> {up} -> {down}")
        run("2200k")
        run("warmer")
        warmer = look()
        run("cooler")
        run("cooler")
        cooler = look()
        check("warmer and cooler step along the scale", warmer.get("rgb") == [255, 141, 11] and cooler.get("rgb") == [255, 162, 60], f"{warmer.get('rgb')} then {cooler.get('rgb')}")
        run("ember")
        run("gold")
        code, out = run("back")
        check("back returns to the look before", code == 0 and look().get("rgb") == [255, 42, 0], out)
        run("1900k", "35")
        code, _ = run("save", "live-check-look")
        run("gold")
        code2, out = run("live-check-look")
        check("save, then use the saved look", code == 0 and code2 == 0 and look().get("rgb") == [255, 135, 0] and look().get("bright") == 35, out)
        code, _ = run("forget", "live-check-look")
        code2, out = run("live-check-look")
        check("forget removes it", code == 0 and code2 == 2, out)

        # -- wrong input changes nothing
        snapshot = look()
        code, out = run("embr")
        check("an unknown word exits 2 and changes nothing", code == 2 and look() == snapshot, out)

        # -- candle
        code, out = run("candle", "60")
        t = trace(8)
        code2, st = run("status")
        check("candle: starts and reports running", code == 0 and "candle running" in st, st)
        if use_sensor:
            check("sensor: the candle flickers", len(set(t)) >= 3, f"{len(set(t))} distinct levels, {min(t):.0f} to {max(t):.0f}")
        code, out = run("25")
        check("brightness changes while the candle runs", code == 0 and "Candle now at 25%" in out, out)
        code, out = run("stop")
        lk = look()
        check("stop: candle ends, lamp steady in the flame colour", code == 0 and lk.get("rgb") == [255, 120, 10] and "candle running" not in run("status")[1], out)

        # -- breathe
        run("amber", "60")
        code, out = run("breathe", "20s")
        t = trace(19)
        time.sleep(3.5)
        check("breathe: runs and returns to the look before", code == 0 and look().get("rgb") == [255, 122, 0] and look().get("bright") == 60, out.split(":")[0])
        if use_sensor and not lit_by_something_else(t, dark):
            check("sensor: the light rises and falls", max(t) >= 2 * max(1.0, min(t)), f"{min(t):.0f} to {max(t):.0f}")

        # -- sunset and resume
        run("gold")
        code, out = run("sunset", "12s")
        t = trace(13)
        time.sleep(2)
        lk = look()
        check("sunset: ends off at 1% deep red", code == 0 and lk.get("on") is False and lk.get("bright") == 1 and lk.get("rgb") == [255, 20, 0], str(lk))
        if use_sensor and not lit_by_something_else(t, dark):
            t = t[10:]  # the first second still shows the glide into the starting look
            falling = all(b <= a + 1 for a, b in zip(t, t[1:]))
            check("sensor: the sunset only gets dimmer, down to dark", falling and t[-1] <= dark + 1, f"{t[0]:.0f} to {t[-1]:.0f}")
        code, out = run("on")
        check("on after a sunset brings back the look from before", code == 0 and look().get("rgb") == [255, 168, 30] and look().get("bright") == 70, out)

        # -- sunrise
        run("off")
        code, out = run("sunrise", "14s")
        t = trace(16)
        time.sleep(1.5)
        lk = look()
        check("sunrise: ends at white 2700K, 100%", code == 0 and lk.get("kelvin") == 2700 and lk.get("bright") == 100 and lk.get("on"), str(lk))
        if use_sensor:
            check("sensor: the sunrise ends far brighter than it starts", t[-1] > t[0] + 10 or t[-1] > min(t) + 10, f"{min(t):.0f} to {t[-1]:.0f}")

        # -- scenes stored in the bulb
        code, out = run("scene", "candlelight", "60")
        check("scene candlelight: bulb reports the scene", code == 0 and look().get("scene") == 4024, out)
        code, out = run("scene", "fire")
        t = trace(6)
        check("scene fire: bulb reports the scene", code == 0 and look().get("scene") == 4022, out)
        if use_sensor:
            check("sensor: the fire scene moves by itself", len(set(t)) >= 3, f"{len(set(t))} distinct levels")

        # -- timer
        run("amber")
        code, out = run("off", "in", "6s")
        st = run("status")[1]
        time.sleep(9)
        check("off in 6s: shown in status, then the lamp goes off", code == 0 and "off at" in st and look().get("on") is False, out.split(".")[0])

        if LONG:
            # Long waits make the helper drop the Bluetooth link and reconnect when the time comes.
            run("amber")
            code, out = run("off", "in", "70s")
            time.sleep(76)
            check("off in 70s: the helper reconnects and switches off", code == 0 and look().get("on") is False, out.split(".")[0])
            code, out = run("sunrise", "12s", "in", "70s")
            time.sleep(60)
            still_off = look().get("on") is False
            time.sleep(22)
            for _ in range(60):  # the start can run late if a Bluetooth scan misses the lamp
                if "sunrise running" not in run("status")[1]:
                    break
                time.sleep(2)
            lk = look()
            check("sunrise in 70s: waits dark, then ends at white 2700K, 100%", code == 0 and still_off and lk.get("kelvin") == 2700 and lk.get("bright") == 100, str(lk))
            run("amber")
            run("off")

        # -- pulse, with the lamp off and on
        code, out = run("pulse", "ok")
        check("pulse on an off lamp: ends off, look intact", code == 0 and look().get("on") is False and look().get("rgb") == [255, 122, 0], out)
        run("on")
        snapshot = look()
        code, out = run("pulse", "fail")
        check("pulse on a lit lamp: look unchanged afterwards", code == 0 and look() == snapshot, out)

        # -- the built-in check and the tour
        code, out = run("check")
        check("light check passes", code == 0 and "Restored" in out, out.splitlines()[-1] if out else "")
        code, out = run("tour", timeout=240)
        check("light tour runs through and restores", code == 0 and "Tour finished" in out and look() == snapshot, out.splitlines()[-1] if out else "")
    finally:
        restore(before)
    after = look()
    check("the look found at the start is back", after == before, f"{after}")

    # -- the helper lets go
    code, out = run("quit")
    time.sleep(2)
    check("quit: the helper exits and releases Bluetooth", code == 0 and not helper_running(), out)
    run("status")
    check("the next command starts the helper again", helper_running())
    print("      waiting 55 seconds to see the helper leave by itself when idle")
    time.sleep(55)
    check("idle: the helper exits by itself", not helper_running())

    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed, started {started}, finished {time.strftime('%H:%M:%S')}."
          + ("" if use_sensor else " Light sensor checks were skipped."))
    for _, name, detail in failed:
        print(f"  failed: {name}   [{detail}]")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
