"""Warm looks for the H6008: Kelvin, named presets, built-in scenes, candle flicker.

Pure data and maths. Nothing here touches Bluetooth; light.py sends the frames.

Sources (govee-ble-local 1.10.0, MIT, see THIRD_PARTY_LICENSE.txt):
  wire/cct.py          the Govee app's own Kelvin-to-colour table
  wire/build.py        the 0x0d white frame and the 0xA3 scene upload
  scenes/store.json    the scenes this bulb model ships with
  _generated/devices.py  this model's white LEDs cover 2700 K to 6500 K
"""
from __future__ import annotations

import math
import random

WHITE_MIN_K, WHITE_MAX_K = 2700, 6500  # what the bulb's white LEDs can make
MIX_MIN_K = 1000  # below WHITE_MIN_K the colour LEDs imitate the temperature

# The colour the Govee app pairs with each colour temperature (first entry per step).
_TINT = {
    1000: "ff3300", 1100: "ff4500", 1200: "ff5200", 1300: "ff5d00", 1400: "ff6600", 1500: "ff6f00", 1600: "ff7600",
    1700: "ff7c00", 1800: "ff8200", 1900: "ff8700", 2000: "ff8d0b", 2100: "ff921d", 2200: "ff9829", 2300: "ff9d33",
    2400: "ffa23c", 2500: "ffa645", 2600: "ffaa4d", 2700: "ffae54", 2800: "ffb25b", 2900: "ffb662", 3000: "ffb969",
    3100: "ffbd6f", 3200: "ffc076", 3300: "ffc37c", 3400: "ffc682", 3500: "ffc987", 3600: "ffcb8d", 3700: "ffce92",
    3800: "ffd097", 3900: "ffd39c", 4000: "ffd5a1", 4100: "ffd7a6", 4200: "ffd9ab", 4300: "ffdbaf", 4400: "ffddb4",
    4500: "ffdfb8", 4600: "ffe1bc", 4700: "ffe2c0", 4800: "ffe4c4", 4900: "ffe5c8", 5000: "ffe7cc", 5100: "ffe8d0",
    5200: "ffead3", 5300: "ffebd7", 5400: "ffedda", 5500: "ffeede", 5600: "ffefe1", 5700: "fff0e4", 5800: "fff1e7",
    5900: "fff3ea", 6000: "fff4ed", 6100: "fff5f0", 6200: "fff6f3", 6300: "fff7f7", 6400: "fff8f8", 6500: "fff9fb",
}


def tint(kelvin: int) -> tuple[int, int, int]:
    """The app's colour for a Kelvin value, to the nearest 100 K."""
    k = min(max(int(round(kelvin / 100.0)) * 100, MIX_MIN_K), WHITE_MAX_K)
    return tuple(bytes.fromhex(_TINT[k]))


def kelvin_payload(kelvin: int) -> tuple[tuple[int, ...], bool]:
    """Arguments after opcode 05 for a colour temperature, and whether the
    white LEDs are used. At or above 2700 K: 0d <tint> <K, big endian> <tint>.
    Below: the plain colour form 0d <tint>, because the white LEDs stop at 2700 K.
    """
    if not MIX_MIN_K <= kelvin <= WHITE_MAX_K:
        raise ValueError(f"Kelvin must be {MIX_MIN_K} to {WHITE_MAX_K}")
    t = tint(kelvin)
    if kelvin >= WHITE_MIN_K:
        return (0x0D, *t, kelvin >> 8, kelvin & 0xFF, *t), True
    return (0x0D, *t), False


# ---------------------------------------------------------------- presets
# name: (what it looks like, colour "#rrggbb" or Kelvin, brightness percent)
PRESETS: dict[str, tuple[str, str | int, int]] = {
    "coals": ("deep red, very dim", "#ff1400", 5),
    "ember": ("red-orange, dim", "#ff2a00", 20),
    "dusk": ("orange-red", "#ff5010", 55),
    "amber": ("amber", "#ff7a00", 45),
    "gold": ("yellow-gold", "#ffa81e", 70),
    "glow": ("the 1800K colour mix, low", 1800, 30),
    "lamp": ("the 2200K colour mix", 2200, 60),
    "tungsten": ("2700K, the warmest real white", 2700, 70),
    "paper": ("3500K white for reading", 3500, 90),
    "day": ("5000K white, full", 5000, 100),
}

# ----------------------------------------------------------------- scenes
# Scenes stored for this bulb model: name -> (scene code, animation data).
# The animation data is uploaded first, then the scene is selected by code.
SCENES: dict[str, tuple[int, str]] = {
    "candlelight": (4024, "9308ffe9661e00ff0a00ffe9e61e00ff3200ffe9c81400c83200ffe9dc1e00ff3200ffe9981400c83200ffe9ff1400ff0a00ffe9890a00c84b00ffe9dc1e00ff3200"),
    "fire": (4022, "8304ffeb590a00ffebde0a00ffeb800a00ffebff0a0003ff1a00ff3f00ff2b00"),
    "sunset-glow": (124, "8301ffc8ff280003ff3264ff0000ff9600"),
    "dusk": (123, "8301ffc8ff280002ff0000ff7f00"),
    "desert": (122, "8301ffd2ff280002ffc800ff7f00"),
    "warm": (143, "8302ffc8282800ffc8ff280001ff3200"),
    "romantic": (4032, "8302ffe6280a00ffe6ff0a0002df1313b3149a"),
    "night-light": (2, ""),
    "reading": (13, ""),
}


def _xor(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def scene_upload_frames(data: bytes, comm: int = 0x01) -> list[bytes]:
    """Split scene animation data into a3 frames.

    Stream: 01, frame count, comm byte, then the data, cut into 17-byte pieces.
    Frame: a3, index (0, 1, 2 ... and ff for the last), piece, XOR checksum.
    There are always at least two frames.
    """
    pieces = max(2, -(-(3 + len(data)) // 17))
    if pieces > 255:
        raise ValueError("Scene data is too long")
    stream = bytes((0x01, pieces, comm)) + data
    frames = []
    for i in range(pieces):
        chunk = stream[i * 17:(i + 1) * 17].ljust(17, b"\x00")
        raw = bytes((0xA3, 0xFF if i == pieces - 1 else i)) + chunk
        frames.append(raw + bytes((_xor(raw),)))
    return frames


def scene_select_payload(code: int) -> tuple[int, int, int]:
    """Arguments after opcode 05 to select a scene: 04, code low byte, code high byte."""
    return 0x04, code & 0xFF, code >> 8


# ----------------------------------------------------------------- candle


class Flame:
    """A candle flame as a brightness level over time, 0..1.

    Three parts added together, the way a real flame behaves in still air:
      drift    slow wandering of the flame height (seconds)
      shimmer  small fast movement that is always there
      gusts    now and then the flame ducks, flutters, and recovers
    As the flame gets lower its colour gets redder.

    depth 0..1 scales how far the light moves; calm 0..1 makes gusts rarer.
    """

    def __init__(self, base=(255, 120, 10), depth=0.5, calm=0.5, seed=None):
        self.base = base
        self.depth = max(0.0, min(1.0, depth))
        self.calm = max(0.0, min(1.0, calm))
        self.rng = random.Random(seed)
        self.drift = 0.0
        self.shimmer = 0.0
        self.dip = 0.0  # current gust depth
        self.dip_target = 0.0
        self.gust_left = 0.0
        self.next_gust = self._gap()

    def _gap(self) -> float:
        mean = 4.0 + 14.0 * self.calm  # seconds between gusts
        return self.rng.expovariate(1.0 / mean) + 1.0

    @staticmethod
    def _ou(x: float, dt: float, tau: float, sigma: float, rng: random.Random) -> float:
        """One step of a mean-reverting random walk (stays near 0, never runs away)."""
        a = math.exp(-dt / tau)
        return x * a + sigma * math.sqrt(1 - a * a) * rng.gauss(0, 1)

    def level(self, dt: float) -> float:
        d = self.depth
        self.drift = self._ou(self.drift, dt, 2.2, 0.07 * d, self.rng)
        flutter = 3.0 if self.gust_left > 0 else 1.0
        self.shimmer = self._ou(self.shimmer, dt, 0.10, 0.03 * d * flutter, self.rng)
        self.next_gust -= dt
        if self.next_gust <= 0:
            self.dip_target = self.rng.uniform(0.18, 0.45) * d
            self.gust_left = self.rng.uniform(0.15, 0.6)
            self.next_gust = self._gap()
        if self.gust_left > 0:
            self.gust_left -= dt
            self.dip += (self.dip_target - self.dip) * min(1.0, dt / 0.07)  # ducks fast
        else:
            self.dip += (0.0 - self.dip) * min(1.0, dt / 0.45)  # comes back slowly
        level = 1.0 - 0.12 * d + self.drift + self.shimmer - self.dip
        return max(0.2, min(1.0, level))

    def color(self, level: float) -> tuple[int, int, int]:
        """Dimmer means redder: green and blue fall faster than red."""
        r, g, b = self.base
        warm = 0.72 + 0.28 * level
        return (
            max(1, min(255, round(r * level))),
            max(0, min(255, round(g * level * warm))),
            max(0, min(255, round(b * level * warm * warm))),
        )

    def next(self, dt: float) -> tuple[int, int, int]:
        return self.color(self.level(dt))


# ------------------------------------------------------------ colour maths

_KELVIN_OF_TINT = {v: k for k, v in _TINT.items()}
# The app's second colour for the warm steps, so a look set from the Govee app is recognised too.
_KELVIN_OF_TINT.update({"ff3800": 1000, "ff4700": 1100, "ff5300": 1200, "ff6500": 1400, "ff6d00": 1500,
                        "ff7300": 1600, "ff7900": 1700, "ff7e00": 1800, "ff8300": 1900, "ff8912": 2000,
                        "ff8e21": 2100, "ff932c": 2200, "ff9836": 2300, "ff9d3f": 2400, "ffa148": 2500,
                        "ffa54f": 2600})


def kelvin_of(rgb) -> int | None:
    """The Kelvin value a colour stands for in the app's table, if it is one."""
    return _KELVIN_OF_TINT.get(bytes(rgb).hex())


def mix(a, b, f: float) -> tuple[int, int, int]:
    """Colour between a and b; f=0 gives a, f=1 gives b."""
    f = max(0.0, min(1.0, f))
    return tuple(round(x + (y - x) * f) for x, y in zip(a, b))


def scale(rgb, level: float) -> tuple[int, int, int]:
    """The same colour, dimmer. Darker colour values dim this bulb smoothly."""
    level = max(0.0, min(1.0, level))
    return tuple(round(c * level) for c in rgb)


def ease(f: float) -> float:
    """Slow start, slow finish."""
    f = max(0.0, min(1.0, f))
    return f * f * (3 - 2 * f)


def along(points, f: float) -> tuple[int, int, int]:
    """Colour at position f (0..1) along a list of colours spaced evenly."""
    if len(points) == 1:
        return tuple(points[0])
    f = max(0.0, min(1.0, f)) * (len(points) - 1)
    i = min(int(f), len(points) - 2)
    return mix(points[i], points[i + 1], f - i)


def redness(rgb) -> float:
    """0 for pure red, about 1 for white: green relative to red."""
    return rgb[1] / max(1, rgb[0])


# From yellow-white to deep red: the path the sun takes going down.
SUNSET_PATH = [(255, 174, 84), (255, 168, 30), (255, 122, 0), (255, 80, 16), (255, 42, 0), (255, 20, 0)]


def sunset_colors(start) -> list[tuple[int, int, int]]:
    """Start colour, then only the stops that are redder than it."""
    return [tuple(start)] + [c for c in SUNSET_PATH if redness(c) < redness(start) - 0.03]


def geometric(start: float, end: float, f: float) -> float:
    """Brightness that changes by equal ratios, which the eye reads as an even fade."""
    start, end = max(start, 1e-6), max(end, 1e-6)
    return start * (end / start) ** max(0.0, min(1.0, f))


# Warm to cool, for `light warmer` and `light cooler`.
LADDER = [1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400, 2700, 3000, 3500, 4000, 5000, 6500]


def step_kelvin(current: int | None, warmer: bool) -> int:
    """Next stop on the ladder. With no known Kelvin, start from a sensible warm stop."""
    if current is None:
        return 1800 if warmer else 2700
    if warmer:
        lower = [k for k in LADDER if k < current]
        return lower[-1] if lower else LADDER[0]
    higher = [k for k in LADDER if k > current]
    return higher[0] if higher else LADDER[-1]


def breath_level(t: float, inhale: float = 4.0, exhale: float = 6.0, low: float = 0.22) -> float:
    """Light level for a paced breath at time t: rises while breathing in, falls while breathing out."""
    t = t % (inhale + exhale)
    f = ease(t / inhale) if t < inhale else 1.0 - ease((t - inhale) / exhale)
    return low + (1.0 - low) * f
