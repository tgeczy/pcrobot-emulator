# -*- coding: utf-8 -*-
"""
Host side of the PC-ROBOT speech engine.

The synthesizer itself is the original 1994 Nikol Elektronika DOS software:
SBDD.EXE (the Sound Blaster driver) and ROBOTVOX.EXE (the MULTIVOX speech
engine).  Both run in DOSBox-X with the mixer muted, so nothing is heard on
the host; instead DOSBox records what the sound card would have played, and
we hand that PCM to NVDA.  The voice is therefore the genuine article, not an
approximation of it.

A tiny resident DOS program, SPEAK.COM, is the bridge: it watches for IN.TXT
in the mounted directory and passes whatever appears to ROBOTVOX through its
INT 2Fh service.  Writing a file is all it takes to make the robot talk.
"""

import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time

#: Text is split into pieces this long.  ROBOTVOX is a serial,
#: uninterruptible queue - once text is submitted it will be spoken in full,
#: and no escape command stops it (measured: audio continues ~5 s after
#: ESC K, ESC R or both) - so a cancelled piece's tail must be sat through.
#: With turbo fast-forwarding that tail at ~40x, pieces can be long; the
#: real ceiling is SPEAK.COM's 200-byte read buffer minus the command
#: prefix and flush suffix encode() adds.
MAX_TEXT = 150

#: Escape byte introducing an inline engine command, followed by a letter and
#: digits.  The same convention the original .TXT files used.
ESC = b'\xfe'

#: Special first bytes understood by SPEAK.COM itself rather than passed to
#: the engine: it exits with a distinct code and the autoexec batch loop
#: flips DOSBox's turbo (fast-forward) mode before restarting it.  Turbo
#: compresses wall time, not audio - the samples are identical, they just
#: arrive ~40x sooner - so synthesis and the draining of abandoned tails
#: both stop being real-time costs.  Payloads from encode() always begin
#: with ESC, so these can never collide with spoken text.
TURBO_ON = b'#'
TURBO_OFF = b'!'
QUIT = b'@'

#: The bundled copy ships inside the add-on so it works out of the box;
#: system-wide installations are fallbacks (or delete the dosbox folder
#: to prefer them).
DOSBOX_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 'dosbox', 'dosbox-x.exe'),
    r'C:\DOSBox-X\dosbox-x.exe',
    r'C:\Program Files\DOSBox-X\dosbox-x.exe',
    r'C:\Program Files (x86)\DOSBox-X\dosbox-x.exe',
)

#: DOSBox-X menu command id for "Record audio to WAV".
MENU_RECORD_WAV = 5207

#: DOSBox flushes its WAV capture in fixed blocks of this many samples
#: (measured, and identical at every mixer rate), so the mixer rate decides
#: how often audio reaches us: at 22050 Hz a block is 743 ms and speech
#: arrived in bursts that starved real-time playback (heard as chop in the
#: middle of every phrase); at 96000 Hz the same block is 171 ms.  The mixer
#: rate in CONF below is chosen for this, not for fidelity.
FLUSH_SAMPLES = 16384

CONF = """[sdl]
autolock=false
priority=higher,normal
waitonerror=false
windowposition=20000,20000

[dosbox]
memsize=32
fastbioslogo=true
startbanner=false
captures={cap}
title=PC-ROBOT speech engine

[mixer]
nosound=true
rate=96000
blocksize=512
prebuffer=5

[cpu]
core=auto
cycles=max
turbo=true
stop turbo on key=false

[sblaster]
sbtype=sb16
sbbase=220
irq=7
dma=1
hdma=5
oplmode=none

[autoexec]
mount c "{work}"
c:
set BLASTER=A220 I7 D1 H5 T6
sbdd
robotvox
:loop
speak
if errorlevel 3 goto ton
if errorlevel 2 goto toff
goto fin
:ton
config -set "cpu turbo=true"
goto loop
:toff
config -set "cpu turbo=false"
goto loop
:fin
"""


class EngineError(RuntimeError):
    pass


def find_dosbox(explicit=None):
    if explicit and os.path.isfile(explicit):
        return explicit
    for p in DOSBOX_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


class _Capture(object):
    """Reads the growing DOSBox capture file and yields mono 16-bit PCM."""

    def __init__(self, cap_dir):
        self.dir = cap_dir
        self.path = None
        self.pos = 0
        self.channels = 2
        self.rate = 22050

    def open(self, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            files = [f for f in os.listdir(self.dir) if f.lower().endswith('.wav')]
            if files:
                files.sort(key=lambda f: os.path.getmtime(os.path.join(self.dir, f)))
                self.path = os.path.join(self.dir, files[-1])
                if os.path.getsize(self.path) > 44:
                    with open(self.path, 'rb') as f:
                        head = f.read(44)
                    self.channels = struct.unpack_from('<H', head, 22)[0] or 2
                    self.rate = struct.unpack_from('<I', head, 24)[0] or 22050
                    self.pos = 44
                    return True
            time.sleep(0.05)
        return False

    def read_new(self):
        """Return whatever PCM has been written since the last call, as mono."""
        if not self.path:
            return b''
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return b''
        if size <= self.pos:
            return b''
        frame = 2 * self.channels
        avail = ((size - self.pos) // frame) * frame
        if avail <= 0:
            return b''
        with open(self.path, 'rb') as f:
            f.seek(self.pos)
            data = f.read(avail)
        self.pos += len(data)
        if self.channels == 1:
            return data
        # The Sound Blaster's mono voice sits centred in DOSBox's stereo mix,
        # so the left channel *is* the signal; slicing it out with array is
        # C-speed, which matters now that the mixer runs at 96 kHz.
        import array
        samples = array.array('h')
        samples.frombytes(data)
        return samples[0::2].tobytes()

    def drain(self):
        while self.read_new():
            pass


def _peak(pcm):
    if not pcm:
        return 0
    n = len(pcm) // 2
    step = max(1, n // 512)
    vals = struct.unpack_from('<%dh' % n, pcm, 0)
    return max(abs(vals[i]) for i in range(0, n, step))


class _NativeProcess(object):
    """Minimal Popen lookalike for a process started with CreateProcessW
    (subprocess cannot set lpDesktop, which the hidden desktop needs)."""

    _STILL_ACTIVE = 259

    def __init__(self, hProcess, pid):
        self._hProcess = hProcess
        self.pid = pid

    def poll(self):
        import ctypes
        from ctypes import wintypes
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(
            self._hProcess, ctypes.byref(code))
        return None if code.value == self._STILL_ACTIVE else code.value

    def terminate(self):
        import ctypes
        ctypes.windll.kernel32.TerminateProcess(self._hProcess, 0)


class Engine(object):
    """Runs the DOS engine and turns text into PCM."""

    #: DOSBox runs on its own Windows desktop: a window there can never
    #: take the user's focus, never appears in the taskbar or Alt+Tab,
    #: and the screen reader never announces it - it simply does not
    #: exist as far as the user's desktop is concerned.  PostMessage and
    #: the file-based control channel both work across desktops.
    DESKTOP_NAME = 'pcrobot_engine'

    def __init__(self, payload_dir, dosbox=None, log=None):
        self.log = log or (lambda *a: None)
        self.dosbox = find_dosbox(dosbox)
        if not self.dosbox:
            raise EngineError('DOSBox-X was not found; install it or set the path')
        self.payload = payload_dir
        self.proc = None
        self.work = None
        self.cap = None
        self.hwnd = None
        self._hdesk = None
        self._prev_foreground = None
        #: set when an utterance was abandoned, so the tail still being spoken
        #: in DOS is discarded before the next one instead of being heard as a
        #: fragment of the previous announcement
        self._dirty = False
        self._lock = threading.Lock()
        #: optimistic view of whether DOSBox turbo is currently engaged
        self._turbo_on = False
        self._turbo_off_timer = None
        self.rate = 22050
        #: rotate the capture file once it passes this size (at 96 kHz the
        #: file grows about 23 MB a minute, silence included)
        self.max_capture_bytes = 512 * 1024 * 1024

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout=25.0):
        self.work = tempfile.mkdtemp(prefix='pcrobot_')
        cap_dir = os.path.join(self.work, 'cap')
        os.makedirs(cap_dir)
        # AHA.RAW is not optional: SBDD looks for it at start-up and
        # ROBOTVOX hangs waiting for the driver if it is missing.
        needed = ('SBDD.EXE', 'ROBOTVOX.EXE', 'ROBOTVOX.OV', 'ROBOTVOX.CF',
                  'AHA.RAW', 'SPEAK.COM')
        for name in needed:
            src = os.path.join(self.payload, name)
            if not os.path.isfile(src):
                raise EngineError('missing engine file: %s' % name)
            with open(src, 'rb') as a, open(os.path.join(self.work, name), 'wb') as b:
                b.write(a.read())
        conf = os.path.join(self.work, 'pcrobot.conf')
        with open(conf, 'w') as f:
            f.write(CONF.format(work=self.work, cap=cap_dir))

        # Remember who had the keyboard, so it can be handed straight back:
        # hiding DOSBox's window is not enough, it stays the foreground window
        # and would swallow the user's keystrokes.
        try:
            import ctypes
            self._prev_foreground = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            self._prev_foreground = None

        self.proc = self._spawn_hidden(conf)
        if self.proc is not None:
            self.log('pcrobot: DOSBox-X started on hidden desktop (pid %s)'
                     % self.proc.pid)
            armed = self._arm_recording_hidden(timeout=timeout)
        else:
            # Fallback: visible desktop with the hide/foreground dance.
            info = None
            creationflags = 0
            if sys.platform == 'win32':
                info = subprocess.STARTUPINFO()
                info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                info.wShowWindow = 7       # SW_SHOWMINNOACTIVE
                creationflags = 0x08000000  # CREATE_NO_WINDOW for the console
            self.proc = subprocess.Popen([self.dosbox, '-conf', conf],
                                         startupinfo=info,
                                         creationflags=creationflags)
            self.log('pcrobot: DOSBox-X started (pid %s)' % self.proc.pid)
            armed = self._arm_recording(timeout=timeout)
        if not armed:
            self.stop()
            raise EngineError('could not start audio capture in DOSBox-X')
        self.cap = _Capture(cap_dir)
        if not self.cap.open(timeout=timeout):
            self.stop()
            raise EngineError('DOSBox-X never produced a capture file')
        self.rate = self.cap.rate
        # The machine boots with turbo=true in the config, so DOS, SBDD and
        # ROBOTVOX come up at ~40x.  The handshake marker is TURBO_ON - a
        # no-op for the config, but SPEAK.COM only consumes it once the
        # whole autoexec chain is up, so the file disappearing IS the ready
        # signal - and crucially it keeps turbo ENGAGED while the boot
        # announcement's tail is still sounding, so the settle wait below
        # swallows it at ~40x instead of in painful real time.
        self._write_command(TURBO_ON)
        in_txt = os.path.join(self.work, 'IN.TXT')
        deadline = time.time() + timeout
        while os.path.isfile(in_txt):
            if self.proc.poll() is not None:
                raise EngineError('DOSBox-X exited during boot')
            if time.time() > deadline:
                self.stop()
                raise EngineError('the DOS side never became ready')
            time.sleep(0.05)
        self._wait_until_quiet(settle=0.8, timeout=8.0)
        self._turbo(False)
        self.log('pcrobot: engine ready at %d Hz' % self.rate)

    def _spawn_hidden(self, conf):
        """Start DOSBox-X on its own hidden desktop; None if that fails."""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hdesk = user32.CreateDesktopW(self.DESKTOP_NAME, None, None,
                                          0, 0x10000000, None)
            if not hdesk:
                return None

            class SI(ctypes.Structure):
                _fields_ = [
                    ('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR),
                    ('lpDesktop', wintypes.LPWSTR),
                    ('lpTitle', wintypes.LPWSTR),
                    ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD),
                    ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD),
                    ('dwXCountChars', wintypes.DWORD),
                    ('dwYCountChars', wintypes.DWORD),
                    ('dwFillAttribute', wintypes.DWORD),
                    ('dwFlags', wintypes.DWORD),
                    ('wShowWindow', wintypes.WORD),
                    ('cbReserved2', wintypes.WORD),
                    ('lpReserved2', ctypes.c_void_p),
                    ('hStdInput', wintypes.HANDLE),
                    ('hStdOutput', wintypes.HANDLE),
                    ('hStdError', wintypes.HANDLE)]

            class PI(ctypes.Structure):
                _fields_ = [('hProcess', wintypes.HANDLE),
                            ('hThread', wintypes.HANDLE),
                            ('dwProcessId', wintypes.DWORD),
                            ('dwThreadId', wintypes.DWORD)]

            si = SI()
            si.cb = ctypes.sizeof(si)
            si.lpDesktop = self.DESKTOP_NAME
            pi = PI()
            cmd = '"%s" -conf "%s"' % (self.dosbox, conf)
            ok = kernel32.CreateProcessW(None, cmd, None, None, False,
                                         0x08000000, None, None,
                                         ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                user32.CloseDesktop(hdesk)
                return None
            kernel32.CloseHandle(pi.hThread)
            self._hdesk = hdesk
            return _NativeProcess(pi.hProcess, pi.dwProcessId)
        except Exception:
            return None

    def _arm_recording_hidden(self, timeout=25.0):
        """Find the window on the hidden desktop and start recording.

        No hiding, no focus juggling: the window cannot bother anyone
        where it lives.  PostMessage reaches it across desktops.  SDL
        creates untitled helper windows before the real one, so the
        record command is only trusted once the capture file actually
        appears; until then other candidate windows are tried, each at
        most once (the command is a toggle).
        """
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        cap_dir = os.path.join(self.work, 'cap')
        start_t = time.time()
        deadline = start_t + timeout
        posted = set()
        target = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum(hwnd, lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == self.proc.pid:
                if user32.GetWindowTextLengthW(hwnd) > 0:
                    target.insert(0, hwnd)
                else:
                    target.append(hwnd)
            return True

        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                if any(f.lower().endswith('.wav')
                       for f in os.listdir(cap_dir)):
                    return True
            except OSError:
                pass
            del target[:]
            user32.EnumDesktopWindows(self._hdesk, enum, 0)
            # ONLY the window carrying our [dosbox] title may be poked.
            # The process also owns Tooltip/IME helper windows, and a
            # tooltip FORWARDS WM_COMMAND to its owner - posting to it
            # and then to the main window toggles recording on and off
            # again.  The title appears well within the timeout.
            for h in target:
                buf = ctypes.create_unicode_buffer(128)
                user32.GetWindowTextW(h, buf, 128)
                if 'PC-ROBOT' in buf.value and h not in posted:
                    self.hwnd = h
                    user32.PostMessageW(h, 0x0111, MENU_RECORD_WAV, 0)
                    posted.add(h)
                    break
            time.sleep(0.1)
        return False

    def _turbo(self, on):
        """Fast-forward the DOS machine (or stop doing so).

        Goes through SPEAK.COM: a one-byte command file makes it exit with a
        distinct code, and the autoexec batch loop runs DOSBox-X's own
        CONFIG -set "cpu turbo=..." before restarting it.  Fire and forget -
        if it does not engage, everything still works at real time.
        """
        try:
            self._write_command(TURBO_ON if on else TURBO_OFF, tries=25)
            self._turbo_on = on
        except Exception:
            pass

    def _schedule_turbo_off(self, delay=1.5):
        """Drop turbo after an idle period instead of per utterance.

        The on/off markers each cost a SPEAK.COM exit, a CONFIG run and a
        reload - per keystroke that overhead made character echo lag.  A
        burst of rapid speech now pays it once: turbo stays engaged while
        utterances keep coming and a timer switches it off when the flow
        stops.  (The idle cost of holding turbo is only capture-file
        growth, which rotation already handles.)
        """
        if self._turbo_off_timer is not None:
            self._turbo_off_timer.cancel()
        t = threading.Timer(delay, self._turbo_off_idle)
        t.daemon = True
        self._turbo_off_timer = t
        t.start()

    def _turbo_off_idle(self):
        if not self._lock.acquire(False):
            return          # a new utterance took over; it reschedules
        try:
            if self.proc and self.proc.poll() is None:
                self._turbo(False)
        except Exception:
            pass
        finally:
            self._lock.release()

    def _wait_until_quiet(self, settle=0.4, timeout=12.0, expect_speech=False):
        """Wait for the capture to go quiet.

        At start-up ROBOTVOX announces itself ("Multimedia robot, start").
        Recording is armed before that happens, so merely waiting for quiet
        would finish *before* the announcement and let it contaminate the
        first real utterance.  With `expect_speech` we wait for the
        announcement to arrive first, and only then for it to end.
        """
        deadline = time.time() + timeout
        seen_speech = not expect_speech
        # Settle is measured in *audio* time, not wall time, so that turbo
        # (which delivers many seconds of audio per wall second) is waited
        # out at turbo speed too.
        quiet_bytes = 0
        need = int(settle * self.rate) * 2
        while time.time() < deadline:
            chunk = self.cap.read_new()
            if not chunk:
                time.sleep(0.02)          # no data yet is not the same as quiet
                continue
            if _peak(chunk) > 90:
                seen_speech = True
                quiet_bytes = 0
            elif seen_speech:
                quiet_bytes += len(chunk)
                if quiet_bytes >= need:
                    break
            time.sleep(0.005)
        self.cap.drain()

    def _hide(self):
        """Push the DOSBox window out of the foreground and out of sight."""
        import ctypes
        if not self.hwnd:
            return
        user32 = ctypes.windll.user32
        try:
            user32.ShowWindow(self.hwnd, 0)                   # SW_HIDE
            if user32.GetForegroundWindow() == self.hwnd and self._prev_foreground:
                # Give the keyboard back to whatever the user was using.
                user32.SetForegroundWindow(self._prev_foreground)
        except Exception:
            pass

    def _keep_hidden(self, seconds=8.0, interval=0.25):
        end = time.time() + seconds
        while time.time() < end and self.proc and self.proc.poll() is None:
            self._hide()
            time.sleep(interval)

    def _post_record_toggle(self):
        import ctypes
        if not self.hwnd:
            return False
        return bool(ctypes.windll.user32.PostMessageW(
            self.hwnd, 0x0111, MENU_RECORD_WAV, 0))

    def _rotate_capture(self):
        """Start a fresh capture file and drop the old one.

        DOSBox records continuously - silence included - at about 5 MB a
        minute, so a long NVDA session would otherwise fill the disk.
        """
        old = self.cap.path
        if not self._post_record_toggle():          # stop
            return
        time.sleep(0.4)
        self._post_record_toggle()                  # start a new file
        time.sleep(0.6)
        prev = self.cap.path
        self.cap.path = None
        if self.cap.open(timeout=8.0) and self.cap.path != prev:
            self.log('pcrobot: rotated capture file')
        if old and old != self.cap.path:
            for _ in range(10):
                try:
                    os.remove(old)
                    break
                except OSError:
                    time.sleep(0.2)
        self._wait_until_quiet(settle=0.2, timeout=3.0)

    def _arm_recording(self, timeout=25.0):
        """Ask DOSBox-X to start recording, via its own menu command."""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        WM_COMMAND = 0x0111
        deadline = time.time() + timeout
        target = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum(hwnd, lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == self.proc.pid and user32.IsWindowVisible(hwnd):
                target.append(hwnd)
                return False
            return True

        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            del target[:]
            user32.EnumWindows(enum, 0)
            if target:
                self.hwnd = target[0]
                # Hide it the instant it exists.  DOSBox uses SDL, which makes
                # and shows its own window regardless of STARTUPINFO, so it
                # takes the foreground - keystrokes would land in DOS instead
                # of the user's application, and the screen reader announces
                # it.  PostMessage still reaches a hidden window.
                self._hide()
                time.sleep(0.3)
                self._hide()
                user32.PostMessageW(self.hwnd, WM_COMMAND, MENU_RECORD_WAV, 0)
                # DOSBox re-shows the window when the running program changes
                # (the title tracks it), so keep hiding for a few seconds.
                threading.Thread(target=self._keep_hidden, daemon=True).start()
                return True
            time.sleep(0.2)
        return False

    def stop(self):
        if self._turbo_off_timer is not None:
            self._turbo_off_timer.cancel()
            self._turbo_off_timer = None
        try:
            if self.work:
                self._write_command(QUIT)
                time.sleep(0.3)
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None
        if self._hdesk:
            try:
                import ctypes
                ctypes.windll.user32.CloseDesktop(self._hdesk)
            except Exception:
                pass
            self._hdesk = None
        if self.work:
            import shutil
            for _ in range(5):
                try:
                    shutil.rmtree(self.work, ignore_errors=False)
                    break
                except OSError:
                    time.sleep(0.4)
            else:
                shutil.rmtree(self.work, ignore_errors=True)
            self.work = None

    # -- speaking ----------------------------------------------------------
    def _write_command(self, payload, tries=50):
        """Hand a line to SPEAK.COM by dropping IN.TXT into the mount."""
        if not self.work:
            return False
        tmp = os.path.join(self.work, 'IN.TMP')
        dst = os.path.join(self.work, 'IN.TXT')
        with open(tmp, 'wb') as f:
            f.write(payload)
        for _ in range(tries):
            try:
                os.rename(tmp, dst)
                return True
            except OSError:
                time.sleep(0.02)      # SPEAK.COM still holds the previous one
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    def encode(self, text, rate=None, pitch=None, volume=None, voice=None,
               whisper=None, articulation=None):
        out = b''
        if voice is not None:
            out += ESC + b'v%d' % voice
        if rate is not None:
            out += ESC + b's%d' % rate
        if pitch is not None:
            out += ESC + b'p%d' % pitch
        if volume is not None:
            out += ESC + b'a%d' % volume
        if whisper is not None:
            out += ESC + b'w%d' % whisper
        if articulation is not None:
            out += ESC + b'x%d' % articulation
        if isinstance(text, bytes):
            body = text
        else:
            body = text.encode('cp852', 'replace')
        body = body.rstrip()
        # ROBOTVOX buffers text until a sentence ends, exactly as it did for
        # KIMOND (which always followed a line with a separate " ." flush).
        # Without this an utterance is not spoken until the *next* one arrives,
        # which is heard as every keypress being announced one keypress late.
        if not body.endswith((b'.', b'!', b'?', b':', b';')):
            body += b'.'
        return out + body + b' .'

    def speak(self, payloads, on_pcm, should_cancel=None,
              start_timeout=5.0, silence_ms=350, max_seconds=60.0,
              commit_delay=0.05):
        """Speak one or more encoded payloads as ONE utterance; stream PCM
        to `on_pcm`.

        A long text arrives as a list of pieces, one SENTENCE each (the
        engine takes at most ~200 bytes per submission anyway).  The pieces
        are spoken strictly one after another within a single capture
        session.  They must not be queued into the robot ahead of time: the
        handoff between submissions happens in REAL time (host I/O, INT 2Fh
        processing), and under turbo every real millisecond of it turns
        into ~40 emulated milliseconds of recorded silence - measured up to
        15 s of dead air between queued sentences.  Sequential submission
        keeps those gaps between captures, where nothing is fed, so the
        listener just hears a natural reading rhythm.

        Within one piece the only pauses are acoustic (comma-scale, well
        under the silence window), so the end of each piece is detected
        quickly and reliably.  Silence is measured in audio time, not in
        however many bytes a read returned.
        """
        if isinstance(payloads, (bytes, bytearray)):
            payloads = [bytes(payloads)]
        if not payloads:
            return False
        with self._lock:
            if not self.proc or self.proc.poll() is not None:
                raise EngineError('engine is not running')
            if self._turbo_off_timer is not None:
                self._turbo_off_timer.cancel()
                self._turbo_off_timer = None
            self.cap.drain()
            # Fast-forward the DOS machine for the whole job: synthesis,
            # streaming and (on cancellation) tail-draining all run at ~40x,
            # leaving NVDA's playback of the audio as the only real-time
            # part.  Turbo is left engaged between rapid utterances (see
            # _schedule_turbo_off) so bursts pay the switching cost once.
            if not self._turbo_on:
                self._turbo(True)
            try:
                return self._speak_locked(payloads, on_pcm, should_cancel,
                                          start_timeout, silence_ms,
                                          max_seconds, commit_delay)
            finally:
                self._schedule_turbo_off()

    def _speak_locked(self, payloads, on_pcm, should_cancel,
                      start_timeout, silence_ms, max_seconds, commit_delay):
        WINDOW = 20                                  # ms per decision window
        # Hold back briefly before committing anything to DOS.  The
        # engine cannot be interrupted, so text handed over during fast
        # navigation would queue up and be spoken long after the user has
        # moved on.  Waiting a moment means a keypress that is superseded
        # is dropped before it ever reaches the engine.
        hold = time.time() + commit_delay
        while time.time() < hold:
            if should_cancel and should_cancel():
                return False          # nothing submitted, nothing to undo
            time.sleep(0.01)
        self.cap.drain()
        win_bytes = max(2, int(self.rate * WINDOW / 1000.0)) * 2
        quiet_needed = max(1, int(silence_ms / WINDOW))
        # Playback consumes in real time but DOSBox delivers in whole
        # flush blocks, so playing the instant speech is heard leaves no
        # reserve: if speech began near the end of a block, the player
        # runs dry mid-word waiting for the next one.  Hold one block's
        # worth after speech is first heard and hand everything over in
        # one piece; that reserve is what rides out every later gap.
        # Under turbo the block's worth of *audio* accumulates in a few
        # wall ms, so the reserve is measured both ways and whichever
        # fills first releases playback.  The reserve is rebuilt at the
        # start of every piece - the player has drained by then.
        lead_secs = float(FLUSH_SAMPLES) / self.rate
        margin_bytes = int(lead_secs * self.rate) * 2
        pending = bytearray()
        lead = bytearray()
        started = False               # any audio fed during this utterance
        hard_stop = time.time() + max_seconds

        def flush_lead():
            if lead:
                on_pcm(bytes(lead))
                del lead[:]

        for payload in payloads:
            if not self._write_command(payload):
                return started
            submit_time = time.time()
            piece_started = False
            lead_until = None
            quiet = 0
            deadline = time.time() + start_timeout
            del pending[:]
            done_piece = False
            while not done_piece:
                if should_cancel and should_cancel():
                    # The engine cannot be stopped, and the piece handed to
                    # it *will* be spoken.  Swallow that audio here, while
                    # we are already watching for it, rather than leaving it
                    # to prefix the next announcement.  NVDA has stopped
                    # playing, so the user hears silence throughout.  The
                    # lead buffer is simply dropped - it was never played.
                    self._drain_tail(submit_time=submit_time,
                                     started=piece_started)
                    return False
                if time.time() > hard_stop:
                    flush_lead()
                    return started
                if lead_until is not None and (len(lead) >= margin_bytes
                                               or time.time() >= lead_until):
                    flush_lead()
                    lead_until = None
                chunk = self.cap.read_new()
                if not chunk:
                    if not piece_started and time.time() > deadline:
                        break         # this piece never spoke; carry on
                    time.sleep(0.01)
                    continue
                pending.extend(chunk)
                while len(pending) >= win_bytes:
                    window = bytes(pending[:win_bytes])
                    del pending[:win_bytes]
                    loud = _peak(window) > 90
                    if not piece_started:
                        if not loud:
                            continue     # the handoff silence before speech
                        piece_started = True
                        started = True
                        lead_until = time.time() + lead_secs
                    if lead_until is not None:
                        lead.extend(window)
                    else:
                        on_pcm(window)
                    if loud:
                        quiet = 0
                    else:
                        quiet += 1
                        if quiet >= quiet_needed:
                            # piece finished; the trailing quiet already fed
                            # becomes the pause between sentences
                            flush_lead()
                            done_piece = True
                            break
        self._maybe_rotate()
        return started

    def _drain_tail(self, submit_time=None, started=False, settle=3.0,
                    total=15.0):
        """Swallow the audio of an abandoned utterance.

        There is a gap of half a second or so between handing text to DOS and
        hearing it, so simply waiting for quiet can finish *before* the tail
        even starts and let it prefix the next announcement.  Wait for it to
        appear (briefly - it may already be over), then for it to end.

        The settle window must outlast the robot's own pauses: between
        sentences of a queued multi-piece tail it stays silent for well
        over a second before carrying on.  Settle is audio time, so under
        turbo even a two-second window costs ~50 ms of wall time.
        """
        # Audio appears about 0.3 s after text is handed over.  Once that
        # window has passed with nothing heard, the utterance is already over
        # and there is nothing to swallow - waiting longer just stalls the
        # next keypress, which is heard as a long gap between announcements.
        appear_until = (submit_time or time.time()) + 0.8
        end = time.time() + total
        # If this utterance was already being heard, the engine is mid-sentence
        # and the rest is definitely still coming - even if we happen to cancel
        # during one of its pauses between words.  Treating that momentary
        # quiet as "nothing is coming" is what let fragments through.
        appeared = bool(started)
        # Settle counts *audio* time so that a turbo-fed tail is swallowed at
        # turbo speed: 0.35 s of emulated silence arrives in a few wall ms.
        quiet_bytes = 0
        need = int(settle * self.rate) * 2
        while time.time() < end:
            chunk = self.cap.read_new()
            if chunk:
                # Only *data* can tell us whether the engine is still talking.
                # An empty read just means DOSBox has not flushed its next
                # block yet; counting that as silence ends the drain almost
                # immediately and lets the rest of the utterance escape into
                # the next announcement.
                if _peak(chunk) > 90:
                    appeared = True
                    quiet_bytes = 0
                elif appeared:
                    quiet_bytes += len(chunk)
                    if quiet_bytes >= need:
                        break
            elif not appeared and time.time() > appear_until:
                break                     # nothing more is coming
            time.sleep(0.005)
        self.cap.drain()

    def _maybe_rotate(self):
        try:
            if (self.cap.path and
                    os.path.getsize(self.cap.path) > self.max_capture_bytes):
                self._rotate_capture()
        except Exception:
            pass

    def stop_speech(self):
        """Historically sent ESC K to the engine; now a no-op.

        Measured: no escape command interrupts audio already handed to
        SBDD, so the write bought nothing - and it competed with the turbo
        marker files for the IN.TXT channel.  Abandoned speech is dealt
        with by the turbo-fed drain in speak() instead.
        """
        pass


#: A sentence boundary: end punctuation, then whitespace, then something
#: that plausibly starts a sentence.  The lookahead keeps Hungarian
#: ordinal dots together ("2026. augusztus" continues lowercase).
_SENTENCE = re.compile(
    '(?<=[.!?;:])\\s+(?=[0-9\\"\\(A-Z\u00c1\u00c9\u00cd\u00d3\u00d6'
    '\u0150\u00da\u00dc\u0170])')


#: ROBOTVOX reads numbers up to nine digits (hundreds of millions);
#: from ten digits it goes silent - measured, and independent of the
#: value, so it is a digit-count limit, not integer overflow.  Longer
#: runs are spelled out digit by digit, which the engine handles fine
#: and which is how screen readers usually treat such numbers anyway.
_HUGE_NUMBER = re.compile('\\d{10,}')


def _spell_digits(match):
    return ' '.join(match.group())


def split_text(text, limit=MAX_TEXT):
    """Break text into pieces, one sentence each.

    One SENTENCE per piece, not merely one chunk of `limit` characters:
    each submission to the engine is spoken as its own unit, and the
    real-time handoff between submissions lands harmlessly between
    captures.  A piece containing two sentences would instead hold the
    engine's long inter-sentence pause *inside* one capture, where the
    end-of-piece silence detection would mistake it for the end.
    Sentences longer than `limit` are split at commas or spaces.
    """
    text = ' '.join((text or '').split())
    text = _HUGE_NUMBER.sub(_spell_digits, text)
    if not text:
        return []
    out = []
    for sentence in _SENTENCE.split(text):
        while len(sentence) > limit:
            window = sentence[:limit + 1]
            cut = -1
            for seps in (',;:', ''):
                best = -1
                for sep in seps:
                    idx = window.rfind(sep + ' ')
                    if idx > best:
                        best = idx
                if best > limit // 4:
                    cut = best + 1
                    break
            if cut < 0:
                cut = window.rfind(' ')
            if cut <= 0:
                cut = limit
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            out.append(sentence)
    return [p for p in out if p]
