"""A small, dependency-free terminal UI.

Everything is a numbered menu so the whole workflow (pick folder, scan,
classify, preview, apply) can be driven without memorising flags. It is plain
ANSI + input(), so it works over SSH and on any Python 3.9+.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap

from . import naming, templates
from .deps import ask, info, ok, warn, _c, ollama_reachable


PROMPT_LABELS = [
    ("vision_prompt", "Vision instruction", templates.CLASSIFY_INSTRUCTION),
    ("vision_system_prompt", "Vision system", templates.CLASSIFY_SYSTEM),
    ("orchestrator_prompt", "Orchestrator instruction", templates.TAXONOMY_INSTRUCTION),
    ("orchestrator_system_prompt", "Orchestrator system", templates.TAXONOMY_SYSTEM),
    ("detection_prompt", "Detection instruction", templates.DETECTION_INSTRUCTION),
    ("detection_system_prompt", "Detection system", templates.DETECTION_SYSTEM),
]


def _clear():
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def _pause():
    try:
        input(_c("\nPress Enter to continue...", "90"))
    except (EOFError, KeyboardInterrupt):
        print()


def _config_path(argv):
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return None


def _apply_cli_overrides(argv, cfg):
    mapping = {
        "--vision-model": "vision_model",
        "--vision-input": "vision_input",
        "--orchestrator-model": "orchestrator_model",
        "--orchestrator-input": "orchestrator_input",
        "--ollama-host": "ollama_host",
        "--openai-base-url": "openai_base_url",
        "--openai-api-key": "openai_api_key",
        "--profile": "profile",
    }
    for i, arg in enumerate(argv):
        if arg in mapping and i + 1 < len(argv):
            cfg[mapping[arg]] = argv[i + 1]
        elif arg == "--workers" and i + 1 < len(argv):
            cfg["workers"] = int(argv[i + 1])
        elif arg == "--cache-root" and i + 1 < len(argv):
            cfg["cache_root"] = argv[i + 1]
    return cfg


def _shorten(path, width=54):
    if len(path) <= width:
        return path
    return "..." + path[-(width - 3):]


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


try:
    import readline as _readline
except Exception:  # pragma: no cover - Windows/some builds
    _readline = None


class _PathCompleter:
    """Tab-completes filesystem paths, preserving a leading ~ and spaces."""

    def __init__(self):
        self._text = None
        self._matches = []

    def __call__(self, text, state):
        if state == 0 or text != self._text:
            self._text = text
            self._matches = self._build(text)
        return self._matches[state] if state < len(self._matches) else None

    @staticmethod
    def _build(text):
        if text == "" or text.endswith(os.sep):
            dirpart, base = text, ""
        else:
            dirpart, base = os.path.split(text)
        try:
            entries = os.listdir(os.path.expanduser(dirpart or "."))
        except OSError:
            return []
        matches = []
        for name in sorted(entries):
            if name.startswith(base):
                candidate = os.path.join(dirpart, name) if dirpart else name
                if os.path.isdir(os.path.expanduser(candidate)):
                    candidate += os.sep
                matches.append(candidate)
        return matches


def _readline_input(prompt):
    """input() with path tab-completion when readline is available."""
    if _readline is None:
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""
    old_completer = _readline.get_completer()
    old_delims = _readline.get_completer_delims()
    _readline.set_completer(_PathCompleter())
    _readline.set_completer_delims("\t\n")
    for bind in ("bind ^I rl_complete", "tab: complete"):
        try:
            _readline.parse_and_bind(bind)
        except Exception:
            pass
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    finally:
        _readline.set_completer(old_completer)
        _readline.set_completer_delims(old_delims)


class App:
    def __init__(self, argv):
        from . import pipeline
        self.pipeline = pipeline
        self.cfg = _apply_cli_overrides(argv, pipeline.load_config(_config_path(argv)))
        last = self.cfg.get("last_directory")
        cwd = os.path.abspath(os.getcwd())
        self.directory = os.path.abspath(os.path.expanduser(last)) if last and os.path.isdir(os.path.expanduser(last)) else cwd
        self.host = self.cfg.get("ollama_host") or "http://localhost:11434"
        self.reachable, self.models = ollama_reachable(self.host)

    # ---------------------------------------------------------------- helpers
    def catalog(self):
        return _read_json(os.path.join(self.directory, "catalog.json"))

    def plan(self):
        return _read_json(os.path.join(self.directory, "move-plan.json"))

    def _safe(self, label, fn):
        try:
            return fn()
        except KeyboardInterrupt:
            print()
            warn("Cancelled.")
            _pause()
        except BaseException as exc:
            warn("%s failed: %s: %s" % (label, type(exc).__name__, exc))
            _pause()
        return None

    # ------------------------------------------------------------------ views
    def header(self):
        print(_c("mmmdocs", "1;36") + _c("  —  local vision PDF cataloger", "90"))
        print(_c("  folder      ", "90") + _shorten(self.directory))
        print(_c("  vision      ", "90") + "%s (%s)" % (self.cfg.get("vision_model"), self.cfg.get("vision_input")))
        print(_c("  orchestrator", "90") + " %s (%s)" % (self.cfg.get("orchestrator_model"), self.cfg.get("orchestrator_input")))
        print(_c("  preset      ", "90") + " %s%s" % (self.cfg.get("preset"), " (customized)" if self._preset_customized() else ""))
        print(_c("  detect      ", "90") + " %s" % self.cfg.get("detect_method"))
        print(_c("  workers     ", "90") + str(self.cfg.get("workers")))
        state = "up, %d models" % len(self.models) if self.reachable else "offline (vision unavailable)"
        print(_c("  ollama      ", "90") + state)
        catalog = self.catalog()
        if catalog:
            print(_c("  catalog     ", "90") + "%d records" % catalog.get("count", 0))
        print()

    def menu(self):
        print("  0  YOLO: detect, build and organize everything")
        print("  1  Automatically detect & build plan")
        print("  2  Review plan")
        print("  3  Apply plan")
        print("  4  View catalog")
        print("  5  Scan folder (manifest, no model)")
        print("  6  Rebuild plan from catalog (no model)")
        print("  7  Document type / preset")
        print("  8  Choose another directory   (%s)" % _shorten(self.directory, 40))
        print("  9  Settings")
        print("  c  Clean raster cache")
        print("  u  Undo last apply")
        print("  q  Quit")

    # ---------------------------------------------------------------- presets
    def _preset_map(self):
        return self.cfg.get("_presets") or self.pipeline.preset_lib.all_presets(self.cfg.get("presets"))

    def _preset_customized(self):
        body = self._preset_map().get(self.cfg.get("preset")) or {}
        template = body.get("name_template")
        return bool(template) and template != self.cfg.get("name_template")

    def _apply_preset(self, name):
        self.cfg.update(self.pipeline.preset_lib.preset_defaults(self._preset_map(), name))
        self.cfg["preset"] = name
        self.pipeline.normalize_config(self.cfg)

    def _detect_and_suggest(self):
        report = self._safe("detect", lambda: self.pipeline.preset_lib.detect(self.directory, self.cfg))
        if report is None:
            return None
        name = report.get("preset")
        body = self._preset_map().get(name) or {}
        print("\nDetected: %s (%s, %.2f) \u2014 %s" % (
            body.get("label", name), report.get("method"), report.get("confidence", 0.0),
            report.get("reason", "")))
        if name and name != self.cfg.get("preset") and name != "custom":
            if ask("Switch preset to %r (%s)?" % (name, body.get("name_template", "")), default=True):
                self._apply_preset(name)
                ok("Preset set to %s" % name)
        return report

    def choose_preset(self):
        while True:
            _clear()
            self.header()
            print(_c("Presets", "1"))
            preset_map = self._preset_map()
            names = sorted(preset_map, key=lambda n: (n not in self.pipeline.preset_lib.BUILTIN_PRESETS, n))
            for i, name in enumerate(names, 1):
                body = preset_map[name]
                mark = " *" if name == self.cfg.get("preset") else ""
                origin = "" if name in self.pipeline.preset_lib.BUILTIN_PRESETS else " [user]"
                print("  %2d  %-14s%s %s%s" % (i, name, origin, body.get("label", ""), mark))
                if body.get("name_template"):
                    print("      %s%s" % (_c("template: ", "90"), body["name_template"]))
            print("  n   New preset")
            print("  e   Edit a user preset")
            print("  x   Delete a user preset")
            print("  d   Detect automatically  (method: %s)" % self.cfg.get("detect_method"))
            print("  m   Detection method")
            print("  b   Back")
            choice = input("> ").strip().lower()
            if choice == "b" or not choice:
                return
            if choice == "n":
                self._new_preset()
                continue
            if choice == "e":
                self._edit_preset()
                continue
            if choice == "x":
                self._delete_preset()
                continue
            if choice == "d":
                self._detect_and_suggest()
                _pause()
                continue
            if choice == "m":
                print("methods: model (always ask), auto (heuristics first), heuristic (no model)")
                value = input("Detection method: ").strip()
                if value in ("model", "auto", "heuristic"):
                    self.cfg["detect_method"] = value
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(names):
                self._apply_preset(names[int(choice) - 1])
                ok("Preset set to %s" % self.cfg.get("preset"))
                _pause()

    # ---------------------------------------------------------- preset builder
    def _refresh_presets(self):
        self.cfg["_presets"] = self.pipeline.preset_lib.all_presets(self.cfg.get("presets"))

    def _first_pdf(self):
        try:
            pdfs = sorted(n for n in os.listdir(self.directory) if n.lower().endswith(".pdf"))
            return os.path.join(self.directory, pdfs[0]) if pdfs else ""
        except OSError:
            return ""

    def _pick_user_preset(self):
        names = sorted(self.cfg.get("presets") or {})
        if not names:
            warn("No user presets yet (n to create one).")
            _pause()
            return None
        presets = self.cfg.get("presets") or {}
        for i, name in enumerate(names, 1):
            print("  %d  %-18s %s" % (i, name, presets[name].get("label", "")))
        print("  b  Back")
        choice = input("> ").strip().lower()
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            return names[int(choice) - 1]
        return None

    def _new_preset(self):
        _clear()
        self.header()
        print(_c("New preset", "1"))
        name = input("Preset name: ").strip()
        if not name:
            warn("Cancelled.")
            _pause()
            return
        raw = input("Fields (comma-separated, in order): ").strip()
        fields = [f.strip() for f in raw.split(",") if f.strip()]
        if not fields:
            warn("No fields given.")
            _pause()
            return
        draft = self.pipeline.preset_lib.build_from_fields(name, fields)
        self._preset_accept(draft, is_new=True)

    def _edit_preset(self):
        _clear()
        self.header()
        print(_c("Edit preset", "1"))
        pid = self._pick_user_preset()
        if not pid:
            return
        draft = dict((self.cfg.get("presets") or {})[pid])
        self._preset_accept(draft, is_new=False, preset_id=pid)

    def _delete_preset(self):
        _clear()
        self.header()
        print(_c("Delete preset", "1"))
        pid = self._pick_user_preset()
        if not pid:
            return
        if not ask("Delete preset %r?" % pid, default=False):
            return
        presets = dict(self.cfg.get("presets") or {})
        presets.pop(pid, None)
        self.cfg["presets"] = presets
        if self.cfg.get("preset") == pid:
            self.cfg["preset"] = "books"
        self._refresh_presets()
        path = self.pipeline.save_config(self.cfg)
        ok("Deleted %r (%s)" % (pid, path))
        _pause()

    def _preset_accept(self, draft, is_new=False, preset_id=None):
        preset_lib = self.pipeline.preset_lib
        while True:
            _clear()
            self.header()
            pid = preset_id or preset_lib.make_id(draft.get("label"))
            fields = preset_lib.fields_from_prompt(draft.get("vision_prompt"))
            print(_c("Preset: %s" % pid, "1"))
            print("  label:       %s" % draft.get("label"))
            print("  filename:    %s" % draft.get("name_template"))
            print("  mode:        %s" % draft.get("mode"))
            print("  description: %s" % draft.get("description"))
            print(_c("  prompt:", "90"))
            for line in textwrap.wrap(draft.get("vision_prompt") or "", 100)[:10]:
                print("    %s" % line)
            ghost = preset_lib.unknown_placeholders(draft.get("name_template"), fields)
            if ghost:
                warn("template uses fields the prompt may not return: %s" % ", ".join(ghost))
            print("\n  e edit prompt   f edit filename   d edit description   t test   s save   c cancel")
            choice = input("> ").strip().lower()
            if choice == "c" or not choice:
                return
            if choice == "e":
                edited = self._edit_in_editor(draft.get("vision_prompt") or "")
                if edited:
                    draft["vision_prompt"] = edited
            elif choice == "f":
                print("available: %s" % ", ".join("{%s}" % f for f in fields))
                value = input("Filename template [%s]: " % draft.get("name_template")).strip()
                if value:
                    draft["name_template"] = value
            elif choice == "d":
                value = input("Description [%s]: " % draft.get("description")).strip()
                if value:
                    draft["description"] = value
            elif choice == "t":
                self._test_preset(draft, pid)
            elif choice == "s":
                self._save_preset(pid, draft, is_new)
                return

    def _test_preset(self, draft, pid):
        default = self._first_pdf()
        raw = _readline_input("Test file [%s]: " % (os.path.basename(default) or "none"))
        path = os.path.abspath(os.path.expanduser(raw)) if raw else default
        if not path or not os.path.isfile(path):
            warn("No file to test.")
            _pause()
            return
        if not self.reachable:
            warn("Ollama is offline; cannot test now.")
            _pause()
            return
        cfg = dict(self.cfg)
        presets_copy = {k: dict(v) for k, v in (self.cfg.get("presets") or {}).items()}
        presets_copy[pid] = draft
        cfg["presets"] = presets_copy
        cfg["_presets"] = self.pipeline.preset_lib.all_presets(presets_copy)
        cfg["preset"] = pid
        # Use the draft verbatim, ignoring any session overrides.
        cfg["vision_prompt"] = draft.get("vision_prompt")
        cfg["name_template"] = draft.get("name_template")
        if "vision_system_prompt" in draft:
            cfg["vision_system_prompt"] = draft.get("vision_system_prompt")
        self.pipeline.normalize_config(cfg)
        info = self._safe("info", lambda: self.pipeline.engine.info_data(path))
        if info is None:
            return
        rec = self._safe("test", lambda: self.pipeline.classify_record(path, info, cfg))
        if rec is None:
            return
        name = self.pipeline.naming.render_filename(rec, draft.get("name_template"), os.path.basename(path))
        print(_c("\nExtracted:", "1"))
        for key in self.pipeline.preset_lib.fields_from_prompt(draft.get("vision_prompt")):
            print("  %s: %r" % (key, rec.get(key)))
        print("  needs_human: %s" % rec.get("needs_human"))
        print(_c("\nFilename: %s" % (name or "(kept original: %s)" % os.path.basename(path)), "32"))
        _pause()

    def _save_preset(self, pid, draft, is_new):
        presets = self.cfg.get("presets") or {}
        if is_new and pid in presets:
            if not ask("Preset %r already exists. Overwrite?" % pid, default=False):
                return
        if is_new and pid in self.pipeline.preset_lib.BUILTIN_PRESETS:
            if not ask("%r shadows a built-in. Continue?" % pid, default=True):
                return
        presets = dict(presets)
        presets[pid] = draft
        self.cfg["presets"] = presets
        # Activate: clear the preset-owned overrides so the preset is authoritative.
        self.cfg["vision_prompt"] = None
        self.cfg["vision_system_prompt"] = None
        self.cfg["name_template"] = naming.DEFAULT_TEMPLATE
        self.cfg["mode"] = "rename"
        self.cfg["preset"] = pid
        self._refresh_presets()
        self.pipeline.normalize_config(self.cfg)
        self.pipeline.apply_active_preset(self.cfg)
        path = self.pipeline.save_config(self.cfg)
        ok("Saved preset %r to %s" % (pid, path))
        _pause()

    # ---------------------------------------------------------------- startup
    def startup(self):
        _clear()
        print(_c("mmmdocs", "1;36") + _c("  —  local vision PDF cataloger", "90"))
        if not self.reachable:
            warn("Ollama is offline; classification will not work.")
        self.prompt_directory()
        print(_c("  preset: %s  |  model: %s" % (self.cfg.get("preset"), self.cfg.get("vision_model")), "90"))
        print(_c("  start with 0 (YOLO) or 1 (auto-build & plan)\n", "90"))

    def prompt_directory(self):
        while True:
            raw = _readline_input(_c("Directory [%s]: " % _shorten(self.directory, 70), "1"))
            path = os.path.abspath(os.path.expanduser(raw)) if raw else self.directory
            if os.path.isdir(path):
                self.directory = path
                if self.cfg.get("last_directory") != path:
                    self.cfg["last_directory"] = path
                    try:
                        self.pipeline.save_config(self.cfg)
                    except Exception:
                        pass
                return path
            warn("Not a directory: %s" % path)

    def _pdf_count(self):
        try:
            return sum(1 for name in os.listdir(self.directory)
                       if name.lower().endswith(".pdf")
                       and os.path.isfile(os.path.join(self.directory, name)))
        except OSError:
            return 0

    def _page(self, lines, per=20):
        for i, line in enumerate(lines, 1):
            print(line)
            if i % per == 0 and i != len(lines):
                try:
                    more = input(_c("  -- Enter for more (%d/%d), q to stop --" % (i, len(lines)), "90")).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                if more == "q":
                    return

    # ---------------------------------------------------------------- actions
    def choose_directory(self):
        self.prompt_directory()
        ok("Directory set to %s" % self.directory)
        _pause()

    def auto_build(self):
        report = self._safe("detect", lambda: self.pipeline.preset_lib.detect(self.directory, self.cfg))
        if report is None:
            return
        chosen = report.get("preset") or "books"
        self._apply_preset(chosen)
        body = self._preset_map().get(chosen) or {}
        print("\nDetected: %s (%s, %.2f) \u2014 %s" % (
            body.get("label", chosen), report.get("method"), report.get("confidence", 0.0),
            report.get("reason", "")))
        print("Using preset %r  |  template: %s" % (chosen, self.cfg.get("name_template")))
        count = self._pdf_count()
        if count == 0:
            warn("No PDFs in %s" % self.directory)
            _pause()
            return
        if not ask("Classify %d file(s) with %s and build the plan?" % (count, self.cfg.get("vision_model")), default=True):
            return
        _clear()
        self.header()
        result = self._safe("classify", lambda: self.pipeline.run(self.directory, self.cfg))
        if result is None:
            return
        catalog, plan = result
        flagged = [r for r in catalog["records"] if r.get("needs_human") or r.get("error")]
        ok("%d classified, %d planned" % (catalog["count"], len(plan)))
        if flagged:
            warn("%d need review" % len(flagged))
        print("Press 2 to review the plan, 3 to apply.")
        _pause()

    def yolo(self):
        report = self._safe("detect", lambda: self.pipeline.preset_lib.detect(self.directory, self.cfg))
        if report is None:
            return
        chosen = report.get("preset") or "books"
        self._apply_preset(chosen)
        body = self._preset_map().get(chosen) or {}
        count = self._pdf_count()
        print(_c("\nYOLO", "1;33"))
        print("  directory : %s" % self.directory)
        print("  detected  : %s (%s, %.2f)" % (
            body.get("label", chosen), report.get("method"), report.get("confidence", 0.0)))
        print("  preset    : %s" % chosen)
        print("  template  : %s" % self.cfg.get("name_template"))
        print("  mode      : %s" % self.cfg.get("mode"))
        print("  model     : %s   workers: %s   files: %d" % (
            self.cfg.get("vision_model"), self.cfg.get("workers"), count))
        print(_c("  effect    : classify, then rename/move ALL files (force)", "33"))
        if count == 0:
            warn("No PDFs in %s" % self.directory)
            _pause()
            return
        if not ask("Proceed?", default=False):
            return
        _clear()
        self.header()
        result = self._safe("yolo", lambda: self.pipeline.run(self.directory, self.cfg))
        if result is None:
            return
        catalog, _plan = result
        applied = self._safe("apply", lambda: self.pipeline.apply_plan(
            self.directory, dry_run=False, force=True, min_confidence=self.cfg.get("min_confidence")))
        if applied is None:
            return
        ok("%d classified; applied %d change(s), %d skipped" % (
            catalog["count"], applied["moved"], applied["skipped"]))
        if applied["skip_reasons"]:
            counts = {}
            for skip in applied["skip_reasons"]:
                counts[skip["reason"]] = counts.get(skip["reason"], 0) + 1
            print("  skipped: " + ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))
        print(_c("  Undo: press u (or run `mmmdocs undo <dir>`).", "90"))
        _pause()

    def review_plan(self):
        catalog = self.catalog()
        if not catalog or not self.plan():
            _clear()
            self.header()
            warn("No catalog/plan yet. Use 1 (auto-build) or 0 (YOLO).")
            _pause()
            return
        _clear()
        self.header()
        records = catalog["records"]
        flagged = [r for r in records if r.get("needs_human") or r.get("error")]
        print(_c("Catalog", "1") + "  %d records, %d flagged" % (len(records), len(flagged)))
        for rec in flagged[:8]:
            print("  ! %s" % os.path.basename(rec["file"])[:80])
        if len(flagged) > 8:
            print("  ... and %d more" % (len(flagged) - 8))
        result = self._safe("plan", lambda: self.pipeline.apply_plan(
            self.directory, dry_run=True, min_confidence=self.cfg.get("min_confidence")))
        if result is None:
            return
        print(_c("\nProposed changes — %d file(s), %d skipped" % (result["moved"], result["skipped"]), "1"))
        by_dir = {}
        for move in result["moves"]:
            old = os.path.basename(move["src"])
            new = os.path.basename(move["dest"])
            by_dir.setdefault(move.get("dir") or ".", []).append((old, new))
        lines = []
        for folder in sorted(by_dir):
            lines.append(_c("  %s/" % folder, "36"))
            for old, new in by_dir[folder]:
                if old == new:
                    lines.append("    %s" % new[:100])
                else:
                    lines.append("    %s" % old[:48])
                    lines.append(_c("      -> %s" % new[:100], "32"))
        self._page(lines)
        print("\nPress 3 to apply.")
        _pause()

    def undo_last(self):
        result = self._safe("undo", lambda: self.pipeline.undo_plan(self.directory, dry_run=True))
        if result is None:
            return
        _clear()
        self.header()
        print(_c("Undo last apply — %d file(s) would be restored" % result["restored"], "1"))
        for move in result["moves"][:30]:
            print("  %s -> %s" % (os.path.basename(move["src"]), os.path.basename(move["dest"])))
        if not result["moves"]:
            _pause()
            return
        if ask("Restore these %d file(s)?" % result["restored"], default=False):
            applied = self._safe("undo", lambda: self.pipeline.undo_plan(self.directory, dry_run=False))
            if applied is not None:
                ok("Restored %d file(s)." % applied["restored"])
        _pause()

    def scan(self):
        from . import engine
        manifest = self._safe("scan", lambda: engine.manifest_data(
            self.directory, sample=self.cfg.get("manifest_sample", 12)))
        if manifest is None:
            return
        files = manifest["files"]
        scans = [f for f in files if not f.get("has_text_layer")]
        with_toc = [f for f in files if f.get("toc_source") == "real"]
        errors = [f for f in files if f.get("error")]
        _clear()
        self.header()
        print(_c("Scan result", "1"))
        print("  PDFs        %d" % len(files))
        print("  text layer  %d" % (len(files) - len(scans)))
        print("  scans/image %d" % len(scans))
        print("  real TOC    %d" % len(with_toc))
        if errors:
            print(_c("  unreadable  %d" % len(errors), "33"))
        if manifest["duplicate_groups"]:
            print(_c("\nDuplicate groups (kept first, rest skipped):", "33"))
            for key, paths in manifest["duplicate_groups"].items():
                print("  %s" % key)
                for path in paths:
                    print("    - %s" % os.path.basename(path))
        _pause()

    def classify(self):
        total = 0
        manifest_catalog = self.catalog()
        if manifest_catalog:
            total = manifest_catalog.get("count", 0)
        print("This will classify every PDF in:")
        print("  %s" % self.directory)
        print("using %s (workers=%s)." % (self.cfg.get("vision_model"), self.cfg.get("workers")))
        if total:
            print(_c("An existing catalog.json (%d records) will be overwritten." % total, "33"))
        if not ask("Continue?", default=True):
            return
        _clear()
        self.header()
        result = self._safe("classify", lambda: self.pipeline.run(self.directory, self.cfg))
        if result is None:
            return
        catalog, plan = result
        records = catalog["records"]
        flagged = [r for r in records if r.get("needs_human") or r.get("error")]
        ok("%d classified, %d planned" % (catalog["count"], len(plan)))
        if flagged:
            warn("%d need review:" % len(flagged))
            for rec in flagged:
                print("   - %s" % os.path.basename(rec["file"]))
        else:
            print("   (nothing flagged; use 5 to preview the moves)")
        _pause()

    def view_catalog(self):
        catalog = self.catalog()
        _clear()
        self.header()
        if not catalog:
            warn("No catalog.json yet. Run option 3 first.")
            _pause()
            return
        print(_c("%-44s %-20s %-16s %5s %s" % ("TITLE", "AUTHOR", "TYPE", "CONF", "FLAG"), "1"))
        print("-" * 96)
        for rec in catalog["records"]:
            flag = "review" if rec.get("needs_human") else ("error" if rec.get("error") else "")
            print("%-44s %-20s %-16s %5s %s" % (
                (rec.get("title") or "?")[:44],
                (rec.get("author") or "")[:20],
                (rec.get("doc_type") or "")[:16],
                rec.get("confidence"),
                flag,
            ))
        print(_c("\n%s" % catalog.get("taxonomy_reason", ""), "90"))
        _pause()

    def preview_plan(self, dry_run=True):
        plan = self.plan()
        if not plan:
            _clear()
            self.header()
            warn("No move-plan.json yet. Run option 3 first.")
            _pause()
            return None
        result = self._safe("plan", lambda: self.pipeline.apply_plan(
            self.directory, dry_run=dry_run, min_confidence=self.cfg.get("min_confidence")))
        if result is None:
            return None
        _clear()
        self.header()
        mode = "DRY-RUN" if result["dry_run"] else "APPLIED"
        print(_c("%s — %d file(s), %d skipped" % (mode, result["moved"], result["skipped"]), "1"))
        by_dir = {}
        for move in result["moves"]:
            old = os.path.basename(move["src"])
            new = os.path.basename(move["dest"])
            by_dir.setdefault(move.get("dir") or ".", []).append((old, new))
        for folder in sorted(by_dir):
            print(_c("\n  %s/" % folder, "36"))
            for old, new in by_dir[folder]:
                if old == new:
                    print("    %s" % new[:100])
                else:
                    print("    %s" % old[:48])
                    print(_c("      -> %s" % new[:100], "32"))
        if result["skip_reasons"]:
            counts = {}
            for skip in result["skip_reasons"]:
                counts[skip["reason"]] = counts.get(skip["reason"], 0) + 1
            print(_c("\n  skipped: " + ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())), "90"))
        return result

    def apply_plan(self):
        result = self.preview_plan(dry_run=True)
        if not result or not result["moves"]:
            _pause()
            return
        if not ask("Apply these %d change(s) now?" % result["moved"], default=False):
            return
        applied = self._safe("apply", lambda: self.pipeline.apply_plan(
            self.directory, dry_run=False, min_confidence=self.cfg.get("min_confidence")))
        if applied is not None:
            ok("Changed %d file(s). Log: apply-log.json" % applied["moved"])
        _pause()

    def rebuild_plan(self):
        meta = self._safe("plan", lambda: self.pipeline.build_plan_from_catalog(
            self.directory, self.cfg))
        if meta is None:
            return
        _clear()
        self.header()
        ok("Plan rebuilt: mode=%s, %d renamed of %d (no model used)"
           % (meta["mode"], meta["renamed"], meta["count"]))
        if meta["renamed"]:
            for item in meta["plan"][:12]:
                if item.get("renamed"):
                    print("  %s\n    -> %s" % (item["original_name"][:48], item["proposed_name"][:80]))
            if meta["renamed"] > 12:
                print("  ... and %d more" % (meta["renamed"] - 12))
        _pause()

    def settings(self):
        while True:
            _clear()
            self.header()
            print(_c("Settings", "1"))
            print("  1  Vision model       (%s)" % self.cfg.get("vision_model"))
            print("  2  Vision backend     (%s)" % self.cfg.get("vision_input"))
            print("  3  Orchestrator model (%s)" % self.cfg.get("orchestrator_model"))
            print("  4  Orchestrator backend (%s)" % self.cfg.get("orchestrator_input"))
            print("  5  Workers            (%s)" % self.cfg.get("workers"))
            print("  6  Render profile     (%s)" % self.cfg.get("profile"))
            print("  7  Ollama host        (%s)" % self.host)
            print("  8  Mode               (%s)" % self.cfg.get("mode"))
            print("  9  Name template      (%s)" % self.cfg.get("name_template"))
            print("  0  Preset            (%s)" % self.cfg.get("preset"))
            print("  p  Prompts            (%d, per node)" % len(PROMPT_LABELS))
            print("  s  Save settings      -> config.json")
            print("  b  Back")
            choice = input("> ").strip().lower()
            if choice == "b" or not choice:
                return
            if choice == "0":
                self.choose_preset()
                continue
            if choice == "p":
                self.edit_prompts()
                continue
            if choice == "s":
                path = self.pipeline.save_config(self.cfg)
                ok("Saved to %s" % path)
                _pause()
                continue
            if choice == "1":
                self.cfg["vision_model"] = input("Vision model: ").strip() or self.cfg["vision_model"]
            elif choice == "2":
                value = input("Backend (ollama/openai): ").strip()
                if value in ("ollama", "openai"):
                    self.cfg["vision_input"] = value
            elif choice == "3":
                self.cfg["orchestrator_model"] = input("Orchestrator model: ").strip() or self.cfg["orchestrator_model"]
            elif choice == "4":
                value = input("Backend (ollama/openai): ").strip()
                if value in ("ollama", "openai"):
                    self.cfg["orchestrator_input"] = value
            elif choice == "5":
                value = input("Workers: ").strip()
                if value.isdigit():
                    self.cfg["workers"] = int(value)
            elif choice == "6":
                print("profiles: low, classify, default, high")
                value = input("Profile: ").strip()
                if value in ("low", "classify", "default", "high"):
                    self.cfg["profile"] = value
            elif choice == "7":
                self.cfg["ollama_host"] = input("Ollama host: ").strip() or self.host
                self.host = self.cfg["ollama_host"]
                self.reachable, self.models = ollama_reachable(self.host)
            elif choice == "8":
                print("modes: catalog (no changes), move (re-folder), rename (in place), rename-move (both)")
                value = input("Mode: ").strip()
                if value in ("catalog", "move", "rename", "rename-move"):
                    self.cfg["mode"] = value
            elif choice == "9":
                print("fields: {title} {author} {year} {doc_type} {language} {topics}")
                print("plus any custom field your vision prompt returns")
                print("default: %s" % naming.DEFAULT_TEMPLATE)
                value = input("Name template (empty resets to default): ").strip()
                self.cfg["name_template"] = value or naming.DEFAULT_TEMPLATE
                ok("Name template = %s" % self.cfg["name_template"])

    def edit_prompts(self):
        while True:
            _clear()
            self.header()
            print(_c("Prompts", "1") + _c("  (empty = reset, 'edit' = $EDITOR)", "90"))
            for i, (key, label, _default) in enumerate(PROMPT_LABELS, 1):
                state = "custom" if self.cfg.get(key) else "default"
                print("  %d  %-24s (%s)" % (i, label, state))
            print("  b  Back")
            choice = input("> ").strip().lower()
            if choice == "b" or not choice:
                return
            if choice.isdigit() and 1 <= int(choice) <= len(PROMPT_LABELS):
                key, label, default = PROMPT_LABELS[int(choice) - 1]
                self._edit_prompt(key, label, default)

    def _edit_prompt(self, key, label, default):
        current = self.cfg.get(key) or default
        print(_c("\nold prompt:", "1") + _c("  (%s)" % ("custom" if self.cfg.get(key) else "built-in default"), "90"))
        for line in textwrap.wrap(current, 100)[:24]:
            print("  %s" % line)
        if len(current) > 2400:
            print("  ... (%d chars total)" % len(current))
        value = input(_c("\nnew prompt: ", "1")).strip()
        if not value:
            self.cfg[key] = None
            ok("Reset %s to default" % label)
        elif value.lower() in ("edit", "e"):
            edited = self._edit_in_editor(current)
            self.cfg[key] = edited or None
            ok("Reset %s to default" % label if not edited else "Updated %s" % label)
        else:
            self.cfg[key] = value
            ok("Updated %s" % label)
        _pause()

    def _edit_in_editor(self, text):
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        handle, path = tempfile.mkstemp(prefix="mmmdocs-prompt-", suffix=".txt")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            subprocess.call([editor, path])
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            warn("Could not open editor (%s): %s" % (editor, exc))
            return ""
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def clean_cache(self):
        cache_root = self.cfg.get("cache_root") or os.path.join(self.directory, ".rpdf-cache")
        if not os.path.isdir(cache_root):
            _clear()
            self.header()
            info("No cache at %s" % cache_root)
            _pause()
            return
        if ask("Delete rendered images under %s?" % cache_root, default=True):
            removed = 0
            for name in os.listdir(cache_root):
                path = os.path.join(cache_root, name)
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            ok("Removed %d cached folder(s)." % removed)
            _pause()

    # ------------------------------------------------------------------- loop
    def run(self):
        actions = {
            "0": self.yolo,
            "1": self.auto_build,
            "2": self.review_plan,
            "3": self.apply_plan,
            "4": self.view_catalog,
            "5": self.scan,
            "6": self.rebuild_plan,
            "7": self.choose_preset,
            "8": self.choose_directory,
            "9": self.settings,
            "c": self.clean_cache,
            "u": self.undo_last,
        }
        while True:
            _clear()
            self.header()
            self.menu()
            try:
                choice = input("\n> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if choice in ("q", "quit", "exit"):
                break
            action = actions.get(choice)
            if action:
                action()


def run_tui(argv=None):
    argv = argv or []
    if not sys.stdin.isatty():
        print("No interactive terminal detected. Use `mmmdocs --help` for commands.")
        return
    try:
        app = App(argv)
        app.startup()
        app.run()
    except KeyboardInterrupt:
        print()
