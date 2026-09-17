#!/usr/bin/env bash
# mmmdocs installer: clone, install the one dependency, and put `mmmdocs` on PATH.
#
#   curl -fsSL https://raw.githubusercontent.com/kaerberus/mmmdocs/main/install.sh | bash
#
# Env overrides: MMMDOCS_DIR, MMMDOCS_REPO, MMMDOCS_BRANCH, PYTHON,
# MMMDOCS_PULL_MODEL=1 (also pulls embeddinggemma if Ollama is present).
set -euo pipefail

REPO_URL="${MMMDOCS_REPO:-https://github.com/kaerberus/mmmdocs.git}"
BRANCH="${MMMDOCS_BRANCH:-main}"
INSTALL_DIR="${MMMDOCS_DIR:-$HOME/.local/share/mmmdocs}"
BIN_DIR="$HOME/.local/bin"

say() { printf '\033[36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33m!\033[0m %s\n' "$1" >&2; }

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  warn "python3 not found (mmmdocs needs Python 3.9+)."
  exit 1
fi
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  warn "Python 3.9+ is required."
  exit 1
fi
say "Python $("$PY" -c 'import platform; print(platform.python_version())')"

# Use a local checkout when the script is run from one, otherwise clone/update.
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/mmmdocs/__main__.py" ]; then
  INSTALL_DIR="$SCRIPT_DIR"
  say "Using existing checkout at $INSTALL_DIR"
else
  if ! command -v git >/dev/null 2>&1; then
    warn "git is required to install."
    exit 1
  fi
  if [ -d "$INSTALL_DIR/.git" ]; then
    say "Updating $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only --quiet || warn "Could not update; using the local copy."
  else
    say "Cloning into $INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
fi

# The only Python dependency.
if "$PY" -c "import fitz" >/dev/null 2>&1; then
  say "PyMuPDF already available"
else
  say "Installing PyMuPDF"
  "$PY" -m pip install --user pymupdf \
    || "$PY" -m pip install pymupdf \
    || "$PY" -m pip install --break-system-packages pymupdf \
    || warn "Could not install PyMuPDF automatically; run: $PY -m pip install pymupdf"
fi

# Put `mmmdocs` on PATH via ~/.local/bin.
mkdir -p "$BIN_DIR"
chmod +x "$INSTALL_DIR/bin/mmmdocs" 2>/dev/null || true
ln -sf "$INSTALL_DIR/bin/mmmdocs" "$BIN_DIR/mmmdocs"
say "Linked $BIN_DIR/mmmdocs"

RC=""
case "$(basename "${SHELL:-}")" in
  zsh) RC="$HOME/.zshrc" ;;
  bash)
    RC="$HOME/.bashrc"
    [ -f "$HOME/.bash_profile" ] && RC="$HOME/.bash_profile"
    ;;
esac
MARKER="# added by mmmdocs installer"
if [ -n "$RC" ] && ! grep -qF "$MARKER" "$RC" 2>/dev/null; then
  {
    printf '\n%s\n' "$MARKER"
    printf 'export MMMDOCS_HOME="%s"\n' "$INSTALL_DIR"
    printf 'case ":$PATH:" in *":%s:"*) ;; *) export PATH="%s:$PATH" ;; esac\n' "$BIN_DIR" "$BIN_DIR"
  } >>"$RC"
  say "Added $BIN_DIR to PATH in $RC (open a new shell or run: source $RC)"
else
  say "PATH entry already present (add $BIN_DIR manually if needed)"
fi

if [ "${MMMDOCS_PULL_MODEL:-0}" = "1" ] && command -v ollama >/dev/null 2>&1; then
  say "Pulling embeddinggemma"
  ollama pull embeddinggemma || warn "embedding model pull failed"
fi

say "Done."
echo
echo "  cd /path/to/pdfs && mmmdocs   # opens that folder in the TUI"
echo "  mmmdocs --help                # commands"
echo
echo "First run checks Ollama and offers to pull gemma4:e4b (the vision model)."
