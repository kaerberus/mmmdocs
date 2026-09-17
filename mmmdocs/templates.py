"""Prompt + output contract for the per-file vision classifier.

The prompt is deliberately short and literal. Small local models do better with
a fixed recipe and a fixed JSON shape than with open-ended instructions, and the
pipeline validates the shape regardless of what the model returns.
"""
from __future__ import annotations

import json

DOC_TYPES = [
    "monograph",
    "exhibition-catalogue",
    "magazine-issue",
    "academic-book",
    "reference",
    "guide",
    "other",
]

CLASSIFY_SYSTEM = "You are a precise bibliographic classifier. You output only JSON."

CLASSIFY_INSTRUCTION = """Identify the ONE PDF file below.

You are shown: (a) the original file name, (b) compact PDF metadata, (c) a short text excerpt when the PDF has a text layer, and (d) rasterized image(s) of the first pages (cover / title page). Look at the images. The printed title on the page is the truth; the file name is only a hint.

Do NOT summarize or describe the book. Return ONLY one JSON object with exactly these keys:
{
  "title": "the real printed title (not the file name)",
  "author": "author / editor / architect, or \\"\\"",
  "year": "publication year if visible, else \\"\\"",
  "topics": ["2-5 short subject tags"],
  "doc_type": "one of: %s",
  "language": "english | japanese | portuguese | ... | multilingual",
  "script": "latin | cjk | mixed",
  "evidence_pages": [pages you actually looked at],
  "confidence": 0.0,
  "needs_human": false,
  "notes": "one short sentence or \\"\\""
}

Rules:
- Base "title" on what is PRINTED. If image and text excerpt disagree, trust the image.
- If the title is unreadable or ambiguous, set "needs_human": true.
- "confidence" is a number from 0.0 to 1.0.
- Output JSON only: no markdown fences, no explanation, no extra keys.
""" % " | ".join(DOC_TYPES)

REQUIRED_KEYS = [
    "title", "author", "year", "topics", "doc_type",
    "language", "script", "evidence_pages", "confidence", "needs_human", "notes",
]

FOLDERS_SYSTEM = "You organize a book library into a small folder taxonomy. You output only JSON."

FOLDERS_INSTRUCTION = """Below is a catalog of PDF records. Propose a SMALL folder taxonomy (at most 10 folders) that groups them sensibly. Prefer grouping by doc_type first, then by dominant subject.

Return ONLY JSON:
{
  "folders": {"<record title>": "<folder path, e.g. Monographs/Modern Japan>", ...},
  "reason": "one short sentence"
}

Every input title must appear exactly once as a key. Use forward-slash paths. Output JSON only.
"""


DETECTION_SYSTEM = "You route a folder of documents to the best matching preset. You output only JSON."

DETECTION_INSTRUCTION = """You are given a sample of documents from ONE folder. Choose the best-matching preset from the list below, or propose a new schema when none fits.

Consider the file names and the sampled first-page text. Return ONLY JSON:
{
  "preset": "<id from the list, or \\"\\">",
  "confidence": 0.0,
  "reason": "one short sentence",
  "match": true,
  "proposed": {"label": "short human name", "fields": ["field_one", "field_two"]}
}

Rules:
- If a preset fits, set "preset" to its id and "match": true.
- If none genuinely fits, set "match": false, "preset": "", and fill "proposed" with
  a short "label" and an ordered list of 2-6 short snake_case "fields" you would put
  in a file name, most identifying first (e.g. ["vendor","invoice_number","date","total"]).
- A preset marked "user_defined": true is purpose-built for this folder; prefer it when it fits.
- "heuristic_score" is the fraction of sampled documents that matched that preset's signature. A high score is strong evidence.
- Use "books" only if nothing else fits.
- Output JSON only."""


def build_detection_user(preset_list, digest_samples, instruction=None, scores=None, mined=None):
    if scores:
        for entry in preset_list:
            entry["heuristic_score"] = scores.get(entry["id"], 0.0)
    listing = json.dumps(preset_list, ensure_ascii=False, indent=2)
    samples = "\n\n".join(
        "### %s\n%s" % (s.get("name", ""), (s.get("text") or "(no text)")[:400])
        for s in digest_samples
    )
    hint = ""
    if mined:
        hint = "\n\n--- FREQUENT LABELS FOUND ---\n" + ", ".join(mined)
    return "%s\n\n--- PRESETS ---\n%s\n\n--- SAMPLES ---\n%s%s" % (
        instruction or DETECTION_INSTRUCTION, listing, samples, hint)


def build_classify_user(path, info, text_excerpt, image_pages, instruction=None):
    meta = {
        "file_name": path.rsplit("/", 1)[-1],
        "pages": info.get("pages"),
        "has_text_layer": info.get("has_text_layer"),
        "toc_source": info.get("toc_source"),
        "toc_titles": [
            e.get("title") for e in (info.get("toc") or [])[:8]
        ],
        "image_pages_shown": image_pages,
    }
    parts = [
        instruction or CLASSIFY_INSTRUCTION,
        "--- FILE ---",
        json.dumps(meta, ensure_ascii=False, indent=2),
    ]
    if text_excerpt:
        parts += ["--- TEXT EXCERPT (first pages) ---", text_excerpt[:4000]]
    else:
        parts += ["--- TEXT EXCERPT ---", "(none: no usable text layer)"]
    return "\n\n".join(parts)


def build_folders_user(records, instruction=None):
    slim = [
        {
            "title": r.get("title") or "",
            "author": r.get("author") or "",
            "topics": r.get("topics") or [],
            "doc_type": r.get("doc_type") or "other",
        }
        for r in records
        if r.get("title")
    ]
    return (instruction or FOLDERS_INSTRUCTION) + "\n\n--- CATALOG ---\n" + json.dumps(slim, ensure_ascii=False, indent=2)
