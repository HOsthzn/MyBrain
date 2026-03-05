# My Brain

**Learn anything and everything — by using, and not abusing, AI.**

A local web UI that orchestrates **Claude Code + Obsidian + NotebookLM** into a personal knowledge-building workflow.

## Prerequisites

- Python 3.9+
- Node.js 18+ with Claude Code CLI: `npm i -g @anthropic-ai/claude-code`
- An Obsidian vault (any folder with `.md` files)

## Quick Start

```bash
pip install -r requirements.txt
python server.py
```

Open `http://localhost:5000` in your browser, then go to the **Config** tab to point the app at your vault.

## The Flow

```
┌─────────────────────────────────────────────────────────┐
│  1. LEARN TAB: Type what you want to learn              │
│     → Claude scans vault + semantic index for gaps      │
│     → Claude scouts YouTube, articles, docs, papers     │
│     → Generates a tailored NotebookLM prompt            │
│                                                         │
│  2. YOU: Paste the prompt into NotebookLM               │
│     → NotebookLM does deep multi-source research        │
│                                                         │
│  3. STAGING TAB: Upload NotebookLM output               │
│     → Drop files or paste text directly                 │
│     → Multiple files supported                          │
│                                                         │
│  4. LEARN TAB: Click "Validate & Vault"                 │
│     → Claude plans 8–12 focused, interlinked notes      │
│     → Writes one note at a time (avoids timeouts)       │
│     → Creates MOC + Resources note                      │
│     → Python writes files directly — no hallucinated IO │
└─────────────────────────────────────────────────────────┘
```

## Project Structure

```
my-brain/
├── server.py          ← Flask backend — API, orchestration, vault I/O
├── embeddings.py      ← Optional semantic search (hnswlib + sentence-transformers)
├── static/
│   └── index.html     ← Single-file web UI
├── config.yaml        ← Your settings (vault path, folder names, etc.)
├── CLAUDE.md          ← Skill instructions for Claude Code
├── .claude/
│   └── skills/        ← Claude Code skills (my-brain, obsidian-markdown, etc.)
└── requirements.txt
```

## Vault Structure

```
MyBrain/                         ← your Obsidian vault root
├── 00 - Inbox/                  ← AI-generated notes land here
├── 01 - Knowledge/              ← Move validated notes here manually
├── 02 - Resources/              ← Source-tracking notes
├── 03 - NotebookLM Prompts/     ← Generated prompts, saved for reuse
├── 04 - Staging/                ← Drop NotebookLM output here
├── 05 - Learning/               ← In-progress learning sessions
├── Templates/
├── _index.json                  ← Auto-generated vault index (used by Claude)
├── _index.md                    ← Human-readable vault index
└── CLAUDE.md                    ← Auto-synced from project on each run
```

## Features

- **Semantic vault scanning** — uses `sentence-transformers` + `hnswlib` to find related notes beyond keyword matching. Degrades gracefully if not installed.
- **Gap analysis** — Claude identifies what you already know and focuses new notes on missing concepts.
- **Resource scouting** — Claude searches YouTube, articles, documentation, and papers and ranks sources.
- **NotebookLM integration** — generates a structured prompt you paste directly into NotebookLM.
- **Atomic note writing** — notes are written one at a time with full wikilink cross-referencing.
- **Persistent vault index** — `_index.json` is rebuilt on every session so Claude always has an up-to-date map of your vault.
- **Rate-limit handling** — automatically retries Claude CLI calls with back-off on API limits.

## Dependencies

| Package | Purpose | Required |
|---|---|---|
| `flask` | Web server | Yes |
| `pyyaml` | Config parsing | Yes |
| `sentence-transformers` | Semantic embeddings | Optional |
| `hnswlib` | Vector index | Optional |
| `numpy` | Embedding math | Optional |

Install all including optional:
```bash
pip install -r requirements.txt
```

Install core only (no semantic search):
```bash
pip install flask pyyaml
```

## Configuration

Edit `config.yaml` or use the **Config** tab in the UI:

```yaml
vault_path: "/path/to/your/obsidian/vault"
notes_folder: "00 - Inbox"
knowledge_folder: "01 - Knowledge"
resources_folder: "02 - Resources"
staging_folder: "04 - Staging"
prompts_folder: "03 - NotebookLM Prompts"
templates_folder: "Templates"
default_tags:
  - "ai-generated"
  - "needs-review"
note_format:
  frontmatter: true
  include_sources: true
  link_style: "wikilink"
search:
  max_resources: 10
  preferred_sources:
    - youtube
    - article
    - documentation
    - paper
```
