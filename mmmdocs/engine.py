#!/usr/bin/env python3
"""mmmdocs.engine -- inspect, search, extract text from and rasterize PDF pages.

This is the single canonical PDF engine shared by:
  * the standalone `mmmdocs` CLI (mmmdocs/pipeline.py, mmmdocs/cli.py), and
  * the opencode `rpdf_*` tools (~/.config/opencode/tools/rpdf.py is a shim).

It is intentionally pure: it never calls a language model. It only turns PDFs
into JSON metadata and rasterized images.

Subcommands:
  info     <pdf>                                  -> JSON metadata / TOC / text-layer stats
  search   <pdf> <query> [--limit N]              -> JSON ranked pages + snippets (text layer)
  text     <pdf> [--pages SPEC] [--max-chars N]   -> JSON extracted text (text layer)
  manifest <dir> [--recursive] [--sample N]       -> compact per-file JSON for every PDF
  render   <pdf> --pages SPEC [options]           -> JSON manifest of rendered images
  cleanup  [--pdf PATH] [--cache-root DIR]        -> JSON list of removed dirs

Render profiles:
  low       100 DPI, long edge 1024 px, JPEG q80
  classify   96 DPI, long edge  768 px, JPEG q70   (fast title/cover disambiguation)
  default   150 DPI, long edge 1600 px, JPEG q85
  high      250 DPI, long edge 2200 px, PNG

Render is non-destructive: each call writes page-%04d-<profile>.<ext> and never
deletes pages rendered by earlier calls. `cleanup` removes the raster cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata

try:
    import fitz  # PyMuPDF
except Exception as exc:  # pragma: no cover
    print(json.dumps({"error": "PyMuPDF (fitz) is not available: %s" % exc}), file=sys.stderr)
    sys.exit(3)


DEFAULT_CACHE_ROOT = os.path.join(os.getcwd(), ".rpdf-cache")
DEFAULT_MAX_PAGES = 12
MIN_TEXT_CHARS = 20

PROFILES = {
    "low": {"dpi": 100, "max_edge": 1024},
    "classify": {"dpi": 96, "max_edge": 768},
    "default": {"dpi": 150, "max_edge": 1600},
    "high": {"dpi": 250, "max_edge": 2200},
}
DEFAULT_QUALITY = {"low": 80, "classify": 70, "default": 85, "high": 90}

# Auto-generated tables of contents are worse than none: they make a small model
# trust titles like "Página 12", "未标题-3" or "002.jpg". Detect and flag them.
_JUNK_TITLE = re.compile(
    r"^(p[áa]gina\s*\d+"
    r"|page\s*\d+"
    r"|未标题.*"
    r"|\d+\.(jpe?g|png|tif?f)$"
    r"|img[_-]?\d+$"
    r"|scan[_-]?\d+$"
    r"|\d+)$",
    re.IGNORECASE,
)


def die(message, code=2):
    print(json.dumps({"error": message}), file=sys.stderr)
    sys.exit(code)


def emit(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _nfc(value):
    return unicodedata.normalize("NFC", value)


def resolve_path(pdf_path):
    """Resolve a PDF path, tolerating Unicode normalization and small typos.

    macOS stores filenames as NFD while callers often pass NFC (or vice versa),
    and curly apostrophes/quotes differ from ASCII. Exact match wins; otherwise
    compare NFC-normalized names, then a case-insensitive prefix match.
    """
    if os.path.exists(pdf_path):
        return pdf_path
    parent, base = os.path.split(pdf_path)
    parent = parent or "."
    if not os.path.isdir(parent):
        return pdf_path
    target = _nfc(base).casefold()
    try:
        names = os.listdir(parent)
    except OSError:
        return pdf_path
    for name in names:
        if _nfc(name).casefold() == target:
            return os.path.join(parent, name)
    for name in names:
        if _nfc(name).casefold().startswith(target):
            return os.path.join(parent, name)
    return pdf_path


def slugify(pdf_path):
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", _nfc(base)).strip("-").lower()
    if len(base) > 60:
        base = base[:60]
    digest = hashlib.sha1(os.path.abspath(pdf_path).encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (base or "pdf", digest)


def name_key(pdf_path):
    """Heuristic duplicate key from a filename (ignores extension and noise tags).

    Deliberately preserves volume/issue numbers (El Croquis 71 != El Croquis 123)
    while collapsing re-download noise ("compressed", "0000 Medium", "copy").
    """
    base = _nfc(os.path.basename(pdf_path))
    base = os.path.splitext(base)[0].lower()
    base = base.replace("_", " ")
    base = re.sub(r"\b(compressed|medium|copy|final|ocr|scanned|z-lib(?:rary)?|libgen|anna'?s archive)\b", " ", base)
    base = re.sub(r"\b0+\b", " ", base)
    base = re.sub(r"[^a-z0-9]+", " ", base)
    return re.sub(r"\s+", " ", base).strip()


def open_doc(pdf_path):
    pdf_path = resolve_path(pdf_path)
    if not os.path.exists(pdf_path):
        die("PDF not found: %s" % pdf_path)
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        die("Could not open PDF: %s" % exc)
    if doc.is_encrypted:
        if not doc.authenticate(""):
            die("PDF is encrypted and requires a password: %s" % pdf_path)
    return doc


def page_text(page):
    try:
        return page.get_text() or ""
    except Exception:
        return ""


def parse_pages(spec, total):
    spec = (spec or "").strip().lower()
    if spec in ("all", "*", "1-" + str(total)):
        return list(range(1, total + 1))
    pages = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                a, b = int(a), int(b)
            except ValueError:
                die("Invalid page range: %r" % part)
            if a > b:
                a, b = b, a
            pages.extend(range(a, b + 1))
        else:
            try:
                pages.append(int(part))
            except ValueError:
                die("Invalid page number: %r" % part)
    seen = set()
    ordered = []
    for p in pages:
        if 1 <= p <= total and p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def clean_toc(doc):
    """Return (toc_entries, toc_source) with auto-generated junk flagged."""
    raw = doc.get_toc(simple=True) or []
    entries = [
        {"level": lvl, "title": _nfc(title or "").strip(), "page": page}
        for (lvl, title, page) in raw
        if title and title.strip()
    ]
    real = [e for e in entries if not _JUNK_TITLE.match(e["title"])]
    if real:
        source = "real"
    elif entries:
        source = "auto"
    else:
        source = "none"
    return entries[:300], real, source


def info_data(pdf):
    doc = open_doc(pdf)
    total = doc.page_count
    text_pages = 0
    size_counter = {}
    for i in range(total):
        page = doc[i]
        if len(page_text(page).strip()) >= MIN_TEXT_CHARS:
            text_pages += 1
        rect = page.rect
        key = (round(rect.width, 1), round(rect.height, 1))
        size_counter[key] = size_counter.get(key, 0) + 1

    sizes = [
        {"width_pt": w, "height_pt": h, "count": c}
        for (w, h), c in sorted(size_counter.items(), key=lambda kv: -kv[1])[:6]
    ]
    entries, real, source = clean_toc(doc)
    resolved = resolve_path(pdf)

    data = {
        "path": os.path.abspath(resolved),
        "pages": total,
        "size_bytes": os.path.getsize(resolved),
        "has_text_layer": text_pages > 0,
        "text_pages": text_pages,
        "text_ratio": round(text_pages / total, 3) if total else 0.0,
        "page_sizes_pt": sizes,
        "toc_entries": len(entries),
        "toc_real_entries": len(real),
        "toc_source": source,
        "name_key": name_key(resolved),
        "toc": entries[:300],
    }
    doc.close()
    return data


def cmd_info(args):
    emit(info_data(args.pdf))


def _sampled_pages(total, sample):
    if sample <= 0 or sample >= total:
        return list(range(total))
    step = max(1, total // sample)
    pages = list(range(0, total, step))[:sample]
    return pages


def _quick_stats(doc, sample):
    total = doc.page_count
    text_pages = 0
    size_counter = {}
    for i in _sampled_pages(total, sample):
        page = doc[i]
        if len(page_text(page).strip()) >= MIN_TEXT_CHARS:
            text_pages += 1
        rect = page.rect
        key = (round(rect.width, 1), round(rect.height, 1))
        size_counter[key] = size_counter.get(key, 0) + 1
    sampled = len(_sampled_pages(total, sample))
    ratio = round(text_pages / sampled, 3) if sampled else 0.0
    sizes = sorted(size_counter.items(), key=lambda kv: -kv[1])
    dominant = sizes[0][0] if sizes else (0.0, 0.0)
    return {
        "has_text_layer": text_pages > 0,
        "text_ratio": ratio,
        "sampled_pages": sampled,
        "dominant_pt": {"width_pt": dominant[0], "height_pt": dominant[1]},
    }


def manifest_data(directory, recursive=False, pattern="*.pdf", sample=12):
    root = resolve_path(directory) if os.path.exists(directory) else directory
    if not os.path.isdir(root):
        die("Not a directory: %s" % root)
    pattern = re.compile(pattern.replace(".", r"\.").replace("*", ".*") + "$")
    paths = []
    if recursive:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if pattern.search(name):
                    paths.append(os.path.join(dirpath, name))
    else:
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if os.path.isfile(full) and pattern.search(name):
                paths.append(full)
    paths.sort()

    files = []
    for path in paths:
        entry = {"path": os.path.abspath(path), "name_key": name_key(path)}
        try:
            entry["size_bytes"] = os.path.getsize(path)
            doc = fitz.open(path)
            entry.update(_quick_stats(doc, sample))
            entry["pages"] = doc.page_count
            entries, real, source = clean_toc(doc)
            entry["toc_entries"] = len(entries)
            entry["toc_real_entries"] = len(real)
            entry["toc_source"] = source
            doc.close()
        except Exception as exc:
            entry["error"] = str(exc)
        files.append(entry)

    # Group by heuristic name_key so callers can dedupe before dispatch.
    groups = {}
    for f in files:
        groups.setdefault(f["name_key"], []).append(f["path"])
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}

    return {
        "directory": os.path.abspath(root),
        "count": len(files),
        "duplicate_groups": duplicates,
        "files": files,
    }


def cmd_manifest(args):
    emit(manifest_data(args.dir, args.recursive, args.pattern, args.sample))


def cmd_search(args):
    doc = open_doc(args.pdf)
    terms = [t for t in re.split(r"\s+", (args.query or "").strip().lower()) if t]
    if not terms:
        die("Empty search query")

    text_pages = 0
    results = []
    for i in range(doc.page_count):
        raw = page_text(doc[i])
        if len(raw.strip()) >= MIN_TEXT_CHARS:
            text_pages += 1
        low = raw.lower()
        counts = [low.count(term) for term in terms]
        present = sum(1 for c in counts if c > 0)
        if present == 0:
            continue
        first = -1
        for term in terms:
            idx = low.find(term)
            if idx != -1 and (first == -1 or idx < first):
                first = idx
        snippet = re.sub(r"\s+", " ", raw[max(0, first - 120):first + 200]).strip()
        results.append({
            "page": i + 1,
            "matches": sum(counts),
            "terms_matched": present,
            "snippet": snippet,
        })

    results.sort(key=lambda r: (-r["terms_matched"], -r["matches"], r["page"]))
    emit({
        "path": os.path.abspath(resolve_path(args.pdf)),
        "query": args.query,
        "text_layer": text_pages > 0,
        "text_pages": text_pages,
        "result_count": len(results),
        "results": results[: args.limit],
    })
    doc.close()


def text_data(pdf, pages=None, head=5, max_chars=12000):
    doc = open_doc(pdf)
    total = doc.page_count
    if pages:
        selected = parse_pages(pages, total)
    else:
        selected = list(range(1, min(total, head) + 1))
    chunks = []
    chars = 0
    truncated = False
    used = []
    for p in selected:
        raw = page_text(doc[p - 1])
        if not raw.strip():
            continue
        if chars + len(raw) > max_chars:
            raw = raw[: max(0, max_chars - chars)]
            truncated = True
        chunks.append(raw)
        used.append(p)
        chars += len(raw)
        if truncated:
            break
    data = {
        "path": os.path.abspath(resolve_path(pdf)),
        "pages_in_pdf": total,
        "pages_extracted": used,
        "has_text_layer": bool(chunks),
        "chars": chars,
        "truncated": truncated,
        "text": "\n\n".join(chunks),
    }
    doc.close()
    return data


def cmd_text(args):
    emit(text_data(args.pdf, args.pages, args.head, args.max_chars))


def _profile_defaults(profile, fmt, quality):
    prof = PROFILES.get(profile, PROFILES["default"])
    dpi = prof["dpi"]
    max_edge = prof["max_edge"]
    if fmt is None:
        fmt = "png" if profile == "high" else "jpg"
    if quality is None:
        quality = DEFAULT_QUALITY.get(profile, 85)
    return dpi, max_edge, fmt, quality


def render_data(pdf, pages_spec, profile="default", dpi=None, max_edge=None,
                fmt=None, quality=None, max_pages=DEFAULT_MAX_PAGES, cache_root=None):
    dpi, max_edge, fmt, quality = _profile_defaults(profile, fmt, quality)
    dpi = dpi if dpi is None else dpi
    max_edge = max_edge if max_edge is None else max_edge

    doc = open_doc(pdf)
    total = doc.page_count
    pages = parse_pages(pages_spec, total)
    if not pages:
        die("No valid pages selected from spec %r (document has %d pages)" % (pages_spec, total))
    if len(pages) > max_pages:
        die(
            "Requested %d pages but max is %d per call. Split into several rpdf_render "
            "calls, or pass a larger max_pages explicitly." % (len(pages), max_pages)
        )

    root = os.path.abspath(cache_root) if cache_root else DEFAULT_CACHE_ROOT
    out_dir = os.path.join(root, slugify(resolve_path(pdf)))
    os.makedirs(out_dir, exist_ok=True)

    ext = "jpg" if fmt == "jpg" else "png"
    images = []
    for p in pages:
        page = doc[p - 1]
        rect = page.rect
        long_pt = max(rect.width, rect.height) or 1.0
        zoom = min(dpi / 72.0, max_edge / long_pt)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
        name = "page-%04d-%s.%s" % (p, profile, ext)
        path = os.path.join(out_dir, name)
        if ext == "jpg":
            with open(path, "wb") as handle:
                handle.write(pix.tobytes("jpeg", jpg_quality=quality))
        else:
            pix.save(path)
        images.append({
            "page": p,
            "path": path,
            "width": pix.width,
            "height": pix.height,
            "bytes": os.path.getsize(path),
        })

    cached_bytes = 0
    cached_pages = 0
    for name in os.listdir(out_dir):
        if name.startswith("page-"):
            cached_pages += 1
            cached_bytes += os.path.getsize(os.path.join(out_dir, name))

    data = {
        "pdf": os.path.abspath(resolve_path(pdf)),
        "pages_in_pdf": total,
        "rendered": len(images),
        "dpi": dpi,
        "max_edge": max_edge,
        "format": fmt,
        "quality": quality,
        "profile": profile,
        "out_dir": out_dir,
        "total_bytes": sum(i["bytes"] for i in images),
        "cache_pages": cached_pages,
        "cache_bytes": cached_bytes,
        "images": images,
    }
    doc.close()
    return data


def cmd_render(args):
    emit(render_data(
        args.pdf, args.pages, args.profile, args.dpi, args.max_edge,
        args.format, args.quality, args.max_pages, args.cache_root,
    ))


def cmd_cleanup(args):
    cache_root = os.path.abspath(args.cache_root) if args.cache_root else DEFAULT_CACHE_ROOT
    removed = []
    if args.pdf:
        target = os.path.join(cache_root, slugify(resolve_path(args.pdf)))
        if os.path.isdir(target):
            shutil.rmtree(target)
            removed.append(target)
    elif os.path.isdir(cache_root):
        for name in sorted(os.listdir(cache_root)):
            path = os.path.join(cache_root, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
                removed.append(path)
    emit({"cache_root": cache_root, "removed": removed})


def build_parser():
    parser = argparse.ArgumentParser(prog="rpdf", description="Rasterize and extract PDF pages for vision models")
    sub = parser.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("info", help="inspect a PDF")
    p_info.add_argument("pdf")
    p_info.set_defaults(func=cmd_info)

    p_search = sub.add_parser("search", help="search the text layer")
    p_search.add_argument("pdf")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=30)
    p_search.set_defaults(func=cmd_search)

    p_text = sub.add_parser("text", help="extract text from the text layer")
    p_text.add_argument("pdf")
    p_text.add_argument("--pages", default=None, help="e.g. 1-5 | all; default first --head pages")
    p_text.add_argument("--head", type=int, default=5, help="pages from the front when --pages is omitted")
    p_text.add_argument("--max-chars", type=int, default=12000, dest="max_chars")
    p_text.set_defaults(func=cmd_text)

    p_manifest = sub.add_parser("manifest", help="compact metadata for every PDF in a directory")
    p_manifest.add_argument("dir")
    p_manifest.add_argument("--recursive", action="store_true")
    p_manifest.add_argument("--pattern", default="*.pdf")
    p_manifest.add_argument("--sample", type=int, default=12, help="pages to sample for text ratio (0 = all)")
    p_manifest.set_defaults(func=cmd_manifest)

    p_render = sub.add_parser("render", help="render pages to images")
    p_render.add_argument("pdf")
    p_render.add_argument("--pages", required=True, help="e.g. 5 | 5-10 | 1,3,7-9 | all")
    p_render.add_argument("--profile", choices=sorted(PROFILES.keys()), default="default")
    p_render.add_argument("--dpi", type=int, default=None)
    p_render.add_argument("--max-edge", type=int, default=None, dest="max_edge")
    p_render.add_argument("--format", choices=["png", "jpg"], default=None)
    p_render.add_argument("--quality", type=int, default=None, help="JPEG quality (1-100)")
    p_render.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, dest="max_pages")
    p_render.add_argument("--cache-root", default=None, dest="cache_root")
    p_render.set_defaults(func=cmd_render)

    p_clean = sub.add_parser("cleanup", help="remove cached renders")
    p_clean.add_argument("--pdf", default=None)
    p_clean.add_argument("--cache-root", default=None, dest="cache_root")
    p_clean.set_defaults(func=cmd_cleanup)

    return parser


def main(argv):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
