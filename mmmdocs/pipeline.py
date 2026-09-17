"""Orchestration: one isolated classification per file, then a dry-run plan.

Design goal: a small local vision model never sees more than one file at a time.
Each file is classified in its own worker process with its own messages and its
own 1-3 images, so no cross-file context can confuse the title. The orchestrator
(which may be a cloud model) only ever sees compact JSON records.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time

from . import engine, naming, nodes, presets as preset_lib, templates


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_KEYS = (
    "vision_prompt", "vision_system_prompt",
    "orchestrator_prompt", "orchestrator_system_prompt",
    "detection_prompt", "detection_system_prompt",
)

# catalog: no changes; move: re-folder only; rename: rename in place;
# rename-move: both.
MODES = ("catalog", "move", "rename", "rename-move")

DEFAULT_CONFIG = {
    "vision_input": "ollama",          # ollama | openai
    "vision_model": "gemma4:e4b",
    "orchestrator_input": "ollama",    # ollama | openai
    "orchestrator_model": None,        # None -> use vision_model
    "ollama_host": "http://localhost:11434",
    "openai_base_url": "https://api.deepseek.com/v1",
    "openai_api_key": None,
    "workers": 4,
    "profile": "classify",
    "max_pages_vision": 3,
    "sample_text_chars": 4000,
    "manifest_sample": 12,
    "cache_root": None,
    "min_confidence": 0.5,
    "mode": "rename",                  # see MODES
    "name_template": naming.DEFAULT_TEMPLATE,
    # None = use the built-in template in mmmdocs/templates.py. A matching
    # "<key>_file" path may be set instead of inlining a long prompt.
    "vision_prompt": None,
    "vision_system_prompt": None,
    "orchestrator_prompt": None,
    "orchestrator_system_prompt": None,
    # Detection ("what kind of folder is this"): model by default, heuristics
    # always computed as corroboration/fallback. Use "auto" for heuristics first.
    "detection_prompt": None,
    "detection_system_prompt": None,
    "detection_input": None,           # None -> orchestrator_input
    "detection_model": None,           # None -> orchestrator_model
    "detect_method": "model",          # model | auto | heuristic
    "detect_sample": 15,
    # Document presets
    "preset": "books",                 # books | spec-sheet | magazines | <user> | auto | custom
    "presets": {},                     # user-defined presets (see config.example.json)
    "last_directory": None,            # remembered by the TUI
}


def config_path():
    """Portable config lives in the mmmdocs repo, not in $HOME."""
    return os.path.join(REPO_ROOT, "config.json")


def mode_flags(mode):
    """Return (rename, move) for a mode string; unknown modes fall back to rename."""
    mode = (mode or "rename").strip().lower()
    if mode == "catalog":
        return False, False
    if mode == "move":
        return False, True
    if mode == "rename-move":
        return True, True
    return True, False


def load_config(path=None):
    """defaults -> preset -> repo config.json -> explicit path.

    An explicit key always beats the preset, so `preset: wis` plus a custom
    `vision_prompt` means the custom prompt wins. Empty strings reset to default.
    """
    raw = {}
    for candidate in (config_path(), path):
        if candidate and os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    raw.update(json.load(handle))
            except (OSError, ValueError):
                pass
    preset_map = preset_lib.all_presets(raw.get("presets"))
    preset_name = raw.get("preset") or DEFAULT_CONFIG["preset"]
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(raw)
    # A preset fills keys that are unset/empty or still at their default; an
    # explicit non-default value always wins.
    for key, value in preset_lib.preset_defaults(preset_map, preset_name).items():
        if not cfg.get(key) or cfg.get(key) == DEFAULT_CONFIG.get(key):
            cfg[key] = value
    cfg["preset"] = preset_name
    cfg["_presets"] = preset_map
    return normalize_config(cfg)


def normalize_config(cfg):
    """Apply reset-to-default semantics and load any "<key>_file" prompts."""
    cfg["orchestrator_model"] = cfg.get("orchestrator_model") or cfg["vision_model"]
    cfg["detection_input"] = cfg.get("detection_input") or cfg.get("orchestrator_input")
    cfg["detection_model"] = cfg.get("detection_model") or cfg.get("orchestrator_model")
    cfg["name_template"] = cfg.get("name_template") or naming.DEFAULT_TEMPLATE
    if not cfg.get("_presets"):
        cfg["_presets"] = preset_lib.all_presets(cfg.get("presets"))
    for key in PROMPT_KEYS:
        if not cfg.get(key):
            cfg[key] = None
        file_key = key + "_file"
        if not cfg.get(key) and cfg.get(file_key):
            try:
                with open(cfg[file_key], "r", encoding="utf-8") as handle:
                    cfg[key] = handle.read().strip() or None
            except OSError:
                cfg[key] = None
    return cfg


def save_config(cfg, path=None):
    """Merge the non-default settings into config.json; defaults are removed."""
    path = path or config_path()
    existing = _read_json(path) or {}
    for key, default in DEFAULT_CONFIG.items():
        value = cfg.get(key)
        if key == "orchestrator_model" and value == cfg.get("vision_model"):
            existing.pop(key, None)  # derived; do not pin it
            continue
        if value is None or value == default:
            existing.pop(key, None)
        else:
            existing[key] = value
    _write_json(path, existing)
    return path


def apply_active_preset(cfg):
    """Apply the named preset's defaults to keys that are unset/at their default.

    Needed after a CLI `--preset` override, which sets the name too late for
    load_config to apply the prompt/template. Idempotent.
    """
    name = (cfg.get("preset") or "books").strip()
    if name in ("auto", "custom"):
        return name
    preset_map = cfg.get("_presets") or preset_lib.all_presets(cfg.get("presets"))
    for key, value in preset_lib.preset_defaults(preset_map, name).items():
        if not cfg.get(key) or cfg.get(key) == DEFAULT_CONFIG.get(key):
            cfg[key] = value
    return name


def resolve_preset(directory, cfg, allow_model=True):
    """If preset is "auto", detect one and apply it. Returns (name, report)."""
    name = (cfg.get("preset") or "books").strip()
    if name != "auto":
        return name, None
    detect_cfg = dict(cfg)
    if not allow_model:
        detect_cfg["detect_method"] = "heuristic"
    report = preset_lib.detect(directory, detect_cfg)
    chosen = report.get("preset") or "books"
    preset_map = cfg.get("_presets") or preset_lib.all_presets(cfg.get("presets"))
    for key, value in preset_lib.preset_defaults(preset_map, chosen).items():
        if not cfg.get(key) or cfg.get(key) == DEFAULT_CONFIG.get(key):
            cfg[key] = value
    cfg["preset"] = chosen
    return chosen, report


def _backend_opts(cfg, which):
    if which == "openai":
        return {"base_url": cfg["openai_base_url"], "api_key": cfg["openai_api_key"]}
    return {"host": cfg["ollama_host"]}


def _slug_folder(path):
    parts = []
    for raw in str(path).replace("\\", "/").split("/"):
        raw = raw.strip()
        if not raw or raw in (".", ".."):
            continue
        raw = re.sub(r'[<>:"|?*]', "", raw).strip(" .")
        if raw:
            parts.append(raw)
    return os.path.join(*parts) if parts else "Unsorted"


def normalize_record(data, path):
    rec = {"file": os.path.abspath(path)}
    for key in templates.REQUIRED_KEYS:
        rec[key] = data.get(key)
    topics = rec.get("topics")
    if not isinstance(topics, list):
        topics = [topics] if topics else []
    rec["topics"] = [str(t).strip() for t in topics if str(t).strip()][:8]
    try:
        rec["confidence"] = float(rec.get("confidence") or 0.0)
    except (TypeError, ValueError):
        rec["confidence"] = 0.0
    try:
        rec["evidence_pages"] = [int(p) for p in (rec.get("evidence_pages") or [])]
    except (TypeError, ValueError):
        rec["evidence_pages"] = []
    rec["needs_human"] = bool(rec.get("needs_human"))
    rec["title"] = str(rec.get("title") or "").strip()
    rec["author"] = str(rec.get("author") or "").strip()
    # Preserve any extra fields a custom prompt returned (spec sheets, etc.) so
    # they can be used in name_template. Nested objects are dropped.
    known = set(templates.REQUIRED_KEYS) | {"file", "images_used", "bytes_used", "error"}
    for key, value in data.items():
        if key in known or key in rec:
            continue
        if isinstance(value, str):
            rec[key] = value[:500]
        elif value is None or isinstance(value, (int, float, bool)):
            rec[key] = value
        elif isinstance(value, list):
            rec[key] = [str(item)[:200] for item in value[:20]
                        if isinstance(item, (str, int, float, bool))]
    return rec


def classify_record(path, info, cfg):
    """Classify exactly one PDF. Renders <= max_pages_vision images, calls the
    vision node once, and removes its raster cache afterwards."""
    path = os.path.abspath(path)
    total = int(info.get("pages") or 0)
    pages = [p for p in (1, 2, 3) if not total or p <= total]
    if not pages:
        pages = [1]
    render = engine.render_data(
        path, ",".join(str(p) for p in pages), profile=cfg.get("profile", "classify"),
        max_pages=cfg.get("max_pages_vision", 3), cache_root=cfg.get("cache_root"),
    )
    image_paths = [im["path"] for im in render["images"]]
    try:
        excerpt = ""
        if info.get("has_text_layer"):
            try:
                excerpt = engine.text_data(
                    path, pages="1-5", max_chars=cfg.get("sample_text_chars", 4000)
                ).get("text", "")
            except BaseException:
                excerpt = ""
        user = templates.build_classify_user(path, info, excerpt, pages, cfg.get("vision_prompt"))
        raw = nodes.chat(
            cfg["vision_input"], cfg["vision_model"],
            cfg.get("vision_system_prompt") or templates.CLASSIFY_SYSTEM,
            user, image_paths, **_backend_opts(cfg, cfg["vision_input"]),
        )
        data = nodes.parse_json(raw)
        rec = normalize_record(data, path)
        # Namespaced so it never collides with a model-extracted "model" field.
        rec["classified_by"] = cfg["vision_model"]
        rec["images_used"] = len(image_paths)
        rec["bytes_used"] = render["total_bytes"]
        return rec
    finally:
        shutil.rmtree(render["out_dir"], ignore_errors=True)


def _classify_worker(payload):
    path, info, cfg = payload
    try:
        return classify_record(path, info, cfg)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        rec = normalize_record({}, path)
        rec["error"] = "%s: %s" % (type(exc).__name__, exc)
        rec["needs_human"] = True
        rec["classified_by"] = cfg.get("vision_model")
        return rec


def _det_folder(rec):
    doc_type = str(rec.get("doc_type") or "other").replace("-", " ").title()
    topic = ""
    for candidate in rec.get("topics") or []:
        candidate = str(candidate).strip()
        if candidate and candidate.lower() not in ("", "other"):
            topic = candidate.title()
            break
    return _slug_folder(doc_type + ("/" + topic if topic else ""))


def propose_taxonomy(records, cfg):
    titled = [r for r in records if r.get("title")]
    if not titled:
        return {}, "no titles"
    try:
        user = templates.build_taxonomy_user(titled, cfg.get("orchestrator_prompt"))
        raw = nodes.chat(
            cfg["orchestrator_input"], cfg["orchestrator_model"],
            cfg.get("orchestrator_system_prompt") or templates.TAXONOMY_SYSTEM,
            user, **_backend_opts(cfg, cfg["orchestrator_input"]),
        )
        data = nodes.parse_json(raw)
        folders = data.get("folders") or {}
        mapping = {str(k): str(v) for k, v in folders.items()}
        return mapping, str(data.get("reason") or "")
    except BaseException as exc:
        print("[mmmdocs] taxonomy model failed (%s); using deterministic folders" % exc, file=sys.stderr)
        return {}, "deterministic fallback"


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _load_apply_log(root):
    """Map abs(src) -> abs(dest) from the last apply, so moved files can be found."""
    data = _read_json(os.path.join(root, "apply-log.json"))
    index = {}
    for move in (data or {}).get("moves", []):
        if move.get("src") and move.get("dest"):
            index[os.path.abspath(move["src"])] = os.path.abspath(move["dest"])
    return index


def resolve_current_path(record, root, index):
    """Find where a record's file lives now, following any prior apply-log moves."""
    candidate = os.path.abspath(record.get("file") or "")
    if candidate and os.path.exists(candidate):
        return candidate
    cur, seen = candidate, set()
    while cur in index and cur not in seen:
        seen.add(cur)
        cur = index[cur]
    if cur and os.path.exists(cur):
        return cur
    base = os.path.basename(candidate)
    if not base:
        return None
    matches = []
    for dirpath, _dirs, files in os.walk(root):
        if base in files:
            matches.append(os.path.join(dirpath, base))
    return matches[0] if len(matches) == 1 else None


def build_move_plan(root, records, taxonomy, mode="rename", name_template=None, apply_log=None):
    rename, move = mode_flags(mode)
    index = apply_log if apply_log is not None else _load_apply_log(root)
    plan = []
    for rec in records:
        src = resolve_current_path(rec, root, index)
        if not src:
            plan.append({
                "src": rec.get("file"),
                "name": "",
                "original_name": os.path.basename(rec.get("file") or ""),
                "proposed_name": "",
                "proposed_dir": ".",
                "proposed_dest": src,
                "renamed": False,
                "reason": "unresolved path",
                "confidence": rec.get("confidence"),
                "needs_human": True,
                "collision": False,
                "error": "unresolved",
            })
            continue

        original = os.path.basename(src)
        if move:
            folder = _slug_folder(taxonomy.get(rec.get("title")) or _det_folder(rec))
        else:
            rel = os.path.relpath(os.path.dirname(src), root)
            folder = "" if rel == "." else rel

        new_name = naming.render_filename(rec, name_template, original) if rename else original
        flagged = bool(rec.get("needs_human"))
        if not new_name:
            new_name = original
            flagged = True
        if folder:
            dest = os.path.join(root, folder, new_name)
        else:
            dest = os.path.join(root, new_name)
        plan.append({
            "src": src,
            "name": new_name,
            "original_name": original,
            "proposed_name": new_name,
            "proposed_dir": folder or ".",
            "proposed_dest": dest,
            "renamed": new_name != original,
            "reason": ", ".join(rec.get("topics") or []) or rec.get("doc_type") or "",
            "confidence": rec.get("confidence"),
            "needs_human": flagged,
            "collision": os.path.exists(dest) and os.path.abspath(dest) != os.path.abspath(src),
            "error": rec.get("error"),
        })
    return plan


def build_plan_from_catalog(root, cfg):
    """Rebuild move-plan.json from an existing catalog.json. No model calls."""
    root = os.path.abspath(root)
    catalog = _read_json(os.path.join(root, "catalog.json"))
    if not catalog:
        raise SystemExit("no catalog.json in %s (run `mmmdocs run` first)" % root)
    records = catalog.get("records", [])
    preset_name, detection = resolve_preset(root, cfg, allow_model=False)
    rename, move = mode_flags(cfg.get("mode"))
    plan = build_move_plan(root, records, {}, cfg.get("mode"), cfg.get("name_template"))
    meta = {
        "directory": root,
        "preset": preset_name,
        "detection": detection,
        "mode": cfg.get("mode"),
        "name_template": cfg.get("name_template"),
        "renamed": sum(1 for item in plan if item.get("renamed")),
        "count": len(plan),
        "plan": plan,
    }
    _write_json(os.path.join(root, "move-plan.json"), meta)
    return meta


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, ensure_ascii=False)
    return path


def _history_path(root):
    return os.path.join(root, "apply-history.jsonl")


def _read_history(root):
    entries = []
    path = _history_path(root)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        pass
    return entries


def _write_history(root, entries):
    path = _history_path(root)
    if entries:
        with open(path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    elif os.path.exists(path):
        os.remove(path)


def _phase(on_phase, name):
    """Fire a phase callback, ignoring any error it raises."""
    if on_phase:
        try:
            on_phase(name)
        except Exception:
            pass


def run(directory, cfg, only=None, limit=None, keep_duplicates=False, on_phase=None):
    root = os.path.abspath(directory)
    if not os.path.isdir(root):
        raise SystemExit("not a directory: %s" % root)
    if not cfg.get("cache_root"):
        cfg["cache_root"] = os.path.join(root, ".rpdf-cache")

    _phase(on_phase, "detect")
    preset_name, detection = resolve_preset(root, cfg)
    if detection:
        print("[mmmdocs] detected preset %r (%s, %.2f): %s"
              % (preset_name, detection.get("method"), detection.get("confidence", 0.0),
                 detection.get("reason", "")), file=sys.stderr)

    _phase(on_phase, "scan")
    print("[mmmdocs] scanning %s" % root, file=sys.stderr)
    manifest = engine.manifest_data(root, sample=cfg.get("manifest_sample", 12))
    files = manifest["files"]

    if only:
        wanted = set(only)
        files = [f for f in files if os.path.basename(f["path"]) in wanted or f["path"] in wanted]

    skip = set()
    if not keep_duplicates:
        for paths in manifest["duplicate_groups"].values():
            for dup in paths[1:]:
                skip.add(os.path.abspath(dup))

    pending = [f for f in files if os.path.abspath(f["path"]) not in skip]
    if limit:
        pending = pending[:limit]

    _phase(on_phase, "classify")
    print("[mmmdocs] %d files to classify (%d duplicate(s) skipped) with %s"
          % (len(pending), len(skip), cfg["vision_model"]), file=sys.stderr)

    payloads = [(f["path"], dict(f), cfg) for f in pending]
    records = []
    workers = int(cfg.get("workers", 4))
    if workers <= 1 or len(payloads) <= 1:
        for i, payload in enumerate(payloads, 1):
            rec = _classify_worker(payload)
            records.append(rec)
            print("[mmmdocs] %d/%d %s" % (i, len(payloads), os.path.basename(rec["file"])), file=sys.stderr)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_classify_worker, p): p[0] for p in payloads}
            for done, future in enumerate(as_completed(futures), 1):
                rec = future.result()
                records.append(rec)
                flag = " !" if rec.get("error") or rec.get("needs_human") else ""
                print("[mmmdocs] %d/%d %s%s" % (done, len(payloads), os.path.basename(rec["file"]), flag),
                      file=sys.stderr)

    _phase(on_phase, "plan")
    records.sort(key=lambda r: r["file"])
    mode = cfg.get("mode", "rename")
    rename, move = mode_flags(mode)
    if move:
        taxonomy, reason = propose_taxonomy(records, cfg)
    else:
        taxonomy, reason = {}, "not grouping (mode=%s)" % mode
    plan = build_move_plan(root, records, taxonomy, mode, cfg.get("name_template"))

    catalog = {
        "directory": root,
        "vision_input": cfg["vision_input"],
        "vision_model": cfg["vision_model"],
        "orchestrator_input": cfg["orchestrator_input"],
        "orchestrator_model": cfg["orchestrator_model"],
        "preset": preset_name,
        "detection": detection,
        "mode": mode,
        "name_template": cfg.get("name_template"),
        "taxonomy_reason": reason,
        "count": len(records),
        "skipped_duplicates": sorted(skip),
        "records": records,
    }
    catalog_path = _write_json(os.path.join(root, "catalog.json"), catalog)
    plan_path = _write_json(
        os.path.join(root, "move-plan.json"),
        {"directory": root, "mode": mode, "name_template": cfg.get("name_template"),
         "renamed": sum(1 for item in plan if item.get("renamed")),
         "count": len(plan), "plan": plan},
    )
    print("[mmmdocs] wrote %s and %s" % (catalog_path, plan_path), file=sys.stderr)
    return catalog, plan


def _unique_path(dest):
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    i = 2
    while os.path.exists("%s (%d)%s" % (base, i, ext)):
        i += 1
    return "%s (%d)%s" % (base, i, ext)


def apply_plan(root, dry_run=True, force=False, plan_path=None, min_confidence=None):
    root = os.path.abspath(root)
    plan_path = plan_path or os.path.join(root, "move-plan.json")
    if not os.path.exists(plan_path):
        raise SystemExit("no plan at %s (run `mmmdocs run` first)" % plan_path)
    with open(plan_path, "r", encoding="utf-8") as handle:
        plan = json.load(handle).get("plan", [])
    threshold = 0.5 if min_confidence is None else float(min_confidence)

    moves, skipped = [], []
    for item in plan:
        src = item["src"]
        if not os.path.exists(src):
            skipped.append({"src": src, "reason": "missing"})
            continue
        if item.get("needs_human") and not force:
            skipped.append({"src": src, "reason": "needs_human"})
            continue
        if (item.get("confidence") or 0) < threshold and not force:
            skipped.append({"src": src, "reason": "low_confidence"})
            continue
        dest = item["proposed_dest"]
        if os.path.abspath(dest) == os.path.abspath(src):
            skipped.append({"src": src, "reason": "already_in_place"})
            continue
        if not dry_run:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            dest = _unique_path(dest)
            shutil.move(src, dest)
        moves.append({"src": src, "dest": dest, "dir": item.get("proposed_dir")})

    result = {"dry_run": dry_run, "moved": len(moves), "skipped": len(skipped), "moves": moves,
              "skip_reasons": skipped}
    if not dry_run:
        result["log"] = _write_json(os.path.join(root, "apply-log.json"), result)
        if moves:
            history = _read_history(root)
            history.append({"moves": moves})
            _write_history(root, history)
    return result


def undo_plan(root, dry_run=True):
    """Reverse the most recent apply, newest move first.

    Prefers the apply-history stack so successive applies can be undone one at a
    time; falls back to a lone apply-log.json from older versions.
    """
    root = os.path.abspath(root)
    history = _read_history(root)
    if history:
        moves_source = history[-1].get("moves", [])
    else:
        log = _read_json(os.path.join(root, "apply-log.json"))
        moves_source = (log or {}).get("moves", [])
    if not moves_source:
        raise SystemExit("nothing to undo in %s (no apply history)" % root)

    moves, skipped = [], []
    for move in reversed(moves_source):
        dest = os.path.abspath(move.get("dest") or "")
        src = os.path.abspath(move.get("src") or "")
        if not dest or not os.path.exists(dest):
            skipped.append({"dest": dest, "reason": "missing"})
            continue
        if os.path.exists(src) and os.path.abspath(src) != dest:
            skipped.append({"src": src, "reason": "target_exists"})
            continue
        if not dry_run:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(dest, src)
            parent = os.path.dirname(dest)
            while os.path.abspath(parent) != root and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
                parent = os.path.dirname(parent)
        moves.append({"src": dest, "dest": src})

    result = {"dry_run": dry_run, "restored": len(moves), "skipped": len(skipped),
              "moves": moves, "skip_reasons": skipped}
    if not dry_run:
        if history:
            remaining = history[:-1]
            _write_history(root, remaining)
            if remaining:
                prev = remaining[-1].get("moves", [])
                _write_json(os.path.join(root, "apply-log.json"),
                            {"dry_run": False, "moved": len(prev), "moves": prev})
            else:
                try:
                    os.remove(os.path.join(root, "apply-log.json"))
                except OSError:
                    pass
        else:
            _write_json(os.path.join(root, "apply-log.json"),
                        {"dry_run": False, "undone": True, "moves": []})
        result["log"] = _write_json(os.path.join(root, "undo-log.json"), result)
    return result


def _bench_sample(files, sample):
    if sample <= 0 or sample >= len(files):
        return files
    ordered = sorted(files, key=lambda f: f.get("size_bytes") or 0)
    step = max(1, len(ordered) // sample)
    picked = ordered[::step][:sample]
    return picked


def bench(directory, models, cfg, sample=5, on_phase=None):
    root = os.path.abspath(directory)
    _phase(on_phase, "scan")
    manifest = engine.manifest_data(root, sample=cfg.get("manifest_sample", 12))
    files = _bench_sample(manifest["files"], sample)
    _phase(on_phase, "classify")
    results = {}
    for model in models:
        cfg2 = dict(cfg)
        cfg2["vision_model"] = model
        rows = []
        for i, f in enumerate(files, 1):
            t0 = time.time()
            rec = _classify_worker((f["path"], dict(f), cfg2))
            seconds = round(time.time() - t0, 2)
            rows.append({
                "file": os.path.basename(f["path"]),
                "seconds": seconds,
                "json_ok": "error" not in rec,
                "needs_human": bool(rec.get("needs_human")),
                "confidence": rec.get("confidence"),
                "title": rec.get("title"),
                "images_used": rec.get("images_used"),
                "bytes_used": rec.get("bytes_used"),
            })
            print("[mmmdocs] %s %d/%d %s %.2fs" % (model, i, len(files), rows[-1]["file"], seconds),
                  file=sys.stderr)
        ok = [r for r in rows if r["json_ok"]]
        results[model] = {
            "files": len(rows),
            "json_ok_rate": round(len(ok) / len(rows), 3) if rows else 0.0,
            "needs_human_rate": round(sum(1 for r in rows if r["needs_human"]) / len(rows), 3) if rows else 0.0,
            "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 2) if rows else 0.0,
            "avg_bytes": int(sum(r["bytes_used"] or 0 for r in rows) / len(rows)) if rows else 0,
            "rows": rows,
        }
    out = _write_json(os.path.join(root, "bench.json"),
                      {"directory": root, "vision_input": cfg["vision_input"], "models": results})
    print("[mmmdocs] wrote %s" % out, file=sys.stderr)
    return results
