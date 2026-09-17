"""Startup dependency bootstrap.

This module must stay importable with nothing but the standard library, because
it runs *before* PyMuPDF (and therefore `mmmdocs.engine`) can be imported. It
checks Python, PyMuPDF, Ollama and the vision model, offers to install whatever
is missing, and reports whether the essentials are satisfied.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request

MIN_PYTHON = (3, 9)


def _color_enabled():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text, code):
    if not _color_enabled():
        return text
    return "\033[%sm%s\033[0m" % (code, text)


_VERBOSE = True


def set_verbose(value):
    global _VERBOSE
    _VERBOSE = bool(value)


def info(message):
    if _VERBOSE:
        print(_c("==> ", "36") + message)


def warn(message):
    # Warnings always go to stderr so they never corrupt JSON on stdout.
    print(_c("! ", "33") + message, file=sys.stderr)


def ok(message):
    if _VERBOSE:
        print(_c("✓ ", "32") + message)


def terminal_width(fallback=80):
    try:
        return max(20, shutil.get_terminal_size((fallback, 24)).columns)
    except Exception:
        return fallback


def ask(prompt, default=True):
    """Print the question (wrapped, so a long prompt never wraps inside input())
    then read a short answer. Avoids readline redraw jumbles on narrow terminals."""
    suffix = "[Y/n]" if default else "[y/N]"
    width = terminal_width()
    for paragraph in (prompt.splitlines() or [prompt]):
        for line in textwrap.wrap(paragraph, width=width) or [paragraph]:
            print(_c(line, "1"))
    try:
        answer = input("  %s > " % suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #

def python_ok():
    return sys.version_info >= MIN_PYTHON


def pymupdf_importable():
    try:
        import fitz  # noqa: F401
        return True
    except Exception:
        return False


def _pip_install(package):
    """Install a package, tolerating venv / externally-managed environments."""
    attempts = [
        [sys.executable, "-m", "pip", "install", "--user", package],
        [sys.executable, "-m", "pip", "install", package],
        [sys.executable, "-m", "pip", "install", "--break-system-packages", package],
    ]
    for cmd in attempts:
        try:
            if subprocess.call(cmd) == 0:
                importlib.invalidate_caches()
                return True
        except OSError:
            continue
    return False


def ollama_reachable(host="http://localhost:11434", timeout=3):
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return True, [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return False, []


def _has_ollama_binary():
    return shutil.which("ollama") is not None


def _start_ollama(host, attempts=10):
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    for _ in range(attempts):
        time.sleep(1)
        reachable, models = ollama_reachable(host)
        if reachable:
            return True
    return False


def _model_present(model, models):
    wanted = {model, model + ":latest", model.split(":")[0] + ":latest"}
    return any(m in wanted or m == model for m in models)


def _pull_model(model):
    try:
        return subprocess.call(["ollama", "pull", model]) == 0
    except OSError:
        return False


def pull_model(model):
    """Pull an Ollama model (used by the TUI to offer the embedding model)."""
    return _pull_model(model)


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #

def ensure(cfg, interactive=None, verbose=True):
    """Check and (if permitted) install dependencies.

    Returns True when the essentials (Python + PyMuPDF) are usable. Ollama is
    offered if missing but is not fatal, so the metadata-only commands and the
    TUI still work without a running model server.

    With verbose=False (used before explicit subcommands) progress lines are
    suppressed and only warnings are emitted, to stderr, so stdout stays clean.
    """
    set_verbose(verbose)
    if interactive is None:
        interactive = sys.stdin.isatty()

    def confirm(prompt, default=True):
        if not interactive:
            return False
        return ask(prompt, default)

    essentials = True

    if not python_ok():
        warn("Python %d.%d+ is required (you have %s)." % (MIN_PYTHON + (sys.version.split()[0],)))
        return False
    ok("Python %s" % sys.version.split()[0])

    if not pymupdf_importable():
        warn("PyMuPDF is not installed (needed to read PDFs).")
        if confirm("Install PyMuPDF now?"):
            info("Installing PyMuPDF ...")
            if _pip_install("pymupdf") and pymupdf_importable():
                ok("PyMuPDF installed")
            else:
                warn("Could not install PyMuPDF automatically.")
        if not pymupdf_importable():
            warn("Install it manually with:  %s -m pip install pymupdf" % sys.executable)
            essentials = False
    else:
        ok("PyMuPDF")

    host = cfg.get("ollama_host") or "http://localhost:11434"
    reachable, models = ollama_reachable(host)
    if not reachable:
        if _has_ollama_binary():
            warn("Ollama is installed but not responding at %s." % host)
            if confirm("Start `ollama serve` now?"):
                info("Starting Ollama ...")
                if _start_ollama(host):
                    ok("Ollama is up")
                    reachable, models = ollama_reachable(host)
        else:
            warn("Ollama is not installed: https://ollama.com/download")
            warn("Without it, only `manifest`, `info`, `text` and `render` work.")
    if reachable:
        ok("Ollama at %s (%d model%s)" % (host, len(models), "" if len(models) == 1 else "s"))
        model = cfg.get("vision_model") or "gemma4:e4b"
        if not _model_present(model, models):
            warn("Vision model %r is not pulled." % model)
            if confirm("Pull %s now? (can be several GB)" % model):
                info("Pulling %s ..." % model)
                if _pull_model(model):
                    ok("Pulled %s" % model)
                else:
                    warn("Pull failed; `mmmdocs run` will fail until it is available.")
        else:
            ok("Vision model %s" % model)

    return essentials
