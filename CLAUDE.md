# CLAUDE.md — My Brain

You are part of the **My Brain** AI-augmented learning system. You help build and maintain a personal Obsidian knowledge base.

## Skills

Skills are in `.claude/skills/`. Read the relevant SKILL.md before acting:

| Skill | When to use |
|---|---|
| `my-brain` | Topic extraction, vault analysis, note planning, note writing |
| `obsidian-markdown` | All Obsidian markdown syntax — wikilinks, callouts, properties, embeds |
| `obsidian-cli` | Interacting with Obsidian via CLI (read, create, search, append) |
| `json-canvas` | Creating .canvas files for visual maps |
| `defuddle` | Extracting clean content from web URLs |

## Response Format

When called via `claude -p`, respond with ONLY the requested output:
- JSON requests → raw JSON (no ```json fences)
- Note requests → raw markdown (no ```markdown fences)
- Never add conversational text before or after

## Folder Structure

- `00 - Inbox/` — New AI-generated notes
- `01 - Knowledge/` — Validated notes (human moves here)
- `02 - Resources/` — Source tracking
- `03 - NotebookLM Prompts/` — Generated prompts
- `04 - Staging/` — NotebookLM output waiting to process