"""The `light` command. Turns plain words into one request for the helper
(agent.py, running inside Light.app) and prints what the lamp now shows.

This file never touches Bluetooth, so it runs from any terminal or script.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import warm

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
USER_PRESETS = ROOT / "presets.json"
APP = next((p for p in (ROOT / "Light.app", ROOT.parent / "Light.app") if p.exists()), ROOT / "Light.app")

RESERVED = {
    "on", "off", "toggle", "up", "down", "brighter", "dimmer", "warmer", "cooler", "status", "stop", "quit",
    "release", "back", "undo", "list", "looks", "presets", "scenes", "help", "scene", "candle", "breathe", "sunset",
    "sunrise", "pulse", "ok", "fail", "tour", "save", "forget", "check", "test", "bright", "color", "colour",
    "kelvin", "preset", "in", "at", "for", "calm", "lively", "gui", "menu",
}

HELP = """light: your Govee lamp (GVH6008DBA0) from the terminal

Everyday
  light                     what the lamp is doing now
  light on | off | toggle
  light 40                  brightness, 1 to 100
  light up | down           a step brighter or dimmer
  light warmer | cooler     a step along the warm-to-cool scale
  light back                the look you had before the last change

Looks (combine freely, in any order)
  light amber               a named look; `light list` shows them all
  light ember 10            a named look at your own brightness
  light 2000k               colour temperature, 1000 to 6500
  light "#ff8800"           any colour (ff8800 without the # works too)
  light 1900k 25
  light save reading-nook   save the current look under your own name
  light forget reading-nook

Moving light
  light candle              a flame flicker; `light 20` changes its brightness while it runs
  light candle calm | lively
  light candle for 45m      flicker, then off
  light breathe             paced breathing for 5 minutes: in as it rises, out as it falls
  light breathe 10
  light sunset              fade to deep red and off over 30 minutes
  light sunset 45
  light sunrise 20 at 7:00  deep red to warm white over 20 minutes, full light at 7:20
  light scene candlelight   an animation stored in the bulb; runs without this Mac
  light tour                about a minute through everything, then back to your look
  light stop                stop any of these and leave the lamp steady

The menu bar panel
  light gui                 open the panel under the lamp icon: sliders, looks, moving light
                            While Lamp is running, Control-Option-Shift-L opens it from anywhere.

Timers and scripts
  light off in 30m          also: light off at 23:30
  light pulse ok            two soft gold swells, then back to your look
  light pulse fail          two slow red ones.  Example: make test && light pulse ok || light pulse fail
  light check               switch, dim and recolour briefly, verify each step, restore
  light status --json       the current look for scripts

Good to know
  Below 2700K the bulb mixes colours, because its white LEDs start at 2700K. Those warm
  mixes are much dimmer than real white.
  candle, breathe, sunset, sunrise and timers run from this Mac: it has to stay awake and in
  Bluetooth range. Scenes run in the bulb itself.
  After a sunset the bulb remembers a faint red; `light on` brings back the look from before.
  While `light` is connected the Govee app cannot connect over Bluetooth. The link is released
  45 seconds after the last command, or at once with `light quit`.
  Problems are logged in work/agent.log next to this tool.
"""


class Usage(Exception):
    pass


def user_presets() -> dict:
    try:
        data = json.loads(USER_PRESETS.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def all_looks() -> dict:
    """name -> {"rgb" | "kelvin", "bright"} for the built-in and saved looks."""
    looks = {}
    for name, (_, look, bright) in warm.PRESETS.items():
        looks[name] = {"kelvin": look, "bright": bright} if isinstance(look, int) else {"rgb": list(bytes.fromhex(look[1:])), "bright": bright}
    for name, v in user_presets().items():
        try:
            looks[name] = {"kelvin": int(v["kelvin"]), "bright": int(v["bright"])} if v.get("kelvin") else {"rgb": [int(c) for c in v["rgb"]], "bright": int(v["bright"])}
        except (KeyError, TypeError, ValueError):
            continue
    return looks


# ------------------------------------------------------------------ words


def as_bright(word: str) -> int | None:
    m = re.fullmatch(r"(\d{1,3})%?", word)
    return int(m.group(1)) if m and int(m.group(1)) <= 100 else None


def as_kelvin(word: str) -> int | None:
    m = re.fullmatch(r"(\d{4})k?", word)
    if not m:
        return None
    k = int(m.group(1))
    if not warm.MIX_MIN_K <= k <= warm.WHITE_MAX_K:
        raise Usage(f"Colour temperature goes from {warm.MIX_MIN_K} to {warm.WHITE_MAX_K}; {k} is outside that")
    return k


def as_rgb(word: str) -> list | None:
    m = re.fullmatch(r"#?([0-9a-f]{6})", word)
    return list(bytes.fromhex(m.group(1))) if m else None


def as_seconds(word: str, bare_minutes: bool = False) -> float | None:
    """30m, 45min, 1h, 1h30m, 90s. A bare number counts as minutes where that is the natural reading."""
    if bare_minutes and re.fullmatch(r"\d+(\.\d+)?", word):
        return float(word) * 60
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?:in)?)?(?:(\d+)s)?", word)
    if not m or not any(m.groups()):
        return None
    h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def until(word: str, now: dt.datetime | None = None) -> float:
    """Seconds from now until the next 7, 7:00, 07:30, 7am or 11:30pm."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", word)
    if not m:
        raise Usage(f"{word!r} is not a time. Try 7:00 or 23:30")
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half:
        if not 1 <= hour <= 12:
            raise Usage(f"{word!r} is not a time. Try 7:00 or 23:30")
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        raise Usage(f"{word!r} is not a time. Try 7:00 or 23:30")
    now = now or dt.datetime.now()
    then = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if then <= now:
        then += dt.timedelta(days=1)
    return (then - now).total_seconds()


def span(words: list, default: float, low: float, high: float, what: str) -> tuple[float, list]:
    """Take a duration off the front of `words` (bare numbers are minutes)."""
    if words and words[0] == "for":
        words = words[1:]
    if words:
        seconds = as_seconds(words[0], bare_minutes=True)
        if seconds is not None:
            if not low <= seconds <= high:
                raise Usage(f"{what} can run from {int(low)} seconds to {int(high // 3600)} hours")
            return seconds, words[1:]
    return default, words


def parse(argv: list) -> dict:
    """Words -> one request. Raises Usage with a sentence the person can act on."""
    words = [w.lower() for w in argv]
    if not words:
        return {"cmd": "status", "brief": True}
    verb, rest = words[0], words[1:]
    simple = {"status": {"cmd": "status"}, "toggle": {"cmd": "toggle"}, "stop": {"cmd": "stop"},
              "quit": {"cmd": "quit"}, "release": {"cmd": "quit"}, "back": {"cmd": "back"}, "undo": {"cmd": "back"},
              "tour": {"cmd": "tour"}, "check": {"cmd": "check"}, "test": {"cmd": "check"},
              "up": {"cmd": "nudge", "what": "up"}, "brighter": {"cmd": "nudge", "what": "up"},
              "down": {"cmd": "nudge", "what": "down"}, "dimmer": {"cmd": "nudge", "what": "down"},
              "warmer": {"cmd": "nudge", "what": "warmer"}, "cooler": {"cmd": "nudge", "what": "cooler"},
              "help": {"local": "help"}, "-h": {"local": "help"}, "--help": {"local": "help"},
              "gui": {"local": "gui"}, "menu": {"local": "gui"},
              "list": {"local": "list"}, "looks": {"local": "list"}, "presets": {"local": "list"}, "scenes": {"local": "list"}}
    if verb == "status" and rest == ["--json"]:
        return {"cmd": "status", "json": True}
    if verb in simple:
        if rest:
            raise Usage(f"`light {verb}` takes nothing after it")
        return dict(simple[verb])

    if verb == "scene":
        if not rest or rest[0] not in warm.SCENES:
            raise Usage("Scenes stored in the bulb: " + ", ".join(warm.SCENES) + ". Example: light scene candlelight")
        req = {"cmd": "scene", "name": rest[0]}
        if len(rest) == 2 and as_bright(rest[1]):
            req["bright"] = as_bright(rest[1])
        elif len(rest) > 1:
            raise Usage("After the scene name only a brightness fits. Example: light scene fire 40")
        return req

    if verb == "candle":
        req = {"cmd": "candle"}
        looks = all_looks()
        i = 0
        while i < len(rest):
            w = rest[i]
            if w == "calm":
                req.update(depth=0.35, calm=0.9)
            elif w == "lively":
                req.update(depth=0.8, calm=0.1)
            elif w == "for" and i + 1 < len(rest) and as_seconds(rest[i + 1], True) is not None:
                req["seconds"] = as_seconds(rest[i + 1], True)
                i += 1
            elif as_bright(w):
                req["bright"] = as_bright(w)
            elif as_seconds(w) is not None:
                req["seconds"] = as_seconds(w)
            elif as_rgb(w):
                req["rgb"] = as_rgb(w)
            elif w in looks and "rgb" in looks[w]:
                req["rgb"] = looks[w]["rgb"]
            else:
                raise Usage(f"{w!r} does not fit after candle. Examples: light candle, light candle 20, light candle calm, light candle for 45m")
            i += 1
        if "seconds" in req and not 10 <= req["seconds"] <= 12 * 3600:
            raise Usage("A candle can run from 10 seconds to 12 hours")
        return req

    if verb == "breathe":
        seconds, left = span(rest, 300, 20, 2 * 3600, "Breathing")
        if left:
            raise Usage("Example: light breathe, or light breathe 10 for ten minutes")
        return {"cmd": "breathe", "seconds": seconds}

    if verb == "sunset":
        seconds, left = span(rest, 1800, 10, 6 * 3600, "A sunset")
        if left:
            raise Usage("Example: light sunset, or light sunset 45 for 45 minutes")
        return {"cmd": "sunset", "seconds": seconds}

    if verb == "sunrise":
        seconds, left = span(rest, 1200, 10, 6 * 3600, "A sunrise")
        req = {"cmd": "sunrise", "seconds": seconds}
        if len(left) == 2 and left[0] == "at":
            req["wait"] = until(left[1])
        elif len(left) == 2 and left[0] == "in" and as_seconds(left[1], True) is not None:
            req["wait"] = as_seconds(left[1], True)
        elif left:
            raise Usage("Examples: light sunrise, light sunrise 20 at 7:00, light sunrise 30 in 8h")
        return req

    if verb in ("pulse", "ok", "fail"):
        kind = verb if verb != "pulse" else (rest[0] if rest else "ok")
        if verb != "pulse" and rest or len(rest) > 1:
            raise Usage("Examples: light pulse ok, light pulse fail, light pulse ff8800")
        if kind == "ok":
            return {"cmd": "pulse", "rgb": [255, 176, 40], "times": 2, "swell": 0.45}
        if kind == "fail":
            return {"cmd": "pulse", "rgb": [255, 16, 0], "times": 2, "swell": 0.9}
        if as_rgb(kind):
            return {"cmd": "pulse", "rgb": as_rgb(kind), "times": 2, "swell": 0.5}
        raise Usage("Examples: light pulse ok, light pulse fail, light pulse ff8800")

    if verb in ("save", "forget"):
        if len(rest) != 1 or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", rest[0]):
            raise Usage(f"Give one name made of letters, digits and dashes. Example: light {verb} reading-nook")
        name = rest[0]
        if name in RESERVED or name in warm.PRESETS or as_rgb(name):
            raise Usage(f"{name!r} already means something to light. Pick another name")
        return {"cmd": "save", "name": name} if verb == "save" else {"local": "forget", "name": name}

    if verb in ("bright", "color", "colour", "kelvin", "preset"):  # the older spellings still work
        if len(rest) != 1:
            raise Usage(f"Example: light {verb} " + {"bright": "40", "kelvin": "2000", "preset": "amber"}.get(verb, '"#ff8800"'))
        if verb == "kelvin" and re.fullmatch(r"\d{4}", rest[0]):
            rest = [rest[0] + "k"]
        words = rest

    return look_request(words)


def look_request(words: list) -> dict:
    looks = all_looks()
    req: dict = {"cmd": "look"}
    explicit_bright = None
    i = 0
    while i < len(words):
        w = words[i]
        if w in ("on", "off"):
            req["power"] = w
        elif w in ("in", "at") and req.get("power") == "off" and len(req) == 2 and i + 2 == len(words):
            seconds = as_seconds(words[i + 1], True) if w == "in" else until(words[i + 1])
            if seconds is None or not 5 <= seconds <= 24 * 3600:
                raise Usage("Examples: light off in 30m, light off at 23:30")
            return {"cmd": "timer", "seconds": seconds}
        elif as_bright(w) is not None:
            explicit_bright = as_bright(w)
        elif as_kelvin(w) is not None:
            req["kelvin"] = as_kelvin(w)
            req.pop("rgb", None)
        elif as_rgb(w) is not None:
            req["rgb"] = as_rgb(w)
            req.pop("kelvin", None)
        elif w in looks:
            req.pop("rgb", None)
            req.pop("kelvin", None)
            req.update(looks[w])
        else:
            close = [n for n in list(looks) + sorted(RESERVED) if n.startswith(w[:3])][:4]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise Usage(f"No look or command is called {w!r}.{hint} `light help` shows everything; `light list` shows the looks")
        i += 1
    if explicit_bright is not None:
        if explicit_bright == 0:
            return {"cmd": "look", "power": "off"}
        req["bright"] = explicit_bright
    if req.get("power") == "off" and len(req) > 2:
        raise Usage("`off` does not combine with a look. Example: light off, or light off in 30m")
    return req


# ----------------------------------------------------------------- output


def colour_ok() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def swatch(rgb, level: float = 1.0) -> str:
    if not colour_ok():
        return ""
    r, g, b = (int(c * (0.35 + 0.65 * level)) for c in rgb)
    return f"\x1b[48;2;{r};{g};{b}m    \x1b[0m "


def print_list() -> None:
    print("Looks  (light NAME, or light NAME 30 for your own brightness)")
    for name, (what, look, bright) in warm.PRESETS.items():
        rgb = warm.tint(look) if isinstance(look, int) else bytes.fromhex(look[1:])
        print(f"  {swatch(rgb, bright / 100)}{name:<10} {bright:>3}%  {what}")
    mine = user_presets()
    if mine:
        print("Your saved looks  (light forget NAME removes one)")
        for name, v in all_looks().items():
            if name in mine:
                rgb = v.get("rgb") or warm.tint(v["kelvin"])
                what = f"white {v['kelvin']}K" if "kelvin" in v else "#" + bytes(rgb).hex()
                print(f"  {swatch(rgb, v['bright'] / 100)}{name:<10} {v['bright']:>3}%  {what}")
    print("Moving light  (light stop ends any of them)")
    print("  candle, breathe, sunset, sunrise, tour")
    print("Scenes stored in the bulb  (light scene NAME)")
    print("  " + ", ".join(warm.SCENES))


# --------------------------------------------------------------- the helper


def version() -> str:
    return str(max(int(p.stat().st_mtime) for p in ROOT.glob("*.py")))


def connect() -> socket.socket | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect("agent.sock")  # relative to WORK; see agent.py
        return s
    except OSError:
        s.close()
        return None


def helper() -> socket.socket:
    """Connect to the helper, starting it if it is not running. Gives up after 45 seconds."""
    WORK.mkdir(exist_ok=True)
    os.chdir(WORK)
    s = connect()
    if s is not None:
        return s
    if not APP.exists():
        raise RuntimeError(f"Light.app is missing next to this tool ({APP}). Run build.sh there first.")
    subprocess.run(["/usr/bin/open", "-g", "-n", str(APP), "--args", "agent"], check=False)
    start = time.monotonic()
    told = False
    while time.monotonic() - start < 45:
        time.sleep(0.1)
        s = connect()
        if s is not None:
            return s
        if not told and time.monotonic() - start > 5:
            told = True
            print("Starting the Bluetooth helper. If macOS asks, allow Light to use Bluetooth.", file=sys.stderr)
    raise RuntimeError(
        "The Bluetooth helper did not start. Open System Settings, Privacy & Security, Bluetooth and check that "
        "Light is allowed, then try again. Details: work/agent.log"
    )


def ask(req: dict) -> int:
    """Send one request, print what comes back, return the exit code."""
    slow = req["cmd"] in ("tour", "pulse", "check")
    as_json = req.pop("json", False)
    for _ in range(3):
        s = helper()
        try:
            s.settimeout(240 if slow else 90)
            s.sendall((json.dumps({**req, "version": version()}) + "\n").encode())
            for line in s.makefile("r"):
                msg = json.loads(line)
                if "say" in msg:
                    print(msg["say"], flush=True)
                    continue
                if msg.get("restart"):
                    break  # an older helper is stepping aside for this version
                text = msg.get("text", "")
                look = msg.get("look")
                if as_json:
                    print(json.dumps({"ok": msg.get("ok"), "text": text, "look": look}))
                    return 0 if msg.get("ok") else 1
                lead = swatch(look["rgb"], look["bright"] / 100) if look and look.get("on") and not look.get("scene") and "\n" not in text else ""
                if text:
                    print(lead + text, file=sys.stdout if msg.get("ok") else sys.stderr)
                return 0 if msg.get("ok") else 1
            else:
                raise RuntimeError("The Bluetooth helper stopped before answering. Try again; details in work/agent.log")
        except socket.timeout:
            raise RuntimeError("The Bluetooth helper did not answer in time. Try again; details in work/agent.log") from None
        finally:
            s.close()
        time.sleep(1.0)  # give the old helper a moment to exit
    raise RuntimeError("The Bluetooth helper keeps restarting. Details: work/agent.log")


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        req = parse(argv)
        if req.get("local") == "help":
            print(HELP, end="")
            return 0
        if req.get("local") == "list":
            print_list()
            return 0
        if req.get("local") == "gui":
            lamp_app = ROOT / "Lamp.app"
            if not lamp_app.exists():
                print(f"light: Lamp.app is missing next to this tool ({lamp_app}). Run build.sh there first.", file=sys.stderr)
                return 1
            subprocess.run(["/usr/bin/open", str(lamp_app), "--args", "--show"], check=False)
            print("The lamp panel is open under the lamp icon in the menu bar. Control-Option-Shift-L opens it from anywhere. "
                  "Right-click the icon for scenes, open at login, and quit.")
            return 0
        if req.get("local") == "forget":
            mine = user_presets()
            if req["name"] not in mine:
                print(f"light: there is no saved look called {req['name']}", file=sys.stderr)
                return 1
            del mine[req["name"]]
            USER_PRESETS.write_text(json.dumps(mine, indent=2, sort_keys=True) + "\n")
            print(f"Forgot {req['name']}")
            return 0
        brief = req.pop("brief", False)
        if req["cmd"] in ("quit", "stop"):
            WORK.mkdir(exist_ok=True)
            os.chdir(WORK)
            probe = connect()
            if probe is None:  # no helper means nothing is connected or running; do not start one to say so
                print("Nothing is connected." if req["cmd"] == "quit" else "Nothing is running.")
                return 0
            probe.close()
        code = ask(req)
        if brief and code == 0:
            print("\n  light off | on | 40 | up | down | warmer | cooler\n"
                  "  light ember | amber | gold | glow | lamp | tungsten     (light list)\n"
                  "  light candle | sunset | breathe | tour | stop\n"
                  "  light help")
        return code
    except Usage as exc:
        print(f"light: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"light: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
