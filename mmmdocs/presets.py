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
import shutil

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


FIELD_HINTS = {
    "title": "the printed title",
    "author": "author, editor, or architect",
    "year": "publication year",
    "date": "the document date",
    "doc_code": "the document code",
    "code": "the document code",
    "model": "the model or type designation",
    "models": "the applicable models",
    "serial": "the serial number",
    "part_number": "the part number",
    "manufacturer": "the manufacturer",
    "publisher": "the publisher",
    "issue": "the issue number",
    "volume": "the volume",
    "revision": "the revision",
    "rev": "the revision",
    "language": "the language",
    "topics": "subject tags",
    "family": "the document family or prefix",
}


def describe_field(name):
    name = str(name).strip()
    return FIELD_HINTS.get(name.lower(), "the " + name.replace("_", " "))


def make_id(name):
    """Slugify a preset name into an id."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(name)).strip("-").lower()
    return slug or "preset"


def build_from_fields(name, fields, separator=" - "):
    """Build a preset from a name and an ordered field list.

    The fields drive both the filename template and the JSON schema the vision
    model is asked to return, so there is a single source of truth.
    """
    fields = [str(f).strip() for f in fields if str(f).strip()]
    if not fields:
        return None
    template = separator.join("{%s}" % f for f in fields)
    schema = ['  "%s": "%s"' % (f, describe_field(f)) for f in fields]
    schema += ['  "confidence": 0.0', '  "needs_human": false']
    prompt = (
        "Read this document and return ONLY JSON with exactly these keys:\n{\n"
        + ",\n".join(schema)
        + "\n}\n\nBase every value on what is printed on the page. Use an empty "
        "string when a value is missing. If the document is unreadable or ambiguous, "
        "set \"needs_human\": true. Output JSON only."
    )
    return {
        "label": str(name).strip() or "Untitled preset",
        "description": "Documents described by fields: %s." % ", ".join(fields),
        "name_template": template,
        "mode": "rename",
        "vision_prompt": prompt,
    }


def sanitize_fields(fields, cap=6):
    """Slugify, de-duplicate and cap a proposed field list."""
    out = []
    for field in fields or []:
        name = re.sub(r"[^a-z0-9_]+", "_", str(field).strip().lower()).strip("_")[:24]
        if name and name not in out:
            out.append(name)
    return out[:cap]


# "Label:" line starts commonly seen on structured documents.
_LABEL_RE = re.compile(r"(?m)^[ \t]*([A-Z][A-Za-z0-9 /&.'#-]{2,29})\s*[:#]")
_LABEL_MAP = {
    "invoice number": "invoice_number", "invoice no": "invoice_number",
    "invoice #": "invoice_number", "invoice date": "date", "date": "date",
    "due date": "due_date", "total": "total", "total due": "total",
    "amount due": "total", "amount": "amount", "vendor": "vendor",
    "supplier": "vendor", "merchant": "vendor", "name": "name", "title": "title",
    "author": "author", "subject": "subject", "model": "model", "serial": "serial",
    "part number": "part_number", "drawing number": "drawing_number",
    "project": "project", "client": "client", "company": "company",
    "revision": "revision", "rev": "revision",
}


def _label_to_field(label):
    key = re.sub(r"[^a-z0-9 ]+", " ", label.lower()).strip()
    if key in _LABEL_MAP:
        return _LABEL_MAP[key]
    return re.sub(r"[^a-z0-9]+", "_", key).strip("_")[:24]


def mine_fields(samples, min_ratio=0.3, cap=6):
    """Candidate field names from frequent `Label:` lines across the samples."""
    if not samples:
        return []
    counts = {}
    for sample in samples:
        seen = set()
        for match in _LABEL_RE.finditer(sample.get("text") or ""):
            field = _label_to_field(match.group(1))
            if field and field not in seen:
                seen.add(field)
                counts[field] = counts.get(field, 0) + 1
    threshold = max(1, int(len(samples) * min_ratio))
    ranked = sorted(((f, c) for f, c in counts.items() if c >= threshold),
                    key=lambda kv: (-kv[1], kv[0]))
    return [f for f, _ in ranked[:cap]]


def fields_from_prompt(prompt):
    """JSON keys found in a prompt, in order (for showing what's available)."""
    seen = []
    for match in re.finditer(r'"([A-Za-z_][A-Za-z0-9_-]*)"\s*:', prompt or ""):
        key = match.group(1)
        if key not in seen:
            seen.append(key)
    return seen


def unknown_placeholders(template, fields):
    """Placeholders in a template that the field list does not define."""
    known = {str(f).strip() for f in fields}
    used = re.findall(r"\{([^{}]+)\}", template or "")
    return [u for u in used if u not in known]


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


# --------------------------------------------------------------------------- #
# fast embedding scan (pure Python; no extra dependencies)
# --------------------------------------------------------------------------- #

def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def _unit_mean(vectors):
    dim = len(vectors[0])
    out = [0.0] * dim
    for vec in vectors:
        for i in range(dim):
            out[i] += vec[i]
    norm = sum(v * v for v in out) ** 0.5
    return [v / norm for v in out] if norm else out


def _assign_all(vectors, clusters):
    assign = []
    for vec in vectors:
        best_i, best_s = 0, -1.0
        for i, cluster in enumerate(clusters):
            score = _cosine(vec, cluster["centroid"])
            if score > best_s:
                best_i, best_s = i, score
        assign.append(best_i)
    return assign


def _refine_clusters(vectors, clusters, merge_threshold=0.75):
    """Merge near-identical centroids and absorb singleton clusters, so short or
    varied first-page text doesn't shatter into many tiny groups."""
    changed = True
    while changed and len(clusters) > 1:
        changed = False
        best_i = best_j = -1
        best_s = -1.0
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                score = _cosine(clusters[i]["centroid"], clusters[j]["centroid"])
                if score > best_s:
                    best_i, best_j, best_s = i, j, score
        if best_s >= merge_threshold:
            clusters[best_i]["members"] += clusters[best_j]["members"]
            clusters[best_i]["centroid"] = _unit_mean(
                [vectors[m] for m in clusters[best_i]["members"]])
            del clusters[best_j]
            changed = True
    if len(clusters) > 1:
        multi = [c for c in clusters if len(c["members"]) > 1]
        singles = [c for c in clusters if len(c["members"]) == 1]
        for single in singles:
            vec = vectors[single["members"][0]]
            best, best_s = None, -1.0
            for cluster in multi:
                score = _cosine(vec, cluster["centroid"])
                if score > best_s:
                    best, best_s = cluster, score
            if best is not None:
                best["members"].append(single["members"][0])
                best["centroid"] = _unit_mean([vectors[m] for m in best["members"]])
            else:
                multi.append(single)
        clusters = multi
    return clusters


def _leader_cluster(vectors, threshold=0.80, max_clusters=24):
    """Online leader clustering: join the nearest centroid above `threshold`,
    otherwise start a new cluster. Deterministic in input order."""
    clusters = []  # {"centroid": vec, "members": [idx into vectors]}
    assign = []
    for idx, vec in enumerate(vectors):
        best_i, best_s = -1, -1.0
        for i, cluster in enumerate(clusters):
            score = _cosine(vec, cluster["centroid"])
            if score > best_s:
                best_i, best_s = i, score
        if best_i >= 0 and best_s >= threshold:
            cluster = clusters[best_i]
            cluster["members"].append(idx)
            cluster["centroid"] = _unit_mean([vectors[m] for m in cluster["members"]])
            assign.append(best_i)
        elif len(clusters) < max_clusters:
            clusters.append({"centroid": list(vec), "members": [idx]})
            assign.append(len(clusters) - 1)
        else:
            clusters[best_i]["members"].append(idx)
            assign.append(best_i)
    return clusters, assign


def _preset_proto(body, name):
    keywords = " ".join((body.get("detect") or {}).get("keywords", []) or [])
    return "%s. %s. template %s. %s" % (
        body.get("label", name), body.get("description", ""),
        body.get("name_template", ""), keywords)


def _embed_opts(cfg, backend):
    if backend == "openai":
        return {"base_url": cfg.get("openai_base_url") or "https://api.deepseek.com/v1",
                "api_key": cfg.get("openai_api_key")}
    return {"host": cfg.get("ollama_host") or "http://localhost:11434"}


def _describe_scans(directory, names, texts, cfg):
    """For no-text files, optionally caption the cover with a vision model so they
    can still be embedded and clustered. Opt-in via `detect_vision`."""
    from . import nodes

    backend = cfg.get("vision_input") or "ollama"
    model = cfg.get("scan_classifier") or cfg.get("vision_model")
    opts = _embed_opts(cfg, backend)
    for i, name in enumerate(names):
        if texts[i].strip():
            continue
        try:
            rendered = engine.render_data(os.path.join(directory, name), "1",
                                          profile="classify", max_pages=1,
                                          cache_root=cfg.get("cache_root"))
            images = [im["path"] for im in rendered["images"]]
            if not images:
                continue
            text = nodes.chat(backend, model, "You briefly caption documents.",
                              "In one line, state the document type and its heading or title.",
                              images, **opts)
            texts[i] = (text or "").strip()
            shutil.rmtree(rendered["out_dir"], ignore_errors=True)
        except BaseException:
            continue


def embed_scan(directory, cfg):
    """Cheap qualitative scan: embed first-page text, cluster, and match presets.

    Returns a report with clusters, a `mixed` flag, `groups`, duplicates,
    outliers, and a cluster-stratified `sample` for the generative detector.
    Raises if the embedding model is unavailable (callers fall back).
    """
    from collections import Counter
    from . import nodes

    directory = os.path.abspath(directory)
    preset_map = cfg.get("_presets") or all_presets(cfg.get("presets"))
    names = _pdfs(directory)
    limit = int(cfg.get("scan_max") or 0)
    if limit and len(names) > limit:
        step = len(names) / float(limit)
        names = [names[int(i * step)] for i in range(limit)]

    texts = []
    for name in names:
        try:
            texts.append(engine.text_data(os.path.join(directory, name), pages="1",
                                          max_chars=800).get("text", ""))
        except BaseException:
            texts.append("")
    if cfg.get("detect_vision"):
        _describe_scans(directory, names, texts, cfg)

    no_text = sum(1 for t in texts if not t.strip())

    backend = cfg.get("embed_input") or "ollama"
    model = cfg.get("embed_model") or "embeddinggemma"
    batch = max(1, int(cfg.get("embed_batch") or 64))
    opts = _embed_opts(cfg, backend)

    vectors = [None] * len(names)
    todo = [i for i, t in enumerate(texts) if t.strip()]
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        for i, vec in zip(chunk, nodes.embed(backend, model, [texts[i] for i in chunk], **opts)):
            vectors[i] = vec

    valid = [(i, vectors[i]) for i in range(len(names)) if vectors[i] is not None]
    report = {
        "directory": directory,
        "files": len(names),
        "embedded": len(valid),
        "no_text": no_text,
        "embed_model": model,
        "clusters": [],
        "mixed": False,
        "groups": [],
        "duplicates": [],
        "outliers": [],
        "sample": [],
    }
    if not valid:
        return report

    vecs = [v for _, v in valid]
    clusters, _assign = _leader_cluster(
        vecs, float(cfg.get("cluster_threshold", 0.70)), int(cfg.get("max_clusters", 24)))
    clusters = _refine_clusters(vecs, clusters, float(cfg.get("merge_threshold", 0.75)))
    assign = _assign_all(vecs, clusters)

    proto_names = list(preset_map)
    proto_vecs = nodes.embed(backend, model,
                             [_preset_proto(preset_map[n], n) for n in proto_names], **opts)
    file_preset, file_score = [], []
    for vec in vecs:
        best_j, best_s = -1, -1.0
        for j, pv in enumerate(proto_vecs):
            score = _cosine(vec, pv)
            if score > best_s:
                best_j, best_s = j, score
        file_preset.append(proto_names[best_j] if best_j >= 0 else None)
        file_score.append(best_s)

    cluster_reports = []
    for ci, cluster in enumerate(clusters):
        members = cluster["members"]
        preset_counts = Counter(file_preset[m] for m in members if file_preset[m])
        dominant, conf = (preset_counts.most_common(1)[0] if preset_counts else (None, 0.0))
        cohesion = sum(_cosine(vecs[m], cluster["centroid"]) for m in members) / len(members)
        cluster_reports.append({
            "id": ci,
            "size": len(members),
            "dominant_preset": dominant,
            "preset_confidence": round(conf / len(members), 3),
            "cohesion": round(cohesion, 3),
            "examples": [names[valid[m][0]] for m in members[:5]],
            "members": [valid[m][0] for m in members],
        })
    cluster_reports.sort(key=lambda c: -c["size"])
    report["clusters"] = cluster_reports

    big = [c for c in cluster_reports if c["size"] >= max(2, int(0.1 * len(vecs)))]
    # Multiple clusters alone don't mean "mixed": a single document type often
    # varies enough to split. It's mixed only when substantial groups map to
    # different presets (or some match none).
    types = {(c["dominant_preset"] or "?") for c in big}
    report["mixed"] = len(big) >= 2 and len(types) > 1

    for group_no, cluster in enumerate(cluster_reports, 1):
        fields = mine_fields([{"name": names[i], "text": texts[i]}
                              for i in cluster["members"][:12]])
        body = preset_map.get(cluster["dominant_preset"]) or {}
        report["groups"].append({
            "label": body.get("label") or "Group %d" % group_no,
            "preset": cluster["dominant_preset"],
            "count": cluster["size"],
            "fields": fields,
            "files": [names[i] for i in cluster["members"]],
        })

    dup_threshold = float(cfg.get("dup_threshold", 0.95))
    for cluster in clusters:
        members = cluster["members"]
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                if _cosine(vecs[members[a]], vecs[members[b]]) >= dup_threshold:
                    report["duplicates"].append([names[valid[members[a]][0]],
                                                 names[valid[members[b]][0]]])
        if len(report["duplicates"]) >= 200:
            break

    outlier_threshold = float(cfg.get("outlier_threshold", 0.55))
    for k, (i, vec) in enumerate(valid):
        if _cosine(vec, clusters[assign[k]]["centroid"]) < outlier_threshold:
            report["outliers"].append(names[i])

    want = max(1, int(cfg.get("detect_sample", 15)))
    per = max(1, want // max(1, len(clusters)))
    for cluster in clusters:
        for m in cluster["members"][:per]:
            i = valid[m][0]
            report["sample"].append({"name": names[i], "text": texts[i]})
    return report


def groups_from_scan(scan, cfg):
    """Turn a scan report into runnable groups, each with its own settings.

    Groups with a matching preset use it; others get a schema built from mined
    fields (injected into cfg['_presets'] as an ephemeral preset).
    """
    preset_map = cfg.get("_presets") or all_presets(cfg.get("presets"))
    out = []
    for group in scan.get("groups", []):
        pid = group.get("preset")
        settings = {}
        if pid and pid in preset_map:
            settings = preset_defaults(preset_map, pid)
        elif group.get("fields"):
            label = group.get("label") or "Group"
            draft = build_from_fields(label, group["fields"])
            pid = make_id(label)
            preset_map[pid] = draft
            settings = {"name_template": draft["name_template"],
                        "vision_prompt": draft["vision_prompt"],
                        "vision_system_prompt": draft.get("vision_system_prompt"),
                        "mode": draft.get("mode", "rename")}
        out.append({"label": group.get("label"), "preset": pid,
                    "files": group.get("files", []), "settings": settings})
    cfg["_presets"] = preset_map
    return out


def save_scan_groups(root, report):
    """Write the scan's groups (file lists) next to the folder for later use."""
    payload = {
        "directory": report.get("directory"),
        "mixed": report.get("mixed"),
        "groups": [{"label": g.get("label"), "preset": g.get("preset"),
                    "count": g.get("count"), "fields": g.get("fields"),
                    "files": g.get("files")} for g in report.get("groups", [])],
        "duplicates": report.get("duplicates", []),
        "outliers": report.get("outliers", []),
    }
    path = os.path.join(os.path.abspath(root), "scan-groups.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    return path


def model_detect(samples, preset_map, cfg, scores=None, mined=None):
    """Ask the detection node to pick a preset or propose a schema.

    Raises only when the reply is unusable (no valid preset and no proposed
    fields), so callers can fall back to heuristics.
    """
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
    user = templates.build_detection_user(
        listing, samples, cfg.get("detection_prompt"), scores, mined)
    raw = nodes.chat(
        backend, model,
        cfg.get("detection_system_prompt") or templates.DETECTION_SYSTEM,
        user, **_backend_opts(cfg, backend),
    )
    data = nodes.parse_json(raw)

    preset_id = str(data.get("preset") or "").strip()
    if preset_id not in preset_map:
        match = [name for name in preset_map if name.lower() == preset_id.lower()]
        preset_id = match[0] if match else ""

    proposed = data.get("proposed") or {}
    fields = sanitize_fields(proposed.get("fields"))
    label = str(proposed.get("label") or "").strip()
    proposal = {"label": label, "fields": fields} if fields else None

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(data.get("reason") or "")
    matched = bool(data.get("match", True)) and bool(preset_id) and not proposal

    if not matched and not proposal:
        raise ValueError("no valid preset and no proposed fields")
    return {"preset": preset_id or None, "confidence": confidence, "reason": reason,
            "match": matched, "proposed": proposal}


def detect(directory, cfg):
    """Detect the best preset for a folder, or propose a schema when none fits.

    Report keys: preset, match, confidence, method, reason, scores, and (when
    match is False) a `proposed` {label, fields} draft.
    """
    directory = os.path.abspath(directory)
    preset_map = cfg.get("_presets") or all_presets(cfg.get("presets"))
    names = _pdfs(directory)
    scan_method = (cfg.get("scan_method") or "auto").strip().lower()
    scan = cfg.get("_scan_cache")
    if scan is None and scan_method in ("auto", "embedding"):
        try:
            scan = embed_scan(directory, cfg)
            cfg["_scan_cache"] = scan
        except BaseException:
            scan = None
    if scan and scan.get("sample"):
        samples = scan["sample"]
    else:
        samples = digest(directory, max_files=int(cfg.get("detect_sample", 15)))
    scores = heuristic_scores(samples, preset_map)
    method = (cfg.get("detect_method") or "model").strip().lower()
    allow_fields = bool(cfg.get("detect_fields", True))
    min_match = float(cfg.get("detect_min_match", 0.5))
    mined = mine_fields(samples) if allow_fields else []
    report = {
        "directory": directory,
        "files": len(names),
        "sampled": len(samples),
        "method": method,
        "scores": scores,
        "mined": mined,
    }
    if scan:
        report["scan"] = {
            "embedded": scan.get("embedded"),
            "no_text": scan.get("no_text"),
            "mixed": scan.get("mixed"),
            "clusters": len(scan.get("clusters", [])),
            "groups": [{"label": g.get("label"), "preset": g.get("preset"),
                        "count": g.get("count")} for g in scan.get("groups", [])],
        }
    chosen, confidence, reason = "books", 0.0, "no signal; default"
    matched = False
    model_proposal = None

    if method in ("model", "auto") and samples:
        if method == "auto" and scores:
            top = max(scores.items(), key=lambda kv: kv[1])
            if top[1] >= 0.6:
                report.update(preset=top[0], match=True, confidence=top[1],
                              reason="heuristic match", method="heuristic")
                return report
        try:
            choice = model_detect(samples, preset_map, cfg, scores, mined)
            user_names = [n for n in preset_map if n not in BUILTIN_PRESETS]
            strong_user = sorted(
                ((n, scores.get(n, 0.0)) for n in user_names if scores.get(n, 0.0) >= 0.6),
                key=lambda kv: -kv[1],
            )
            if choice["preset"] and choice["preset"] not in user_names and strong_user and choice["match"]:
                best, score = strong_user[0]
                report.update(preset=best, match=True, confidence=score, method="heuristic+user",
                              reason="user preset %r matched %.0f%% of samples; model suggested %r"
                                     % (best, score * 100, choice["preset"]))
                return report
            chosen = choice["preset"] or "books"
            confidence = choice["confidence"]
            reason = choice["reason"]
            model_proposal = choice["proposed"]
            matched = choice["match"]
        except BaseException as exc:
            report["model_error"] = "%s: %s" % (type(exc).__name__, exc)

    # Fall back to heuristics when the model wasn't used or gave nothing usable.
    if not matched and model_proposal is None:
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if ranked and ranked[0][1] > 0:
            chosen = ranked[0][0]
            confidence = ranked[0][1]
            reason = "heuristic match"
            method = "heuristic"
            matched = True

    # A generic built-in with no heuristic support is not a real match.
    if matched and chosen in BUILTIN_PRESETS and scores.get(chosen, 0) == 0 and confidence < min_match:
        matched = False

    report.update(preset=chosen, match=matched, confidence=confidence,
                  reason=reason, method=method)
    if not matched and allow_fields:
        fields = sanitize_fields((model_proposal or {}).get("fields", []) + mined)
        if fields:
            label = (model_proposal or {}).get("label") or "Schema for %s" % os.path.basename(directory)
            report["proposed"] = {"label": label, "fields": fields}
    return report
