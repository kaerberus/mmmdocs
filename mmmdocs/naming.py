"""Turn a classification record into a clean filename.

Templates are simple Python format strings over the record fields, e.g.
"{author} - {title} ({year})". Missing fields are dropped intelligently, so an
empty author does not leave a dangling " - " and an empty year does not leave
"()". If there is no title, `render_filename` returns None and the caller keeps
the original name.
"""
from __future__ import annotations

import os
import re

DEFAULT_TEMPLATE = "{author} - {title} ({year})"

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_EMPTY_GROUP = re.compile(r"[\(\[\{]\s*[\)\]\}]")
_DOUBLE_SEP = re.compile(r"\s*[-–—|,]\s*(?=[-–—|,])")
_EDGE = re.compile(r"^[\s\-–—|,]+|[\s\-–—|,]+$")
_MULTI_DASH = re.compile(r"-{2,}")

# Windows forbids these as file stems, even with an extension.
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
_WINDOWS_RESERVED |= {"COM%d" % i for i in range(1, 10)}
_WINDOWS_RESERVED |= {"LPT%d" % i for i in range(1, 10)}


class _Missing(dict):
    def __missing__(self, key):
        return ""


def _values(record):
    """Every scalar field, plus topics joined, so custom prompt fields work."""
    values = {}
    for key, value in record.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            values[key] = str(value).strip() if value is not None else ""
    topics = record.get("topics") or []
    if isinstance(topics, list):
        topics = " / ".join(str(t).strip() for t in topics if str(t).strip())
    values["topics"] = str(topics).strip()
    return values


def render_filename(record, template=None, original_name="", max_length=150):
    """Return a sanitized filename (with extension), or None if it cannot be built.

    The template is the only requirement: any field (built-in or a custom field
    the model returned) can be used, and a name is produced even without a title.
    """
    template = template or DEFAULT_TEMPLATE
    try:
        name = template.format_map(_Missing(_values(record)))
    except (ValueError, KeyError, IndexError):
        return None

    name = _EMPTY_GROUP.sub("", name)
    name = _DOUBLE_SEP.sub("", name)
    name = _EDGE.sub("", name)
    name = _ILLEGAL.sub(" ", name)
    name = re.sub(r"\s+", " ", name)
    name = _MULTI_DASH.sub("-", name)
    name = name.strip(" .-_")
    if not name:
        return None
    if name.upper() in _WINDOWS_RESERVED:
        name = "_" + name

    ext = os.path.splitext(original_name)[1] or ".pdf"
    room = max_length - len(ext)
    if len(name) > room:
        name = name[:room].rstrip(" .-_")
    return name + ext
