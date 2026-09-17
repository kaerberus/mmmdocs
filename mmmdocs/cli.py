"""mmmdocs command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import engine, pipeline, presets, progress


def _apply_overrides(cfg, args):
    for flag, key in (
        ("vision_model", "vision_model"),
        ("vision_input", "vision_input"),
        ("orchestrator_model", "orchestrator_model"),
        ("orchestrator_input", "orchestrator_input"),
        ("ollama_host", "ollama_host"),
        ("openai_base_url", "openai_base_url"),
        ("openai_api_key", "openai_api_key"),
        ("profile", "profile"),
        ("mode", "mode"),
        ("name_template", "name_template"),
        ("vision_prompt", "vision_prompt"),
        ("vision_system_prompt", "vision_system_prompt"),
        ("orchestrator_prompt", "orchestrator_prompt"),
        ("orchestrator_system_prompt", "orchestrator_system_prompt"),
        ("preset", "preset"),
        ("detect_method", "detect_method"),
        ("embed_model", "embed_model"),
        ("scan_method", "scan_method"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            cfg[key] = value
    if getattr(args, "workers", None) is not None:
        cfg["workers"] = args.workers
    if getattr(args, "cache_root", None) is not None:
        cfg["cache_root"] = args.cache_root
    return cfg


def _load(args):
    cfg = pipeline.load_config(getattr(args, "config", None))
    cfg = pipeline.normalize_config(_apply_overrides(cfg, args))
    pipeline.apply_active_preset(cfg)
    return cfg


def cmd_tui(args):
    from .tui import run_tui
    run_tui([])


def cmd_manifest(args):
    engine.emit(engine.manifest_data(args.dir, args.recursive, args.pattern, args.sample))


def cmd_info(args):
    engine.emit(engine.info_data(args.pdf))


def cmd_text(args):
    engine.emit(engine.text_data(args.pdf, args.pages, args.head, args.max_chars))


def cmd_render(args):
    engine.emit(engine.render_data(
        args.pdf, args.pages, args.profile, args.dpi, args.max_edge,
        args.format, args.quality, args.max_pages, args.cache_root,
    ))


def cmd_cleanup(args):
    engine.cmd_cleanup(args)


def cmd_classify_one(args):
    cfg = _load(args)
    with progress.Spinner("Detecting"):
        pipeline.resolve_preset(os.path.dirname(os.path.abspath(args.file)), cfg)
    info = engine.info_data(args.file)
    with progress.Spinner("Classifying"):
        rec = pipeline._classify_worker((args.file, info, cfg))
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_presets(args):
    cfg = _load(args)
    preset_map = cfg.get("_presets") or presets.all_presets(cfg.get("presets"))
    print("active: %s\n" % cfg.get("preset"))
    for name in sorted(preset_map):
        body = preset_map[name]
        origin = "built-in" if name in presets.BUILTIN_PRESETS else "user"
        print("%-14s %-9s %s" % (name, origin, body.get("label", "")))
        if body.get("description"):
            print("               %s" % body["description"])
        print("               template: %s" % body.get("name_template", ""))


def cmd_detect(args):
    cfg = _load(args)
    with progress.Spinner("Detecting"):
        report = presets.detect(args.dir, cfg)
    print(json.dumps(report, indent=2, ensure_ascii=False))


def cmd_scan(args):
    cfg = _load(args)
    if getattr(args, "limit", None):
        cfg["scan_max"] = args.limit
    with progress.Spinner("Scanning"):
        report = presets.embed_scan(args.dir, cfg)
    path = presets.save_scan_groups(args.dir, report)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    print("embedded %d/%d files (%d with no text) using %s"
          % (report["embedded"], report["files"], report["no_text"], report["embed_model"]))
    print("mixed folder: %s   clusters: %d" % (report["mixed"], len(report["clusters"])))
    for group in report["groups"]:
        fields = ",".join(group["fields"]) if group["fields"] else "-"
        print("  %-28s %5d  preset=%s  fields=%s"
              % (group["label"][:28], group["count"], group["preset"] or "-", fields))
    if report["duplicates"]:
        print("near-duplicates: %d pair(s)" % len(report["duplicates"]))
    if report["outliers"]:
        print("outliers: %d" % len(report["outliers"]))
    print("groups written to %s" % path)


def cmd_run(args):
    cfg = _load(args)
    spin = progress.Spinner("Detecting")

    def phase(name):
        if name == "detect":
            spin.start()
        else:
            spin.stop()

    try:
        if getattr(args, "groups", False):
            with progress.Spinner("Scanning"):
                scan = presets.embed_scan(args.dir, cfg)
            cfg["_scan_cache"] = scan
            groups = presets.groups_from_scan(scan, cfg)
            catalog, plan = pipeline.run_groups(args.dir, cfg, groups, on_phase=phase)
        else:
            catalog, plan = pipeline.run(
                args.dir, cfg, only=args.only, limit=args.limit,
                keep_duplicates=args.keep_duplicates, on_phase=phase,
            )
    finally:
        spin.stop()
    renamed = sum(1 for item in plan if item.get("renamed"))
    summary = [
        "%d classified, %d planned (%d renamed, mode=%s)"
        % (catalog["count"], len(plan), renamed, catalog.get("mode")),
    ]
    flagged = [r for r in catalog["records"] if r.get("error") or r.get("needs_human")]
    if flagged:
        summary.append("%d need review:" % len(flagged))
        for rec in flagged:
            summary.append("  - %s" % os.path.basename(rec["file"]))
    print("\n".join(summary))


def cmd_plan(args):
    cfg = _load(args)
    meta = pipeline.build_plan_from_catalog(args.dir, cfg)
    print("mode=%s renamed=%d planned=%d -> %s/move-plan.json"
          % (meta["mode"], meta["renamed"], meta["count"], os.path.abspath(args.dir)))


def cmd_undo(args):
    with progress.Spinner("Undoing"):
        result = pipeline.undo_plan(args.dir, dry_run=not args.yes)
    mode = "DRY-RUN" if result["dry_run"] else "UNDONE"
    print("%s: %d restored, %d skipped" % (mode, result["restored"], result["skipped"]))
    for move in result["moves"]:
        print("  %s -> %s" % (os.path.basename(move["src"]), os.path.basename(move["dest"])))
    if result["dry_run"]:
        print("  (dry-run; pass --yes to undo)")


def cmd_bench(args):
    cfg = _load(args)
    models = [m.strip() for m in args.vision_models.split(",") if m.strip()]
    spin = progress.Spinner("Scanning")

    def phase(name):
        if name == "scan":
            spin.start()
        else:
            spin.stop()

    try:
        results = pipeline.bench(args.dir, models, cfg, sample=args.sample, on_phase=phase)
    finally:
        spin.stop()
    for model, r in results.items():
        print("%-24s json_ok=%s needs_human=%s avg=%ss avg_bytes=%s" % (
            model, r["json_ok_rate"], r["needs_human_rate"], r["avg_seconds"], r["avg_bytes"]))


def cmd_apply(args):
    with progress.Spinner("Applying"):
        result = pipeline.apply_plan(
            args.dir, dry_run=not args.yes, force=args.force,
            plan_path=args.plan, min_confidence=args.min_confidence,
        )
    mode = "DRY-RUN" if result["dry_run"] else "APPLIED"
    print("%s: %d file(s), %d skipped" % (mode, result["moved"], result["skipped"]))
    for move in result["moves"]:
        old = os.path.basename(move["src"])
        new = os.path.basename(move["dest"])
        label = new if new == old else "%s  ->  %s" % (old, new)
        folder = move.get("dir")
        if folder and folder not in (".", os.path.dirname(move["src"])):
            label = "%s  [%s]" % (label, folder)
        print("  %s" % label)
    if result["skip_reasons"]:
        counts = {}
        for skip in result["skip_reasons"]:
            counts[skip["reason"]] = counts.get(skip["reason"], 0) + 1
        print("  skipped: " + ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))
    if result["dry_run"]:
        print("  (dry-run; pass --yes to apply)")


def _add_model_flags(p):
    p.add_argument("--config", help="JSON config file")
    p.add_argument("--vision-model")
    p.add_argument("--vision-input", choices=["ollama", "openai"])
    p.add_argument("--orchestrator-model")
    p.add_argument("--orchestrator-input", choices=["ollama", "openai"])
    p.add_argument("--ollama-host")
    p.add_argument("--openai-base-url")
    p.add_argument("--openai-api-key")
    p.add_argument("--profile", choices=sorted(engine.PROFILES.keys()))
    p.add_argument("--workers", type=int)
    p.add_argument("--cache-root")
    p.add_argument("--embed-model")
    p.add_argument("--scan-method", dest="scan_method", choices=["auto", "embedding", "model"])


def _add_organize_flags(p):
    p.add_argument("--mode", choices=list(pipeline.MODES),
                   help="catalog | move | rename | rename-move (default rename)")
    p.add_argument("--name-template", dest="name_template",
                   help="filename template, e.g. \"{author} - {title} ({year})\"; empty resets to default")


def _add_preset_flag(p):
    p.add_argument("--preset",
                   help="document preset (books, spec-sheet, magazines, or a user-defined one) "
                        "or 'auto' to detect")
    p.add_argument("--detect-method", dest="detect_method",
                   choices=["model", "auto", "heuristic"],
                   help="how detection decides (default model)")


def _add_prompt_flags(p):
    p.add_argument("--vision-prompt", dest="vision_prompt",
                   help="override the per-file classifier instruction; empty resets to default")
    p.add_argument("--vision-system-prompt", dest="vision_system_prompt",
                   help="override the classifier system message; empty resets to default")
    p.add_argument("--orchestrator-prompt", dest="orchestrator_prompt",
                   help="override the taxonomy instruction; empty resets to default")
    p.add_argument("--orchestrator-system-prompt", dest="orchestrator_system_prompt",
                   help="override the taxonomy system message; empty resets to default")


def build_parser():
    parser = argparse.ArgumentParser(prog="mmmdocs", description="Catalog PDFs with a local vision model")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("tui", help="launch the interactive menu")
    p.set_defaults(func=cmd_tui)

    p = sub.add_parser("manifest", help="compact metadata for every PDF in a directory")
    p.add_argument("dir")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--pattern", default="*.pdf")
    p.add_argument("--sample", type=int, default=12)
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("info", help="inspect one PDF")
    p.add_argument("pdf")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("text", help="extract text from one PDF")
    p.add_argument("pdf")
    p.add_argument("--pages")
    p.add_argument("--head", type=int, default=5)
    p.add_argument("--max-chars", type=int, default=12000, dest="max_chars")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("render", help="rasterize pages")
    p.add_argument("pdf")
    p.add_argument("--pages", required=True)
    p.add_argument("--profile", choices=sorted(engine.PROFILES.keys()), default="default")
    p.add_argument("--dpi", type=int)
    p.add_argument("--max-edge", type=int, dest="max_edge")
    p.add_argument("--format", choices=["png", "jpg"])
    p.add_argument("--quality", type=int)
    p.add_argument("--max-pages", type=int, default=engine.DEFAULT_MAX_PAGES, dest="max_pages")
    p.add_argument("--cache-root", dest="cache_root")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("classify-one", help="classify a single PDF and print its JSON record")
    p.add_argument("file")
    _add_model_flags(p)
    _add_prompt_flags(p)
    _add_preset_flag(p)
    p.set_defaults(func=cmd_classify_one)

    p = sub.add_parser("run", help="classify a directory and write catalog.json + move-plan.json")
    p.add_argument("dir")
    p.add_argument("--only", action="append", help="limit to a file name (repeatable)")
    p.add_argument("--limit", type=int)
    p.add_argument("--keep-duplicates", action="store_true", dest="keep_duplicates")
    p.add_argument("--groups", action="store_true",
                   help="scan first, then classify per group with each group's preset/schema")
    _add_model_flags(p)
    _add_organize_flags(p)
    _add_prompt_flags(p)
    _add_preset_flag(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("plan", help="rebuild move-plan.json from catalog.json (no model)")
    p.add_argument("dir")
    p.add_argument("--config", help="JSON config file")
    _add_organize_flags(p)
    _add_preset_flag(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("presets", help="list document presets (built-in and user-defined)")
    p.add_argument("--config", help="JSON config file")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("detect", help="suggest the best preset for a folder")
    p.add_argument("dir")
    _add_model_flags(p)
    _add_preset_flag(p)
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("scan", help="fast embedding scan: clusters, mixed groups, duplicates")
    p.add_argument("dir")
    p.add_argument("--limit", type=int, help="max files to embed (default: all)")
    p.add_argument("--json", action="store_true", help="print the full JSON report")
    _add_model_flags(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("bench", help="compare vision models on a sample of files")
    p.add_argument("dir")
    p.add_argument("--vision-models", required=True, dest="vision_models",
                   help="comma-separated model names, e.g. gemma4:e2b,gemma4:e4b,gemma4:12b")
    p.add_argument("--sample", type=int, default=5)
    _add_model_flags(p)
    _add_prompt_flags(p)
    _add_preset_flag(p)
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("apply", help="apply move-plan.json (dry-run unless --yes)")
    p.add_argument("dir")
    p.add_argument("--yes", action="store_true", help="actually move files")
    p.add_argument("--force", action="store_true", help="move/low-confidence/needs_human too")
    p.add_argument("--plan", help="path to move-plan.json")
    p.add_argument("--min-confidence", type=float, dest="min_confidence")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("undo", help="reverse the last apply (dry-run unless --yes)")
    p.add_argument("dir")
    p.add_argument("--yes", action="store_true", help="actually move files back")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("cleanup", help="remove cached renders")
    p.add_argument("--pdf")
    p.add_argument("--cache-root", dest="cache_root")
    p.set_defaults(func=cmd_cleanup)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
