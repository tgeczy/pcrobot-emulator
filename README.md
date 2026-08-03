# pcrobot-emulator

An NVDA screen reader driver for **PC-ROBOT / RobotVOX Multimedia 2.0** —
the first Hungarian text-to-speech system for PC, created by NIKOL
(Nikléczy Péter and Olaszy Gábor) in 1994.

Nothing about the voice is re-implemented or approximated: the original
DOS engine runs inside DOSBox-X with its mixer muted, the audio the Sound
Blaster would have played is captured digitally, and NVDA plays it. The
voice is bit-for-bit the 1994 article — and thanks to some tricks below,
it boots in about **0.6 seconds** and answers a keypress in about **0.1**.

## The easy way: install the released add-on

Grab `pcrobot-<version>.nvda-addon` from the
[Releases](https://github.com/tgeczy/pcrobot-emulator/releases) page.
It bundles everything (engine files with the authors' written permission,
DOSBox-X under GPL v2) — press Enter on it and NVDA installs it.

## The from-source way

This repository deliberately contains **only this project's own code**
(MIT licensed). Two ingredients are fetched separately:

1. **Engine files** (© NIKOL, published with the authors' express
   permission): download from the
   [Internet Archive](https://archive.org/details/pcrobot-archive) and
   place `SBDD.EXE`, `ROBOTVOX.EXE`, `ROBOTVOX.OV`, `ROBOTVOX.CF` and
   `AHA.RAW` into `addon/synthDrivers/_pcrobot_engine/bin/`.
2. **DOSBox-X** (GPL v2): from [dosbox-x.com](https://dosbox-x.com/),
   place `dosbox-x.exe` and its `COPYING.txt` into
   `addon/synthDrivers/_pcrobot_engine/dosbox/`.

Then `python tools/build_addon.py` produces the installable
`.nvda-addon`.

## How it works (the fun parts)

* **Hidden desktop**: DOSBox-X is spawned onto its own Windows desktop
  (`CreateDesktopW` + `CreateProcessW(lpDesktop=...)`), so its window can
  never take focus, never appears in the taskbar, and the screen reader
  never announces it. `PostMessage` and a file-based control channel work
  across desktops.
* **Turbo everywhere**: DOSBox-X's fast-forward is toggled *from inside
  DOS* — a one-byte marker file makes the tiny bridge program
  (`SPEAK.COM`, 326 bytes of assembly, source in `tools/make_speak.py`)
  exit with a code that an AUTOEXEC batch loop turns into
  `config -set "cpu turbo=..."`. The machine boots at ~40×, synthesizes
  at ~40×, and swallows cancelled speech at ~40×; only the audio you
  actually hear plays at 1×.
* **Speech capture**: DOSBox's WAV capture is read as it grows; silence
  detection is done in audio time, one sentence per submission, so long
  text reads with natural pauses and cancellation is always clean.

## Hear it

[`demo/pcrobot_NVDA_demo.mp3`](demo/pcrobot_NVDA_demo.mp3) - the robot
driving NVDA through Windows dialogs in 2026, recorded live.

## Credits

* **Nikléczy Péter and Olaszy Gábor (NIKOL)** — the synthesizer itself,
  and the gracious 2026 permission to preserve and republish it.
* The [Museum of Hungarian Speech Technology](https://www.magyarbeszed.hu/)
  — the authors' own archive of this history.
* Driver and tooling: Tamas Geczy (tgeczy), with heavy lifting by
  Claude (Anthropic).

Related: [BraiLab PC archive](https://archive.org/details/brailab-pc-archive)
— the other great Hungarian speech synthesizer for blind users, preserved
the same week.
