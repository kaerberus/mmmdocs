# mmmdocs

> Agentically coded with DeepSeek V4.1 Flash.

Catalog a folder of PDFs with a **local vision model**, then review a dry-run plan
before anything is moved. Built for mixed shelves — scanned books, magazines,
monographs, CJK scans, service/spec sheets — and small models: **one file is
classified at a time**, so no model ever has to hold more than a single cover in
its head. Runs against [Ollama](https://ollama.com) by default, so there are no
API keys.

```bash
curl -fsSL https://raw.githubusercontent.com/kaerberus/mmmdocs/main/install.sh | bash
cd /path/to/your/pdfs && mmmdocs
# 0 = YOLO, 1 = guided, 2 = change directory
```

---

## Contents

- [Install](#install)
- [The TUI](#the-tui)
- [Command line](#command-line)
- [How it works](#how-it-works)
- [Presets & detection](#presets--detection)
- [Custom prompts & other document types](#custom-prompts--other-document-types)
- [Modes, names & images](#modes-names--images)
- [Model per node](#model-per-node)
- [Configuration](#configuration)
- [Output files](#output-files)
- [Benchmarking models](#benchmarking-models)
- [opencode integration](#opencode-integration)
- [Troubleshooting](#troubleshooting)
- [Layout](#layout)
- [License](#license)

---

## Install

**One line** (clones to `~/.local/share/mmmdocs`, installs PyMuPDF, links
`mmmdocs` into `~/.local/bin` and adds it to your PATH):

```bash
curl -fsSL https://raw.githubusercontent.com/kaerberus/mmmdocs/main/install.sh | bash
```

Then `cd` into a folder of PDFs and run `mmmdocs` — it opens on that folder.
Re-run the installer any time to update. Env overrides:
`MMMDOCS_DIR`, `MMMDOCS_BRANCH`, `PYTHON`, and `MMMDOCS_PULL_MODEL=1` to also pull
`embeddinggemma`.

**Or clone and run it directly** — on startup `mmmdocs` checks every dependency,
offers to install what's missing, and drops you into a TUI:

```bash
git clone https://github.com/kaerberus/mmmdocs
cd mmmdocs
python3 -m mmmdocs
```

It checks, in order: **Python 3.9+** · **PyMuPDF** (offers `pip install`) ·
**Ollama** (offers `ollama serve`, or points at the download) · **the vision
model** (default `gemma4:e4b`, offers `ollama pull`). Only Python and PyMuPDF are
needed to start; without Ollama the metadata commands and the TUI still work.

Optional: `export PATH="$PWD/bin:$PATH"` to call `mmmdocs` instead of
`python3 -m mmmdocs`.

## The TUI

The TUI **opens on the directory you launched it from** (so `cd` where your PDFs
are, then run `mmmdocs`); option `2` changes it, with tab-completion. It shows:

```
mmmdocs — /path/to/docs   (79 PDFs)
  preset  books   model: gemma4:e4b   workers: 4
  status  ollama up (14 models)
  plan    79 classified, 79 planned

  0  YOLO — auto-detect, build, and organize everything
  1  Set up and run — guided
  2  Change directory   (/path/to/docs)
  u  Undo last apply
  s  Settings
  q  Quit
```

| | Detects type | You confirm type | You review the plan | On flagged files |
| --- | --- | --- | --- | --- |
| **0 YOLO** | yes | no (one `y/n`) | no | moves them anyway |
| **1 Guided** | yes | yes | yes | skips them (`f` forces) |

Long steps are never silent: detection, scanning, classifying, applying and
undoing show a live `Detecting....... 12s` indicator (growing dots + elapsed),
so you know it's working. Piped/scripted output stays clean — the indicator goes
to stderr and prints the label only once when there's no terminal.

### YOLO (0)

Detect the document type, print exactly what it will run, ask a single `y/n`, then
classify and **rename/move every file** (forced, including low-confidence ones). It
finishes with a report and `Undo: press u`.

### Guided run (1)

If a plan already exists:

```
An old plan already exists (79 files). Overwrite it with a fresh classification? [y/N]
```

- `N` (default) → **review & apply the existing plan** — no model spend.
- `y` → rebuild from scratch.

The build path:

1. **Document type** — `Detected: <label> (<method>, <conf>)`. If it differs from
   the current preset you choose **`[Y] use detected`** or **`k keep current`**;
   if it matches, just **`[Enter] continue`**. Either way, `p` picks another
   preset and `n` builds a new one (no second confirmation after choosing).
2. **Ready** — folder, preset, template, mode, model, workers, file count →
   `Classify and build the plan? [Y/n]`.
3. **Classify → plan** — progress, then counts and flagged files.
4. **Review → apply** — the paginated `old -> new` list, then
   `Apply now? [y/N]` (`f` forces, including flagged). Nothing moves until you
   confirm; cancelling leaves the plan saved.

### Settings (s)

Everything secondary lives here:

```
  1  Document type / preset    (books)
  2  Prompts                   (6, per node)
  3  Mode                      (rename)
  4  Name template             ({author} - {title} ({year}))
  5  Models & performance      -> vision/orchestrator, workers, profile, host
  6  Detection method          (model)
  ------ folder tools ------
  7  Scan folder (manifest)
  8  View catalog
  9  Rebuild plan from catalog
  c  Clean raster cache
  --------------------------
  w  Save settings -> config.json
  b  Back
```

Settings are session-only until you press **`w`**; the file is `<repo>/config.json`
and holds only the values you changed.

### Create a preset

**Settings → 1 → n**, then two inputs:

```
Preset name: Invoices & receipts
Fields (comma-separated, in order): vendor, invoice_number, date, total
```

The field list is the single source of truth: it becomes the filename schema
(`{vendor} - {invoice_number} - {date} - {total}`) **and** the JSON schema the
vision model is asked to return (`confidence` and `needs_human` are added
automatically).
Then:

```
  e edit prompt   f edit filename   d edit description   t test   s save   c cancel
```

- `t` classifies one real file with the **unsaved** draft and prints the extracted
  fields and resulting filename — iterate until it's right (skipped if Ollama is
  offline).
- `s` saves it to `config.json`, activates it, and it's immediately available to
  detection and the run modes.
- **Settings → 1** also lets you pick a preset, or `e` edit / `x` delete an
  existing **user** preset (built-ins can be shadowed but not changed).

### Customize prompts

**Settings → 2**. Each node has an editable instruction and system message. It
shows `old prompt:`, then `new prompt:` where **empty resets to the built-in
default**, a typed line replaces it, and **`edit`** opens `$EDITOR` for multi-line
work. Save with **Settings → w**.

## Command line

Run `mmmdocs <command> --help` for the full flag list.

| Command | Purpose |
| --- | --- |
| *(none)* / `tui` | Launch the interactive menu. |
| `manifest DIR` | Compact metadata for every PDF in a directory. `--recursive`, `--sample N`. |
| `run DIR` | Classify all files, write `catalog.json` + `move-plan.json`. `--preset`, `--mode`, `--name-template`, `--only NAME` (repeatable), `--limit N`, `--keep-duplicates`. |
| `plan DIR` | Rebuild `move-plan.json` from an existing `catalog.json` — **no model calls**. `--preset`, `--mode`, `--name-template`. |
| `presets` | List built-in and user-defined presets. |
| `detect DIR` | Suggest the best preset for a folder. `--detect-method model\|auto\|heuristic`. |
| `scan DIR` | Fast embedding scan: clusters, mixed groups, near-duplicates, outliers. `--limit N`, `--json`. |
| `bench DIR` | Compare vision models. `--vision-models a,b,c`, `--sample N`. |
| `apply DIR` | Apply `move-plan.json` (rename and/or move). Dry-run unless `--yes`; `--force`, `--min-confidence X`. |
| `undo DIR` | Reverse the last apply. Dry-run unless `--yes`. |
| `classify-one FILE` | Classify a single PDF, print its JSON record (debugging). |
| `info PDF` | Page count, sizes, text-layer stats, TOC (`toc_source`). |
| `text PDF` | Extract the text layer. `--pages SPEC`, `--head 5`, `--max-chars N`. |
| `render PDF --pages SPEC` | Rasterize pages. `--profile low\|classify\|default\|high`. |
| `cleanup` | Delete cached renders. `--pdf FILE` for one file, else the whole cache. |

```bash
mmmdocs manifest /path/to/docs          # inspect, no model
mmmdocs run     /path/to/docs           # classify -> catalog.json + move-plan.json
mmmdocs apply   /path/to/docs           # dry-run review
mmmdocs apply   /path/to/docs --yes     # apply
mmmdocs undo    /path/to/docs --yes     # reverse the last apply
```

Page specs are 1-based: `5`, `5-10`, `1,3,7-9`, or `all`. Progress indicators go
to stderr, so `mmmdocs detect … | jq` (or any piped command) still receives clean
stdout.

## How it works

```
mmmdocs run DIR
      │
      ├─ manifest   scan with PyMuPDF: pages, text layer, TOC quality,
      │             duplicate groups                              (no model)
      ├─ workers    ONE isolated process per PDF:
      │               render pages 1-3 (profile classify, ~35 KB each)
      │               + optional text excerpt -> ONE vision call -> JSON record
      ├─ taxonomy   one orchestrator call groups titles into folders
      │             (only when the mode moves files; deterministic fallback)
      └─ write      catalog.json + move-plan.json       (nothing is changed yet)

mmmdocs apply DIR [--yes]   apply the plan, dry-run unless --yes
mmmdocs undo  DIR [--yes]   reverse the last apply
```

Isolation is the point: a small model that only ever sees one cover cannot confuse
one book's title with another book's body text.

## Presets & detection

A **preset** bundles the settings that differ between document types — the vision
prompt, the filename template, and the mode — so you pick "what kind of folder is
this" instead of knowing template syntax. Built-ins: `books`, `spec-sheet`,
`magazines`. Add your own under `presets` in `config.json` (or build one in the
TUI); they appear in the picker and in detection.

```json
"presets": {
  "invoices": {
    "label": "Invoices & receipts",
    "description": "Bills and receipts; vendor, invoice number, date, total.",
    "name_template": "{vendor} - {invoice_number} - {date}",
    "mode": "rename",
    "vision_prompt": "Read this invoice and return JSON: vendor, invoice_number, date, total, needs_human.",
    "detect": {"regex": "(?i)invoice\\s*(no|number|#)?\\s*[:#]?\\s*[A-Z0-9-]{3,}", "keywords": ["invoice", "receipt", "total due"]}
  }
}
```

**Detection** samples the text layer only (no rendering) and suggests a preset:

- Heuristics score every preset's `detect` signature (`regex`/`keywords`) against
  the sample — always computed.
- The **detection node** (default: the orchestrator) then chooses, using a
  configurable prompt that includes the heuristic scores and flags user-defined
  presets. A strong match on a user preset beats a generic model pick.
- `detect_method`: `model` (default), `auto` (heuristics first, model only when
  unclear), or `heuristic` (no model).

In the TUI, guided run (`1`) detects and asks you to confirm the type; run it on
demand with **Settings → 1 → d**. From the shell:

```bash
mmmdocs presets                  # list built-ins + user presets
mmmdocs detect "/path/to/docs"   # -> {preset: invoices, confidence: .., scores: {...}}
mmmdocs run "/path/to/docs" --preset auto        # detect, then apply
mmmdocs run "/path/to/docs" --preset invoices    # force one
```

**When nothing fits, detection proposes a schema.** Alongside the preset it can
return a `label` and an ordered field list (frequent `Label:` lines are mined from
the sample too), and mmmdocs builds the filename template *and* the JSON prompt
from that list — the same generator as the TUI preset builder. So a folder of a
novel document type gets a real schema instead of a wrong built-in:

- **Guided (1)** shows `Proposed schema: {supplier} - {date} - {order_number}` and
  offers `[Enter] use proposed`, `e edit & save` (opens the prefilled builder),
  `p pick`, `n new`.
- **YOLO (0)** builds it silently for that run and **never writes `config.json`**
  (it stays ephemeral); set `yolo_schema: ask` to review/save it instead.
- Turn the behaviour off with `detect_fields: false`; a chosen preset with no
  heuristic support and confidence below `detect_min_match` counts as "no match".

Because presets are just defaults, editing the prompt or template afterwards marks
the preset `(customized)` and your edit wins.

## Fast scan (embeddings)

Large, uncategorized folders get a cheap qualitative pass before anything
expensive. mmmdocs embeds each document's first-page text with a small embedding
model and reasons over the vectors:

```bash
ollama pull embeddinggemma        # ~300M, multilingual; default embed_model
mmmdocs scan "/path/to/docs"      # clusters, mixed groups, dupes, outliers
mmmdocs scan "/path/to/docs" --limit 500 --json
```

- **Clusters** files with pure-Python leader clustering (cosine), **assigns
  presets** by nearest centroid, and flags a **mixed** folder when several groups
  are substantial.
- Flags **near-duplicates** and **outliers**, and writes `scan-groups.json` with
  each group's file list (so it's actionable without per-group runs yet).
- Feeds the generative detector a **cluster-stratified sample** instead of a fixed
  stride, so minority types aren't missed.

In the TUI, guided run (`1`) asks *"Are all documents in this directory similarly
structured (all books / all papers / all magazines)?"*. If not, it scans, shows
the groups, and can process the dominant group now. YOLO (`0`) scans and **stops**
on a mixed folder rather than applying one schema across incompatible types.
`scan_method: auto` uses embeddings when the model is available and falls back to
the model-only path otherwise.

## Custom prompts & other document types

The same values are editable from the CLI (empty string resets). The per-file
classifier appends the file name, PDF metadata, a text excerpt and the rendered
pages after your instruction, so a custom prompt only replaces the instructions:

```bash
mmmdocs run "/path/to/docs" \
  --vision-prompt "Read the datasheet and return JSON with keys: model, serial, voltage, needs_human." \
  --name-template "{model} - {serial}"
```

**You own the output schema**: every extra JSON field the model returns is
preserved and can be referenced in `name_template` (a `title` is not required).
Unreadable or ambiguous items should set `"needs_human": true`. The taxonomy prompt
must still return `{"folders": {...}, "reason": "..."}`, and grouping needs a
non-empty `title` (rename mode does not).

## Modes, names & images

`run` and `plan` take `--mode` (config `mode`, default `rename`):

| mode | folder | filename |
| --- | --- | --- |
| `catalog` | unchanged | unchanged |
| `move` | taxonomy folders | unchanged |
| `rename` | current folder | template name |
| `rename-move` | taxonomy folders | template name |

Filenames come from `--name-template` (config `name_template`, default
`{author} - {title} ({year})`). Fields: `{title}`, `{author}`, `{year}`,
`{doc_type}`, `{language}`, `{topics}`, plus any custom field your prompt returns.
Missing fields are dropped cleanly — an empty author gives `Title (Year)`, not
` - Title ()`.

**Filename safety** is deterministic (no reliance on prompt wording): illegal
characters `< > : " / \ | ? *` and control characters are removed, empty `()`/`[]`
and dangling separators collapse, Windows reserved stems (`CON`, `PRN`, `AUX`,
`NUL`, `COM1–9`, `LPT1–9`) get a leading `_`, names are stripped of trailing
spaces/dots, and capped at 150 characters (extension kept). Records that still
produce no name keep their original filename and are flagged for review.

**Render profiles** (the vision node reads 1–3 images per file):

| Profile | DPI | Long edge | Format | Typical size | Use for |
| --- | --- | --- | --- | --- | --- |
| `classify` | 96 | 768 px | JPEG q70 | ~35 KB | covers / title pages (default in `run`) |
| `low` | 100 | 1024 px | JPEG q80 | ~100–180 KB | quick overviews, photos |
| `default` | 150 | 1600 px | JPEG q85 | ~150–500 KB | normal reading |
| `high` | 250 | 2200 px | PNG | ~2 MB | small type and CJK body text |

## Model per node

- **vision node** — reads the cover/title page and produces the record.
- **orchestrator node** — groups the records into folders (and drives detection).

They can be different models, locally or in the cloud:

```bash
mmmdocs run /path/to/books \
  --vision-model gemma4:e4b \
  --orchestrator-input openai --orchestrator-model deepseek-chat \
  --openai-api-key "$DEEPSEEK_API_KEY"
```

Flags: `--vision-model`, `--vision-input ollama|openai`, `--orchestrator-model`,
`--orchestrator-input`, `--ollama-host`, `--openai-base-url`, `--openai-api-key`,
`--workers`, `--profile`.

> The vision node must be **vision-capable** (the `gemma4:*` family is). Text-only
> models such as `qwen3.8:27b` should only be the orchestrator/detection node.

## Configuration

All keys are optional and live in `<repo>/config.json` (not `$HOME`, so settings
travel with the folder). CLI flags override the file, and the TUI writes it with
`Settings → w`. Start from `config.example.json` for a documented template.

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
| `name_template` | `{author} - {title} ({year})` | filename template |
| `vision_prompt` / `vision_system_prompt` | *(null)* | classifier instruction / system |
| `orchestrator_prompt` / `orchestrator_system_prompt` | *(null)* | taxonomy instruction / system |
| `detection_prompt` / `detection_system_prompt` | *(null)* | detection instruction / system |
| `detection_input` / `detection_model` | *(null)* | node used for detection (default: orchestrator) |
| `detect_method` | `model` | `model`, `auto`, or `heuristic` |
| `detect_sample` | `15` | files sampled during detection |
| `detect_fields` | `true` | let detection propose a schema when nothing fits |
| `detect_min_match` | `0.5` | confidence below which a preset counts as "no match" |
| `yolo_schema` | `auto` | YOLO uses a proposed schema (`auto`) or opens the builder (`ask`) |
| `scan_method` | `auto` | `auto`, `embedding`, or `model` for the fast scan |
| `embed_input` / `embed_model` | `ollama` / `embeddinggemma` | embedding backend and model |
| `embed_batch` | `64` | texts per embedding request |
| `scan_max` | `0` | max files to embed (`0` = all) |
| `cluster_threshold` / `merge_threshold` | `0.7` / `0.75` | cosine to join a cluster / to merge two clusters |
| `max_clusters` | `24` | cluster cap |
| `dup_threshold` / `outlier_threshold` | `0.95` / `0.55` | near-duplicate and outlier cosine cutoffs |
| `scan_classifier` / `detect_vision` | *(null)* / `false` | optional VL classifier for text-less covers |
| `preset` | `books` | active preset, or `auto`, or `custom` |
| `presets` | `{}` | user-defined presets (see `config.example.json`) |


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
      "confidence": 1.0,
      "needs_human": false,
      "classified_by": "gemma4:e4b",
      "images_used": 3,
      "bytes_used": 49792
    }
  ]
}
```

**`move-plan.json`** — a proposed destination (folder and/or filename) per file,
with `original_name`, `proposed_name`, `renamed`, `confidence` and a reason.
**`bench.json`** — per-model comparison (after `bench`).
**`apply-log.json`** / **`apply-history.jsonl`** — after `apply --yes`; the history
is a stack so successive applies undo one at a time.
**`undo-log.json`** — after `undo --yes`.

`apply` skips records flagged `needs_human` or below `min_confidence` unless
`--force` is passed, never overwrites an existing file (collisions get `" (2)"`),
and creates destination folders as needed. `undo` reverses the most recent apply
(newest move first) and is itself a dry-run unless `--yes`.

## Benchmarking models

```bash
mmmdocs bench /path/to/books --vision-models gemma4:e2b,gemma4:e4b,gemma4:12b --sample 5
```

Prints a summary and writes `bench.json`:

```
gemma4:e4b   json_ok=1.0 needs_human=0.0 avg=14.2s avg_bytes=51200
```

Metrics per model: JSON-valid rate, `needs_human` rate, average seconds per file,
average image bytes — enough to choose on evidence.

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
python3 tools/make_agents.py            # regenerate
python3 tools/make_agents.py --check    # report drift only
```

Apply a librarian plan from opencode with `/apply-organizer <dir>` (dry-run first).
Restart opencode after changing config, agents, or the shim.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `PDF not found` on a file that exists | macOS Unicode/curly-quote mismatch. `mmmdocs` normalizes NFC/NFD and falls back to a prefix match; if it still fails, pass the exact path from `manifest`. |
| `empty model response` / bad JSON | The model returned prose. `mmmdocs` strips fences and extracts the first JSON object; try a larger model or lower `--workers`. |
| Vision call fails (no images read) | The model is not vision-capable. Use a `gemma4:*` model for `--vision-model`. |
| CJK or small annotations unreadable | Raise the profile: `--profile high` (≈2 MB/page — avoid at scale). |
| Slow `run` | Reduce `--workers` if Ollama serializes, or run during idle time. Each file is 1–3 images. |
| Files moved somewhere unexpected | Review the plan first (guided run, or `apply` without `--yes`); `apply` is a dry-run by default. `u` undoes. |
| Duplicate files skipped | Real twins are collapsed by `name_key`. Pass `--keep-duplicates` to classify both. |
| Files were moved but not renamed | The plan was built in `move` mode. Rebuild with `mmmdocs plan DIR --mode rename` (no model), then apply. |
| Path with backslashes rejected in the TUI | Don't paste a shell-escaped path; type it plainly or launch `mmmdocs` from the folder and press Enter. |
| Settings/prompt changes vanish after quitting | They are session-only until **Settings → w**; the file is `<repo>/config.json`. |

## Layout

```
mmmdocs/
├── mmmdocs/
│   ├── __main__.py    # startup bootstrap -> TUI or CLI
│   ├── deps.py        # stdlib-only dependency checks + install prompts
│   ├── tui.py         # interactive menu (no external deps)
│   ├── engine.py      # pure PDF engine: info/text/manifest/render (no model calls)
│   ├── nodes.py       # Ollama + OpenAI-compatible model clients
│   ├── templates.py   # per-file + detection prompts and JSON contract
│   ├── presets.py     # built-in presets, field-driven builder, detection
│   ├── naming.py      # record -> clean filename (template + sanitizing)
│   ├── pipeline.py    # orchestration, plan/apply/undo, dry-run
│   └── cli.py         # argparse front-end (python -m mmmdocs <command>)
├── bin/mmmdocs        # launcher
├── install.sh         # curl | bash installer
├── tools/make_agents.py
├── config.example.json
├── LICENSE
└── requirements.txt
```

`engine.py` is intentionally free of model calls so it can be tested, reused, and
shared by both the CLI and the opencode tools.

## License

[MIT](LICENSE).

Agentically coded with **DeepSeek V4.1 Flash**.
