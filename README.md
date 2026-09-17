# mmmdocs

> Agentically coded with DeepSeek V4.1 Flash.

Catalog a folder of PDFs with a **local vision model**, then review a dry-run plan
before anything is moved. Built for mixed shelves (scanned books, magazines,
monographs, CJK scans) and small models: **one file is classified at a time**, so
no model ever has to hold more than a single cover in its head.

Runs entirely against [Ollama](https://ollama.com) by default, so there are **no
API keys**. The orchestrator node can optionally be pointed at any
OpenAI-compatible endpoint (e.g. DeepSeek) when you want the grouping step to be
smarter without paying for cloud vision.

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Commands](#commands)
- [Model per node](#model-per-node)
- [Configuration](#configuration)
- [Output files](#output-files)
- [Benchmarking models](#benchmarking-models)
- [opencode integration](#opencode-integration)
- [Troubleshooting](#troubleshooting)

---

## Install

No install step. Clone it, then run it — on startup `mmmdocs` checks every
dependency, offers to install whatever is missing, and drops you into a TUI.

```bash
git clone https://github.com/kaerberus/mmmdocs
cd mmmdocs
python3 -m mmmdocs
```

On first run it checks, in order:

1. **Python 3.9+**
2. **PyMuPDF** — if missing, offers to `pip install` it
3. **Ollama** — if it is installed but not running, offers to `ollama serve` it;
   if it is not installed at all, points you at the download
4. **The vision model** (default `gemma4:e4b`) — offers to `ollama pull` it

Only Python and PyMuPDF are required to start; without Ollama you can still use
the metadata commands and the TUI.

## Quick start

```bash
export PATH="$PWD/bin:$PATH"   # optional: call `mmmdocs` instead of python3 -m mmmdocs

mmmdocs            # no arguments -> interactive TUI
mmmdocs --help     # list the scriptable subcommands
```

On launch the TUI asks for a directory (tab-completes; **Enter keeps the last
one**, persisted in `config.json`), then shows only the four intents:

```
  0  YOLO — auto-detect, build, and organize everything
  1  Set up and run — guided
  u  Undo last apply
  s  Settings
  q  Quit
```

**0 = YOLO.** One keypress: it detects the document type, prints the exact
settings it will use, asks a single `y/n`, then classifies and **renames/moves
every file** (forced, including low-confidence ones). It finishes with a report
and an undo hint; `u` restores the last run.

**1 = guided.** If a plan already exists it asks *"An old plan already exists —
overwrite it with a fresh classification?"* (`N` reuses it: review & apply, no
model spend). Otherwise it walks: confirm the detected document type → confirm
the run → classify → review the `old -> new` list → `Apply now? [y/N]` (`f`
forces, including flagged files). Nothing is moved until you confirm.

Everything else (presets, prompts, mode, name template, models, detection,
directory, scan, catalog, rebuild, cache) lives under **`s` Settings**.

Prefer the shell? The same steps are scriptable:

```bash
mmmdocs manifest /path/to/books          # inspect, no model
mmmdocs run /path/to/books               # classify -> catalog.json + move-plan.json
mmmdocs apply /path/to/books             # dry-run review
mmmdocs apply /path/to/books --yes       # apply
```

Without `bin/` on `PATH`, run everything as `python3 -m mmmdocs ...` from inside
the repo (or set `PYTHONPATH=/path/to/mmmdocs`).

## How it works

```
mmmdocs run DIR
      │
      ├─ manifest        scan DIR with PyMuPDF: pages, text layer, TOC quality,
      │                  per-file name key, duplicate groups        (no model)
      │
      ├─ workers         ONE isolated worker process per PDF:
      │                    render pages 1-3 (profile: classify, ~35 KB each)
      │                    + optional text excerpt
      │                    -> ONE vision call -> fixed JSON record
      │
      ├─ taxonomy        one orchestrator call groups titles into folders
      │                  (only when mode moves files; deterministic fallback)
      │
      ├─ plan            apply mode + name template -> proposed destinations
      │
      └─ write           catalog.json + move-plan.json   (nothing is changed)

mmmdocs plan DIR            rebuild the plan from catalog.json (no model)
mmmdocs apply DIR [--yes]   apply, dry-run unless --yes
mmmdocs undo DIR [--yes]    reverse the last apply
```

Isolation is the point: a small model that only ever sees one cover cannot
confuse one book's title with another book's body text.

## Commands

Run `mmmdocs <command> --help` for the full flag list.

| Command | Purpose |
| --- | --- |
| *(none)* / `tui` | Launch the interactive menu. |
| `manifest DIR` | Compact metadata for every PDF in a directory. `--recursive`, `--sample N`. |
| `run DIR` | Classify all files, write `catalog.json` + `move-plan.json`. `--preset`, `--mode`, `--name-template`, `--only NAME` (repeatable), `--limit N`, `--keep-duplicates`. |
| `plan DIR` | Rebuild `move-plan.json` from an existing `catalog.json` — **no model calls**. `--preset`, `--mode`, `--name-template`. |
| `presets` | List built-in and user-defined presets. |
| `detect DIR` | Suggest the best preset for a folder (model + heuristics). `--detect-method model\|auto\|heuristic`. |
| `bench DIR` | Compare vision models. `--vision-models a,b,c`, `--sample N`. |
| `apply DIR` | Apply `move-plan.json` (rename and/or move). Dry-run unless `--yes`; `--force`, `--min-confidence X`. |
| `undo DIR` | Reverse the last apply. Dry-run unless `--yes`. |
| `classify-one FILE` | Classify a single PDF, print its JSON record. Useful for debugging. |
| `info PDF` | Page count, sizes, text-layer stats, TOC (with `toc_source`). |
| `text PDF` | Extract the text layer. `--pages SPEC`, `--head 5`, `--max-chars N`. |
| `render PDF --pages SPEC` | Rasterize pages. `--profile low\|classify\|default\|high`. |
| `cleanup` | Delete cached renders. `--pdf FILE` for one file, else the whole cache. |

Page specs are 1-based: `5`, `5-10`, `1,3,7-9`, or `all`.

## Modes and renaming

`run` and `plan` take `--mode` (config key `mode`, default `rename`):

| mode | folder | filename |
| --- | --- | --- |
| `catalog` | unchanged | unchanged |
| `move` | taxonomy folders | unchanged |
| `rename` | current folder | template name |
| `rename-move` | taxonomy folders | template name |

Filenames come from `--name-template` (config `name_template`, default
`{author} - {title} ({year})`). Fields: `{title}`, `{author}`, `{year}`,
`{doc_type}`, `{language}`, `{topics}`. Missing fields are dropped cleanly — an
empty author gives `Title (Year)`, not ` - Title ()` — and illegal characters are
sanitized. Records with no title keep their original name and are flagged for
review.

```bash
# rename in place using the existing classification (no model, instant)
mmmdocs plan "/path/to/books" --mode rename

# move + rename with a custom scheme
mmmdocs plan "/path/to/books" --mode rename-move --name-template "{title} - {author} ({year})"

# preview, apply, and (if needed) undo
mmmdocs apply "/path/to/books"
mmmdocs apply "/path/to/books" --yes
mmmdocs undo  "/path/to/books" --yes
```

`plan` regroups with a deterministic scheme and never calls a model. `run`
additionally asks the orchestrator model for folder names when the mode moves files.

### Filename safety

Generated names are sanitized deterministically (no reliance on prompt wording):
cross-platform-illegal characters `< > : " / \ | ? *` and control characters are
removed, empty `()`/`[]` and dangling separators collapse, Windows reserved stems
(`CON`, `PRN`, `AUX`, `NUL`, `COM1–9`, `LPT1–9`) get a leading `_`, names are
stripped of trailing spaces/dots, and capped at 150 characters (extension kept).

### Render profiles

| Profile | DPI | Long edge | Format | Typical size | Use for |
| --- | --- | --- | --- | --- | --- |
| `classify` | 96 | 768 px | JPEG q70 | ~35 KB | covers / title pages (default in `run`) |
| `low` | 100 | 1024 px | JPEG q80 | ~100–180 KB | quick overviews, photos |
| `default` | 150 | 1600 px | JPEG q85 | ~150–500 KB | normal reading |
| `high` | 250 | 2200 px | PNG | ~2 MB | small type and CJK body text |

## Custom prompts & other document types

Each node has an editable instruction and system message, because small models are
sensitive to wording. In the TUI: **Settings → 2 Prompts**. For each prompt it shows
`old prompt:`, then `new prompt:` where **empty resets to the built-in default**,
a typed line replaces it, and **`edit`** opens `$EDITOR` for multi-line editing.
**Settings → w** saves to `<repo>/config.json`.

The same values are available from the CLI (empty string resets):

```bash
mmmdocs run "/path/to/docs" \
  --vision-prompt "Read the datasheet and return JSON with keys: model, serial, voltage, needs_human." \
  --name-template "{model} - {serial}"
```

The per-file classifier appends the file name, PDF metadata, a text excerpt and
the rendered pages after your instruction, so a custom prompt only replaces the
instructions. **You own the output schema**: every extra JSON field the model
returns is preserved and can be referenced in `name_template` (a title is not
required). Unreadable/ambiguous items should set `"needs_human": true`.

This makes non-book documents work, e.g. a spec-sheet prompt plus
`--name-template "{model} - {serial}"`. The taxonomy prompt must still return
`{"folders": {...}, "reason": "..."}` to group files, and grouping needs a
non-empty `title` (rename mode does not).

## Presets & detection

A **preset** bundles the settings that differ between document types — the vision
prompt, the filename template, and the mode — so you pick "what kind of folder is
this" instead of knowing template syntax. Built-ins: `books`, `spec-sheet`,
`magazines`. Add your own under `presets` in `config.json`; they appear in the
picker and in detection.

```json
"presets": {
  "wis": {
    "label": "Mercedes WIS service docs",
    "description": "DaimlerChrysler WIS print-outs; code + heading per page.",
    "name_template": "{doc_code} - {title}",
    "mode": "rename",
    "vision_prompt": "You are given ONE Mercedes-Benz WIS print-out ...",
    "detect": {"regex": "[A-Z]{2}\\d{2}\\.\\d{2}-[A-Z]-\\d{3,4}", "keywords": ["daimlerchrysler"]}
  }
}
```

**Detection** samples the text layer only (no rendering) and suggests a preset:

- Heuristics score every preset's `detect` signature (`regex`/`keywords`) against
  the sample — always computed.
- The **detection node** (default: the orchestrator) is then asked to choose, with
  a configurable prompt that includes the heuristic scores and flags user-defined
  presets. A strong match on a user-defined preset wins over a generic model pick.
- `detect_method`: `model` (default), `auto` (heuristics first, model only when
  unclear), or `heuristic` (no model).

```bash
mmmdocs presets                  # list built-ins + user presets
mmmdocs detect "/path/to/docs"   # -> {preset: wis, confidence: .., scores: {...}}
mmmdocs run "/path/to/docs" --preset auto     # detect, then apply
mmmdocs run "/path/to/docs" --preset wis      # force one
```

In the TUI, choosing a folder runs detection and offers to switch
(`Detected: Mercedes WIS service docs (model, 0.98) — switch preset? [Y/n]`), and
**Settings → 1 Document type / preset** is a picker showing each preset's label and resulting
template. Because presets are just defaults, editing the prompt or template
afterwards marks the preset `(customized)` and your edit wins.

The detection prompt is editable too, like the other node prompts
(`detection_prompt` / `detection_system_prompt` in **Settings → 2 Prompts**).

### Create a preset in the TUI

**Settings → 1 → n**, then two inputs:

```
Preset name: WIS service docs
Fields (comma-separated, in order): doc_code, title, models, date
```

The field list is the single source of truth: it becomes the filename schema
(`{doc_code} - {title} - {models} - {date}`) **and** the JSON schema the vision
model is asked to return (`confidence` and `needs_human` are added automatically).
Then:

```
  e edit prompt   f edit filename   d edit description   t test   s save   c cancel
```

- `t` classifies one real file with the **unsaved** draft and prints the extracted
  fields and the resulting filename — iterate until it's right (skips if Ollama is
  offline).
- `s` saves it to `config.json`, activates it, and it's immediately available to
  `detect`, `run`, and YOLO.
- `e`/`x` in the preset menu edit or delete existing **user** presets (built-ins
  can be shadowed but not changed). Advanced knobs — mode, detection
  `regex`/`keywords`, system prompt — stay editable via `e`.

## Model per node

Two independent nodes:

- **vision node** — reads the cover/title page and produces the record.
- **orchestrator node** — groups the returned records into folders.

They can be different models, locally or in the cloud:

```bash
# small local vision + cloud orchestrator
mmmdocs run /path/to/books \
  --vision-model gemma4:e4b \
  --orchestrator-input openai \
  --orchestrator-model deepseek-chat \
  --openai-api-key "$DEEPSEEK_API_KEY"
```

Relevant flags: `--vision-model`, `--vision-input ollama|openai`,
`--orchestrator-model`, `--orchestrator-input`, `--ollama-host`,
`--openai-base-url`, `--openai-api-key`, `--workers`, `--profile`.

> The vision node must be a **vision-capable** model (the `gemma4:*` family is).
> Text-only models such as `qwen3.8:27b` should only be used as the orchestrator.

Any flag can also live in a config file:

```bash
cp config.example.json my-config.json
mmmdocs run /path/to/books --config my-config.json
```

## Configuration

`config.example.json` (all keys optional; CLI flags override the file):

| Key | Default | Meaning |
| --- | --- | --- |
| `vision_input` | `ollama` | `ollama` or `openai` |
| `vision_model` | `gemma4:e4b` | model that reads covers |
| `orchestrator_input` | `ollama` | `ollama` or `openai` |
| `orchestrator_model` | *(null)* | falls back to `vision_model` |
| `ollama_host` | `http://localhost:11434` | Ollama server |
| `openai_base_url` | `https://api.deepseek.com/v1` | any OpenAI-compatible base |
| `openai_api_key` | *(null)* | required for the `openai` backend |
| `workers` | `4` | parallel per-file processes |
| `profile` | `classify` | render profile used during `run` |
| `max_pages_vision` | `3` | cover/title pages sent per file |
| `sample_text_chars` | `4000` | text excerpt kept per file |
| `manifest_sample` | `12` | pages sampled for the text-layer ratio |
| `cache_root` | *(null)* | raster cache; defaults to `DIR/.rpdf-cache` |
| `min_confidence` | `0.5` | `apply` refuses records below this |
| `mode` | `rename` | `catalog`, `move`, `rename`, or `rename-move` |
| `name_template` | `{author} - {title} ({year})` | filename template for renames |
| `vision_prompt` | *(null)* | override the per-file classifier instruction |
| `vision_system_prompt` | *(null)* | override the classifier system message |
| `orchestrator_prompt` | *(null)* | override the taxonomy instruction |
| `orchestrator_system_prompt` | *(null)* | override the taxonomy system message |
| `detection_prompt` | *(null)* | override the folder-classification instruction |
| `detection_system_prompt` | *(null)* | override the detection system message |
| `detection_input` / `detection_model` | *(null)* | node used for detection (default: orchestrator) |
| `detect_method` | `model` | `model`, `auto`, or `heuristic` |
| `detect_sample` | `15` | files sampled during detection |
| `preset` | `books` | active preset, or `auto`, or `custom` |
| `presets` | `{}` | user-defined presets (see `config.example.json`) |
| `last_directory` | *(null)* | directory the TUI remembers between launches |

Settings live in **`<repo>/config.json`** (not `$HOME`), so they travel with the
folder. In the TUI, **Settings → 2 Prompts** edits prompts and **Settings → w** saves them; the file
is created with only the values you changed.

## Output files

Written into the target directory.

**`catalog.json`** — one record per file:

```json
{
  "directory": "/books",
  "vision_model": "gemma4:e4b",
  "count": 1,
  "skipped_duplicates": [],
  "records": [
    {
      "file": "/books/A History of Japan.pdf",
      "title": "A History of Japan: From Stone Age to Superpower",
      "author": "Kenneth G. Henshall",
      "year": "2004",
      "topics": ["Japanese history", "East Asia"],
      "doc_type": "monograph",
      "language": "english",
      "script": "latin",
      "evidence_pages": [1, 2, 3],
      "confidence": 1.0,
      "needs_human": false,
      "notes": "",
      "classified_by": "gemma4:e4b",
      "images_used": 3,
      "bytes_used": 49792
    }
  ]
}
```

**`move-plan.json`** — a proposed destination (folder and/or filename) for each
file, with `original_name`, `proposed_name`, `renamed`, `confidence` and a reason.
**`bench.json`** — per-model comparison (only after `bench`).
**`apply-log.json`** / **`apply-history.jsonl`** — written after `apply --yes`; the
history file is a stack so successive applies can be undone one at a time.
**`undo-log.json`** — written after `undo --yes`.

`apply` skips records flagged `needs_human` or below `min_confidence` unless
`--force` is passed, never overwrites an existing file (collisions get `" (2)"`),
and creates the destination folders as needed. `undo` reverses the most recent
apply (newest move first) using the history stack, and is itself a dry-run unless
`--yes`.

## Benchmarking models

```bash
mmmdocs bench /path/to/books --vision-models gemma4:e2b,gemma4:e4b,gemma4:12b --sample 5
```

Prints a summary and writes `bench.json`:

```
gemma4:e4b   json_ok=1.0 needs_human=0.0 avg=14.2s avg_bytes=51200
```

Metrics per model: JSON-valid rate, `needs_human` rate, average seconds per file,
and average image bytes — enough to choose on evidence.

## opencode integration

The same engine backs the opencode `rpdf_*` tools and the `rpdf` / `librarian`
agents. `~/.config/opencode/tools/rpdf.py` is a shim that imports this package;
point it elsewhere with `MMMDOCS_HOME`:

```bash
export MMMDOCS_HOME="/path/to/mmmdocs"
```

Agent variants (`rpdf-e2b`, `rpdf-e4b`, `rpdf-12b`, `rpdf-26b`, `rpdf-31b`, and
`librarian-*`) are generated from the base agent files:

```bash
python3 tools/make_agents.py            # regenerate ~/.config/opencode/agent/rpdf-*.md
python3 tools/make_agents.py --check    # report drift only
```

Apply a librarian plan from opencode with `/apply-organizer <dir>` (dry-run first).
Restart opencode after changing config, agents, or the shim.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `PDF not found` on a file that exists | macOS Unicode/curly-quote mismatch. `mmmdocs` normalizes NFC/NFD and falls back to a prefix match; if it still fails, pass the exact path from `manifest`. |
| `empty model response` / bad JSON | The model returned prose. `mmmdocs` already strips fences and extracts the first JSON object; try a larger model or lower `--workers`. |
| Vision call fails (no images read) | The model is not vision-capable. Use a `gemma4:*` model for `--vision-model`. |
| CJK or small annotations unreadable | Raise the profile for that run: `--profile high` (≈2 MB/page — avoid at scale). |
| Slow `run` | Reduce `--workers` if Ollama serializes, or run during idle time. Each file is 1–3 images. |
| Files moved into subfolders you did not expect | Review `move-plan.json` before `apply --yes`; `apply` is a dry-run by default. |
| Duplicate files skipped | Real twins are collapsed by `name_key`. Pass `--keep-duplicates` to classify both. |
| Files were moved but not renamed | The apply was built in `move` mode. Rebuild with `mmmdocs plan DIR --mode rename` (no model needed), then apply. |
| Path with backslashes/backticks rejected in the TUI | Don't paste a shell-escaped path; type it plainly or launch `mmmdocs` from the folder and press Enter. |
| Prompt/settings changes vanish after quitting | They are session-only until you press **s** (Save settings); the file is `<repo>/config.json`. |

## Layout

```
mmmdocs/
├── mmmdocs/
│   ├── __main__.py    # startup bootstrap -> TUI or CLI
│   ├── deps.py        # stdlib-only dependency checks + install prompts
│   ├── tui.py         # interactive menu (no external deps)
│   ├── engine.py      # pure PDF engine: info/text/manifest/render (no model calls)
│   ├── nodes.py       # Ollama + OpenAI-compatible model clients
│   ├── templates.py   # per-file prompt and JSON contract
│   ├── naming.py      # record -> clean filename (template + sanitizing)
│   ├── pipeline.py    # orchestration, plan/apply/undo, dry-run
│   └── cli.py         # argparse front-end (python -m mmmdocs <command>)
├── bin/mmmdocs         # launcher
├── tools/make_agents.py
├── config.example.json
└── requirements.txt
```

`engine.py` is intentionally free of model calls so it can be tested, reused, and
shared by both the CLI and the opencode tools.

## License

[MIT](LICENSE).

Agentically coded with **DeepSeek V4.1 Flash**.
