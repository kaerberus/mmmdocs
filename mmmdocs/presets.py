"""Document presets and folder detection.

A preset bundles the settings that differ between document types (the vision
prompt, the filename template, the organize mode), so a user picks "what kind of
folder is this" instead of knowing template syntax. Built-ins ship with mmmdocs;
users add their own under `presets` in config.json.

Detection samples the text layer only (no rendering), scores presets with cheap
signatures, and (by default) asks the detection node to choose with a
configurable prompt. Heuristics are always computed as corroboration/fallback.
"""
from __future__ import annotations

import json
import os
import re

from . import engine


SPEC_SHEET_PROMPT = """You are given ONE technical document (datasheet, manual, or spec sheet).
Read the printed model/type designation, any serial or part number, the manufacturer, and the document title.
Return ONLY JSON:
{
  "model": "model or type designation, or \\"\\"",
  "serial": "serial or part number, or \\"\\"",
  "part_number": "part number, or \\"\\"",
  "manufacturer": "manufacturer, or \\"\\"",
  "title": "document title, or \\"\\"",
  "doc_type": "spec-sheet",
  "confidence": 0.0,
  "needs_human": false,
  "notes": "one short sentence or \\"\\""
}
Output JSON only."""

MAGAZINE_PROMPT = """You are given ONE magazine or journal issue.
Read the printed masthead title, the issue number, volume, and cover date.
Return ONLY JSON:
{
  "title": "printed magazine/journal title",
  "issue": "issue number, or \\"\\"",
  "volume": "volume, or \\"\\"",
  "year": "cover year, or \\"\\"",
  "publisher": "publisher, or \\"\\"",
  "doc_type": "magazine-issue",
  "confidence": 0.0,
  "needs_human": false,
  "notes": "one short sentence or \\"\\""
}
Output JSON only."""


BUILTIN_PRESETS = {
    "books": {
        "label": "Books / monographs",
        "description": "Bibliographic records: title, author, year. Uses the built-in prompt.",
        "name_template": "{author} - {title} ({year})",
        "mode": "rename",
        "vision_prompt": None,
        "vision_system_prompt": None,
        "detect": {"keywords": ["isbn", "table of contents", "contents", "preface",
                                 "chapter", "published by"], "min_ratio": 0.3},
    },
    "spec-sheet": {
        "label": "Spec sheets / datasheets",
        "description": "Technical documents keyed by model and serial/part number.",
        "name_template": "{model} - {serial}",
        "mode": "rename",
        "vision_prompt": SPEC_SHEET_PROMPT,
        "vision_system_prompt": None,
        "detect": {"keywords": ["datasheet", "data sheet", "part number", "serial",
                                 "voltage", "specification", "operating instructions"],
                   "min_ratio": 0.3},
    },
    "magazines": {
        "label": "Magazine issues",
        "description": "Periodicals keyed by title and year.",
        "name_template": "{title} ({year})",
        "mode": "rename",
        "vision_prompt": MAGAZINE_PROMPT,
        "vision_system_prompt": None,
        "detect": {"keywords": ["issue", "vol.", "no.", "issn", "editorial"],
                   "min_ratio": 0.3},
    },
}

# Keys a preset is allowed to set on the config.
_PRESET_KEYS = ("name_template", "mode", "vision_prompt", "vision_system_prompt")


def all_presets(user_presets=None):
    """Built-ins merged with user-defined presets (user wins on name clash)."""
    merged = {name: dict(body) for name, body in BUILTIN_PRESETS.items()}
    for name, body in (user_presets or {}).items():
        if isinstance(body, dict):
            merged[name] = dict(body)
    return merged


def preset_defaults(preset_map, name):
    """The config keys a preset sets. Unknown/"custom"/"auto" -> {}."""
    if not name or name in ("custom", "auto"):
        return {}
    body = preset_map.get(name)
    if not body:
        return {}
    return {key: body[key] for key in _PRESET_KEYS if key in body}


def _natural_key(name):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def _pdfs(directory):
    names = []
    try:
        entries = os.listdir(directory)
    except OSError:
        return names
    for name in entries:
        if name.lower().endswith(".pdf") and os.path.isfile(os.path.join(directory, name)):
            names.append(name)
    names.sort(key=_natural_key)
    return names


def digest(directory, max_files=15, chars=400):
    """Text-only sample: `max_files` evenly spread PDFs, first pages only."""
    names = _pdfs(directory)
    if not names:
        return []
    if len(names) > max_files:
        step = len(names) / float(max_files)
        picked = [names[int(i * step)] for i in range(max_files)]
    else:
        picked = names
    out = []
    for name in picked:
        try:
            text = engine.text_data(os.path.join(directory, name), pages="1-2",
                                    max_chars=chars).get("text", "")
        except BaseException:
            text = ""
        out.append({"name": name, "text": text})
    return out


def heuristic_scores(samples, preset_map):
    """Fraction of sampled documents whose text matches each preset's signature."""
    scores = {}
    for name, body in preset_map.items():
        detect = body.get("detect") or {}
        keywords = [k.lower() for k in detect.get("keywords", [])]
        regex = detect.get("regex")
        if not keywords and not regex:
            scores[name] = 0.0
            continue
        hits = 0
        for sample in samples:
            text = sample.get("text") or ""
            matched = any(k in text.lower() for k in keywords)
            if not matched and regex:
                matched = bool(re.search(regex, text))
            if matched:
                hits += 1
        scores[name] = round(hits / len(samples), 3) if samples else 0.0
    return scores


def _backend_opts(cfg, backend):
    if backend == "openai":
        return {"base_url": cfg.get("openai_base_url"), "api_key": cfg.get("openai_api_key")}
    return {"host": cfg.get("ollama_host") or "http://localhost:11434"}


def model_detect(samples, preset_map, cfg, scores=None):
    """Ask the detection node to pick a preset. Raises on an unusable reply."""
    from . import nodes, templates

    listing = [
        {"id": name, "label": body.get("label", name),
         "description": body.get("description", ""),
         "user_defined": name not in BUILTIN_PRESETS}
        for name, body in preset_map.items()
    ]
    backend = cfg.get("detection_input") or cfg.get("orchestrator_input") or "ollama"
    model = (cfg.get("detection_model") or cfg.get("orchestrator_model")
             or cfg.get("vision_model"))
    user = templates.build_detection_user(listing, samples, cfg.get("detection_prompt"), scores)
    raw = nodes.chat(
        backend, model,
        cfg.get("detection_system_prompt") or templates.DETECTION_SYSTEM,
        user, **_backend_opts(cfg, backend),
    )
    data = nodes.parse_json(raw)
    preset_id = str(data.get("preset") or "").strip()
    if preset_id not in preset_map:
        match = [name for name in preset_map if name.lower() == preset_id.lower()]
        if not match:
            raise ValueError("unknown preset %r" % preset_id)
        preset_id = match[0]
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {"preset": preset_id, "confidence": confidence, "reason": str(data.get("reason") or "")}


def detect(directory, cfg):
    """Detect the best preset for a folder. Returns a report dict."""
    directory = os.path.abspath(directory)
    preset_map = cfg.get("_presets") or all_presets(cfg.get("presets"))
    names = _pdfs(directory)
    samples = digest(directory, max_files=int(cfg.get("detect_sample", 15)))
    scores = heuristic_scores(samples, preset_map)
    method = (cfg.get("detect_method") or "model").strip().lower()
    report = {
        "directory": directory,
        "files": len(names),
        "sampled": len(samples),
        "method": method,
        "scores": scores,
    }

    if method in ("model", "auto") and samples:
        if method == "auto" and scores:
            top = max(scores.items(), key=lambda kv: kv[1])
            if top[1] >= 0.6:
                report.update(preset=top[0], confidence=top[1],
                              reason="heuristic match", method="heuristic")
                return report
        try:
            choice = model_detect(samples, preset_map, cfg, scores)
            user_names = [n for n in preset_map if n not in BUILTIN_PRESETS]
            strong_user = sorted(
                ((n, scores.get(n, 0.0)) for n in user_names if scores.get(n, 0.0) >= 0.6),
                key=lambda kv: -kv[1],
            )
            if choice["preset"] not in user_names and strong_user:
                best, score = strong_user[0]
                report.update(
                    preset=best, confidence=score, method="heuristic+user",
                    reason="user preset %r matched %.0f%% of samples; model suggested %r"
                           % (best, score * 100, choice["preset"]))
                return report
            report.update(preset=choice["preset"], confidence=choice["confidence"],
                          reason=choice["reason"], method="model")
            return report
        except BaseException as exc:
            report["model_error"] = "%s: %s" % (type(exc).__name__, exc)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if ranked and ranked[0][1] > 0:
        report.update(preset=ranked[0][0], confidence=ranked[0][1],
                      reason="heuristic match", method="heuristic")
    else:
        report.update(preset="books", confidence=0.0,
                      reason="no signal; default", method="fallback")
    return report
