#!/usr/bin/env python3
"""Generate per-model opencode agent variants from one base agent file.

Keeps a single source of truth for the agent prompt while exposing a distinct
`rpdf-<model>` / `librarian-<model>` agent per candidate, so models can be
benchmarked by switching `subagent_type` instead of editing config.

Usage:
    python3 tools/make_agents.py [--agent-dir ~/.config/opencode/agent] [--check]
"""
from __future__ import annotations

import argparse
import os
import re
import sys

VARIANTS = {
    "rpdf": {
        "e2b": "ollama/gemma4:e2b",
        "e4b": "ollama/gemma4:e4b",
        "12b": "ollama/gemma4:12b",
        "26b": "ollama/gemma4:26b-mxfp8",
        "31b": "ollama/gemma4:31b-nvfp4",
    },
    "librarian": {
        "deepseek": "deepseek/deepseek-flash",
        "qwen27b": "ollama/qwen3.8:27b-nvfp4",
        "gemma31b": "ollama/gemma4:31b-nvfp4",
    },
}


def rewrite(base_text, model):
    text, sep, rest = base_text.partition("\n---\n")
    if not sep:
        raise SystemExit("base agent file has no frontmatter block")
    front = text
    front = re.sub(r"^model:.*$", "model: %s" % model, front, flags=re.MULTILINE)
    front = re.sub(
        r'^(description:.*?)(\s*)$',
        lambda m: '%s Model: %s.%s' % (m.group(1).rstrip("."), model, m.group(2)),
        front,
        count=1,
        flags=re.MULTILINE,
    )
    return front + "\n---\n" + rest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-dir", default=os.path.expanduser("~/.config/opencode/agent"))
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    args = parser.parse_args(argv)

    drift = 0
    for base_name, models in VARIANTS.items():
        base_path = os.path.join(args.agent_dir, base_name + ".md")
        if not os.path.exists(base_path):
            print("skip %s (no base file)" % base_name, file=sys.stderr)
            continue
        with open(base_path, "r", encoding="utf-8") as handle:
            base_text = handle.read()
        for slug, model in models.items():
            out_path = os.path.join(args.agent_dir, "%s-%s.md" % (base_name, slug))
            new_text = rewrite(base_text, model)
            old_text = None
            if os.path.exists(out_path):
                with open(out_path, "r", encoding="utf-8") as handle:
                    old_text = handle.read()
            if old_text == new_text:
                continue
            drift += 1
            if args.check:
                print("drift: %s" % out_path)
            else:
                with open(out_path, "w", encoding="utf-8") as handle:
                    handle.write(new_text)
                print("wrote: %s" % out_path)
    if args.check and drift == 0:
        print("all variants up to date")
    return 1 if (args.check and drift) else 0


if __name__ == "__main__":
    sys.exit(main())
