"""A tiny progress spinner for silent, blocking steps.

On a terminal it animates `Label....... 12s` on stderr (growing dots + elapsed
seconds) and finishes with `Label... done (12.4s)`. When the stream is not a TTY
(pipes, CI, JSON consumers) it prints the label once and never animates, so
stdout stays clean.

Usage:
    with Spinner("Detecting"):
        do_slow_thing()

    # or manual control (e.g. stop when real progress output begins):
    spin = Spinner("Scanning"); spin.start()
    ...
    spin.stop()
"""
from __future__ import annotations

import sys
import threading
import time


class Spinner:
    def __init__(self, label, stream=None, interval=0.35):
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self.interval = interval
        self.active = False
        self._stop = threading.Event()
        self._thread = None
        self._start_time = None
        self._isatty = bool(getattr(self.stream, "isatty", lambda: False)())

    def start(self):
        if self.active:
            return self
        self.active = True
        self._stop.clear()
        self._start_time = time.time()
        if self._isatty:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            try:
                print("%s..." % self.label, file=self.stream, flush=True)
            except Exception:
                pass
        return self

    def _spin(self):
        dots = 1
        while not self._stop.wait(self.interval):
            elapsed = int(time.time() - self._start_time)
            try:
                self.stream.write("\r%s%s %ds   " % (self.label, "." * dots, elapsed))
                self.stream.flush()
            except Exception:
                return
            dots += 1

    def _clear(self):
        try:
            self.stream.write("\r" + " " * 80 + "\r")
            self.stream.flush()
        except Exception:
            pass

    def stop(self):
        if not self.active:
            return
        self.active = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        if self._isatty:
            elapsed = time.time() - self._start_time if self._start_time else 0.0
            self._clear()
            try:
                print("%s... done (%.1fs)" % (self.label, elapsed), file=self.stream, flush=True)
            except Exception:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False
