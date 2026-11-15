light: your Govee lamp from the terminal

This controls one lamp: GVH6008DBA0, by its Bluetooth identifier on this Mac.
It talks to the bulb over local Bluetooth. It uses no Govee account, no cloud
and no Wi-Fi.

Everyday

  light                 what the lamp is doing now, plus a reminder of the commands
  light on | off | toggle
  light 40              brightness
  light up | down | warmer | cooler
  light ember           a named look (light list shows them with colour swatches)
  light ember 10        the same look at your own brightness
  light 2000k           colour temperature
  light "#ff8800"       any colour
  light back            the look before the last change
  light save NAME       save the current look; then light NAME brings it back

Moving light

  light candle          a flame driven from this Mac. light 20 changes its brightness.
                        Variants: light candle calm, light candle lively,
                        light candle for 45m (then off), light candle ember (redder flame)
  light breathe         a breathing pacer: in while it rises (4 s), out while it falls (6 s)
  light sunset 30       fade to deep red and off over 30 minutes
  light sunrise 20 at 7:00
  light scene candlelight   one of nine animations stored in the bulb; these run
                        without the Mac (candlelight, fire, sunset-glow, dusk, desert,
                        warm, romantic, night-light, reading)
  light tour            about a minute through the looks and modes, then back to your look
  light stop            ends any of these and leaves the lamp steady

The menu bar panel

  light gui             starts Lamp.app and opens its panel under the lamp icon in the
                        menu bar. While Lamp runs, Control-Option-Shift-L opens and closes
                        the panel from anywhere, and so does a click on the icon.
                        Esc or a click elsewhere closes it.

  In the panel: the power switch, brightness and warmth sliders (the lamp follows as you
  drag), your looks as swatches, and Candle, Breathe, Sunset and Sunrise. While one of
  those runs, the panel shows what is running, when it ends, and Stop. Along the bottom:
  Off in…, Save this look…, Tour; with the lamp off, Sunrise at… sets a start time.
  Right-click a saved look to forget it. Right-click the menu bar icon for the scenes
  stored in the bulb, Open at login, and Quit Lamp.

  The shortcut only works while Lamp is running. To have it after every restart, turn on
  Open at login in that right-click menu.

  The panel asks the lamp for its state every 1.5 seconds while it is open, which holds
  the Bluetooth link. Closed, it asks nothing, unless a mode or timer is running.

Timers and scripts

  light off in 30m      or: light off at 23:30
  light pulse ok        two soft gold swells, then back to your look
  light pulse fail      two slow red ones
                        make test && light pulse ok || light pulse fail
  light status --json   for scripts
  light check           switches, dims and recolours for a few seconds, verifies each
                        step against the bulb, then restores

light help prints the full list.

What this bulb can and cannot do

  Real white runs from 2700K to 6500K. The bulb clamps anything lower to 2700K
  (measured: it reports 2700K and gives the same light). Below 2700K, light
  mixes red and green LEDs to imitate the temperature, using the Govee app's own
  colour table. Those mixes give about a fifth of the light of real white at the
  same brightness number, and colours of objects look flatter under them.
  light warmer and light cooler adjust brightness when they cross 2700K so the
  room does not jump.

  Any setting wakes the bulb. So after a sunset the bulb remembers a faint
  deep red at 1%. light on brings back the look from before the sunset. If you
  cut power at the wall instead, the lamp comes back as that faint red until
  the next light command.

  candle, breathe, sunset, sunrise and timers run from this Mac. It has to stay
  awake and within Bluetooth range; light asks macOS not to idle-sleep while one
  runs, but a closed lid still sleeps. If the lamp stops answering, the mode
  ends and the next light status tells you. Scenes run inside the bulb.

  While light is connected, the Govee app cannot reach the bulb over Bluetooth.
  light lets go 45 seconds after the last command. light quit lets go at once.

How it works

  light        a small wrapper that runs cli.py
  cli.py       reads your words and sends one request to the helper
  Light.app    the helper. macOS grants Bluetooth to an app, so this app owns
               the permission (you see "Light" in System Settings, Privacy &
               Security, Bluetooth). It starts on the first command, holds the
               link so repeat commands answer in well under a second, runs the
               animations, and exits when idle.
  agent.py     the helper's code
  Lamp.app     the menu bar panel, built from gui/*.swift. It sends the same requests
               as cli.py and needs no permissions of its own.
  light.py     the Bluetooth protocol (from version 1), with small additions
  warm.py      looks, scenes, colour tables and the flame model

  light verifies every lasting change twice: the bulb must acknowledge the
  command, and a separate state readback must match. Animation frames between two
  verified states go out unchecked so motion stays smooth.

  The bulb uses a two-step handshake and AES-ECB/RC4 framing with a published
  vendor constant. Protocol reference: govee-ble-local 1.10.0 (MIT, Brady Woods),
  see THIRD_PARTY_LICENSE.txt. The scene data and the Kelvin colour table come
  from the same package.

Build and test

  bash build.sh                           venv, Light.app, Lamp.app, offline tests
  .venv/bin/python -m unittest discover -s tests
  .venv/bin/python tests/live_check.py    about five minutes on the real lamp;
                                          also reads the MacBook's light sensor to
                                          confirm the room changed

  build.sh signs Light.app with your first Apple Development certificate, so a
  rebuild does not ask for Bluetooth permission again. The first run asks once.
  If light says the helper did not start, allow Light under System Settings,
  Privacy & Security, Bluetooth.

  Logs: work/agent.log. Your saved looks: presets.json.

Version 1

  The original one-shot tool is in v1/. Its "Govee Light.app" is still here and
  still works from a terminal that has Bluetooth permission itself.
