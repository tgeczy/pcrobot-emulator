# -*- coding: utf-8 -*-
"""
NVDA synthesizer driver for PC-ROBOT (RobotVOX / MULTIVOX), Nikol Elektronika, 1994.

The speech is produced by the original DOS software running in DOSBox-X with
its mixer muted; DOSBox records what the sound card would have played and the
PCM is handed to NVDA.  Nothing about the voice is re-implemented, so it
sounds exactly as it did in 1994.

Synthesis happens on a worker thread and is streamed as it arrives, so speech
starts as soon as the robot does.
"""

import builtins
import os
import struct
import sys
import threading

try:
    import queue
except ImportError:                                     # pragma: no cover
    import Queue as queue

_HERE = os.path.dirname(__file__)
_ENGINE_DIR = os.path.join(_HERE, '_pcrobot_engine')
#: The engine package name starts with an underscore on purpose: NVDA scans
#: synthDrivers/ for synthesizers and skips names beginning with "_".
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config
import nvwave
import tones
from logHandler import log
from synthDriverHandler import SynthDriver, VoiceInfo
from synthDriverHandler import synthIndexReached, synthDoneSpeaking
from speech.commands import IndexCommand

try:
    from autoSettingsUtils.driverSetting import NumericDriverSetting
except ImportError:                                     # older NVDA layout
    from driverHandler import NumericDriverSetting

_ = getattr(builtins, '_', lambda s: s)

from _pcrobot_engine import core

BIN_DIR = os.path.join(_ENGINE_DIR, 'bin')

#: The engine's own parameter ranges, from KIMOND's help text.
SPEED_FASTEST, SPEED_NORMAL = 1, 7       # lower is faster
PITCH_MIN, PITCH_MAX = 55, 155           # Hz; the engine accepts 20-512,
#: but its own resting voice is ~90-105 Hz (ROBOTVOX.CF ships PITCH=90 and
#: the demo scripts use p105), so NVDA's default 50 must land near 105.
VOLUME_MAX = 13
ARTIC_MAX = 5


def _scale(value, lo, hi):
    """NVDA's 0-100 sliders onto an engine range."""
    value = max(0, min(100, int(value)))
    return int(round(lo + (hi - lo) * value / 100.0))


#: A lone voiceless plosive is acoustically just a click; the engine
#: renders these four below the silence threshold (measured - every other
#: letter, digit and accented vowel speaks fine), so as single typed
#: characters they are echoed by their Hungarian letter names instead.
CHAR_NAMES = {
    'k': 'ká', 'p': 'pé', 'q': 'kú', 't': 'té',
}

#: The Sound Blaster output peaks around -26 dBFS even at the engine's full
#: volume - quiet next to modern system audio - so the PCM is lifted here.
#: 16x puts speech peaks just under full scale; anything hotter is clamped.
GAIN = 16


def _boost(pcm):
    n = len(pcm) // 2
    vals = struct.unpack('<%dh' % n, pcm)
    return struct.pack(
        '<%dh' % n,
        *[32767 if v > 2047 else (-32768 if v < -2048 else v * GAIN)
          for v in vals])


class SynthDriver(SynthDriver):
    name = 'pcrobot'
    description = 'PC-ROBOT (MULTIVOX, 1994)'

    supportedSettings = (
        SynthDriver.VoiceSetting(),
        SynthDriver.RateSetting(),
        SynthDriver.PitchSetting(),
        SynthDriver.VolumeSetting(),
        NumericDriverSetting('articulation', _('&Articulation'),
                             availableInSettingsRing=True, defaultVal=0,
                             minVal=0, maxVal=100, minStep=20,
                             normalStep=20, largeStep=20,
                             displayName=_('Articulation')),
        NumericDriverSetting('whisper', _('&Whisper'),
                             availableInSettingsRing=True, defaultVal=0,
                             minVal=0, maxVal=100, minStep=100,
                             normalStep=100, largeStep=100,
                             displayName=_('Whisper')),
    )
    supportedCommands = {IndexCommand}
    supportedNotifications = {synthIndexReached, synthDoneSpeaking}

    @classmethod
    def check(cls):
        if not core.find_dosbox():
            log.warning('pcrobot: DOSBox-X not found')
            return False
        for name in ('SBDD.EXE', 'ROBOTVOX.EXE', 'ROBOTVOX.OV', 'ROBOTVOX.CF',
                     'AHA.RAW', 'SPEAK.COM'):
            if not os.path.isfile(os.path.join(BIN_DIR, name)):
                log.warning('pcrobot: engine file missing: %s' % name)
                return False
        return True

    def __init__(self):
        super(SynthDriver, self).__init__()
        self._rate = 50
        self._pitch = 50
        self._volume = 100
        self._voice = '0'
        self._articulation = 0
        self._whisper = 0

        self._engine = core.Engine(BIN_DIR, log=log.debug)
        self._player = self._makePlayer(self._engine.rate)
        self._queue = queue.Queue()
        self._gen = 0
        self._genLock = threading.Lock()
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._alive = False

        self._thread = threading.Thread(target=self._worker, name='pcrobot synth')
        self._thread.daemon = True
        self._thread.start()
        # The DOS machine needs several seconds to boot, and NVDA has no voice
        # until it does.  Beep so the silence reads as "starting", not "broken".
        self._beep(440, 70)
        # Booting DOS takes a few seconds; do it off the main thread so NVDA
        # stays responsive while the robot wakes up.
        threading.Thread(target=self._boot, name='pcrobot boot',
                         daemon=True).start()

    @staticmethod
    def _beep(hz, ms):
        try:
            tones.beep(hz, ms)
        except Exception:
            pass

    def _boot(self):
        try:
            self._engine.start()
            self._alive = True
            self._beep(880, 90)
            if self._engine.rate != self._player.samplesPerSec:
                try:
                    self._player.close()
                except Exception:
                    pass
                self._player = self._makePlayer(self._engine.rate)
        except Exception:
            log.error('pcrobot: engine failed to start', exc_info=True)
            self._beep(220, 250)
        finally:
            self._ready.set()

    def _makePlayer(self, rate):
        try:
            return nvwave.WavePlayer(
                channels=1, samplesPerSec=rate, bitsPerSample=16,
                outputDevice=config.conf['audio']['outputDevice'])
        except Exception:
            try:
                return nvwave.WavePlayer(
                    channels=1, samplesPerSec=rate, bitsPerSample=16,
                    outputDevice=config.conf['speech']['outputDevice'])
            except Exception:
                return nvwave.WavePlayer(1, rate, 16)

    # -- NVDA interface ----------------------------------------------------
    def speak(self, speechSequence):
        parts, indexes = [], []
        for item in speechSequence:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, IndexCommand):
                indexes.append(item.index)
        text = ' '.join(p.strip() for p in parts if p and p.strip())
        text = ' '.join(text.split())
        if text or indexes:
            with self._genLock:
                gen = self._gen
            self._queue.put((gen, text, indexes))

    def _isCurrent(self, gen):
        with self._genLock:
            return gen == self._gen

    def cancel(self):
        with self._genLock:
            self._gen += 1
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass
        try:
            self._player.stop()
        except Exception:
            pass
        # Telling the DOS side to shut up means touching the filesystem, which
        # must not happen on NVDA's main thread.
        threading.Thread(target=self._quieten, daemon=True).start()

    def _quieten(self):
        try:
            self._engine.stop_speech()
        except Exception:
            pass

    def pause(self, switch):
        try:
            self._player.pause(switch)
        except Exception:
            pass

    def terminate(self):
        self._stopping.set()
        self.cancel()
        self._queue.put(None)
        self._thread.join(timeout=5)
        try:
            self._engine.stop()
        except Exception:
            pass
        try:
            self._player.close()
        except Exception:
            pass

    # -- worker ------------------------------------------------------------
    def _worker(self):
        while not self._stopping.is_set():
            job = self._queue.get()
            if job is None:
                break
            try:
                self._ready.wait(timeout=30)
                if self._alive and self._isCurrent(job[0]):
                    self._render(job)
            except Exception:
                log.error('pcrobot: synthesis failed', exc_info=True)
            finally:
                self._queue.task_done()

    def _render(self, job):
        gen, text, indexes = job
        spoke = [False]

        def on_pcm(chunk):
            if not self._isCurrent(gen):
                return
            self._player.feed(_boost(chunk))
            spoke[0] = True

        if text:
            if len(text) == 1:
                text = CHAR_NAMES.get(text.lower(), text)
            payloads = [
                self._engine.encode(
                    piece,
                    rate=_scale(100 - self._rate, SPEED_FASTEST, SPEED_NORMAL),
                    pitch=_scale(self._pitch, PITCH_MIN, PITCH_MAX),
                    volume=_scale(self._volume, 0, VOLUME_MAX),
                    voice=int(self._voice),
                    whisper=1 if self._whisper >= 50 else 0,
                    articulation=_scale(self._articulation, 0, ARTIC_MAX))
                for piece in core.split_text(text)]
            if payloads and self._isCurrent(gen):
                # One sentence per payload, spoken sequentially by the
                # engine within a single capture session; long text reads
                # with a natural pause between sentences.  Single typed
                # characters barely need the anti-queue debounce at all.
                self._engine.speak(
                    payloads, on_pcm,
                    should_cancel=lambda: not self._isCurrent(gen),
                    commit_delay=0.02 if len(text) <= 2 else 0.05,
                    # If the engine says nothing within this window it never
                    # will; waiting the old 5 s stalled the typing queue.
                    start_timeout=1.5,
                    max_seconds=60.0 + 5.0 * len(payloads))
        if not self._isCurrent(gen):
            return
        if spoke[0]:
            try:
                self._player.idle()
            except Exception:
                pass
        if self._isCurrent(gen):
            for index in indexes:
                try:
                    synthIndexReached.notify(synth=self, index=index)
                except Exception:
                    pass
            synthDoneSpeaking.notify(synth=self)

    # -- settings ----------------------------------------------------------
    def _get_rate(self):
        return self._rate

    def _set_rate(self, value):
        self._rate = max(0, min(100, int(value)))

    def _get_pitch(self):
        return self._pitch

    def _set_pitch(self, value):
        self._pitch = max(0, min(100, int(value)))

    def _get_volume(self):
        return self._volume

    def _set_volume(self, value):
        self._volume = max(0, min(100, int(value)))

    def _get_articulation(self):
        return self._articulation

    def _set_articulation(self, value):
        self._articulation = max(0, min(100, int(value)))

    def _get_whisper(self):
        return self._whisper

    def _set_whisper(self, value):
        self._whisper = max(0, min(100, int(value)))

    # -- voices ------------------------------------------------------------
    def _get_availableVoices(self):
        return {
            '0': VoiceInfo('0', 'PC-ROBOT férfi 1', 'hu'),
            '1': VoiceInfo('1', 'PC-ROBOT férfi 2', 'hu'),
        }

    def _get_voice(self):
        return self._voice

    def _set_voice(self, value):
        self._voice = '1' if str(value) == '1' else '0'

    def _get_language(self):
        return 'hu'
