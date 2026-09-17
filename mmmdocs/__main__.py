import json
import os
import sys

from .deps import ensure

REPO_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def _preparse(argv):
    cfg = {"ollama_host": "http://localhost:11434", "vision_model": "gemma4:e4b"}
    # The portable repo config, then an explicit --config, then CLI flags.
    paths = [REPO_CONFIG]
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            paths.append(argv[i + 1])
        elif arg.startswith("--config="):
            paths.append(arg.split("=", 1)[1])
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                cfg.update(json.load(handle))
        except Exception:
            pass
    for i, arg in enumerate(argv):
        if arg == "--ollama-host" and i + 1 < len(argv):
            cfg["ollama_host"] = argv[i + 1]
        elif arg == "--vision-model" and i + 1 < len(argv):
            cfg["vision_model"] = argv[i + 1]
    return cfg


def main():
    argv = sys.argv[1:]
    cfg = _preparse(argv)
    wants_tui = not argv or argv[0] in ("tui", "ui", "menu")

    if not ensure(cfg, verbose=wants_tui):
        raise SystemExit(1)

    if wants_tui:
        from .tui import run_tui
        run_tui(argv)
    else:
        from .cli import main as cli_main
        cli_main(argv)


if __name__ == "__main__":
    main()
