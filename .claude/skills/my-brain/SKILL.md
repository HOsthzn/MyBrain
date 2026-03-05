---
name: my-brain
description: Skills for the My Brain AI-augmented learning system. Use when analyzing knowledge gaps, planning notes, writing Obsidian notes from research, extracting topics from natural language, or scouting learning resources.
---

# My Brain — Learning System Skills

These skills are used by the My Brain orchestrator to build a personal knowledge base.

## SKILL: topic_extract

**When:** Natural language learning request.
**Task:** Extract the core topic keyword(s) for use as a knowledge base category.

Rules:
- Extract the specific subject, not the intent ("I want to learn X" → "X")
- 2-5 words max, proper casing, standard terminology
- Include language/framework context: "async in C#" → "C# Async/Await"
- Natural languages get "Language" suffix: "Dutch" → "Dutch Language"

Examples:
| Input | Extracted |
|---|---|
| "I want to learn Dutch" | "Dutch Language" |
| "teach me about async in C#" | "C# Async/Await" |
| "how do databases work" | "Database Fundamentals" |
| "I need to understand Docker networking" | "Docker Networking" |

## SKILL: analyze_vault

**When:** Given a vault scan (JSON) and a topic.
**Task:** Analyze existing knowledge and identify gaps.

Rules:
- Read vault scan carefully — note names, tags, wikilinks show what exists
- Gaps should be specific subtopics, not vague categories
- Learning path should be ordered foundational → advanced
- Search queries should be specific enough for quality resources

Output: JSON object (no fences):
```
{
  "topic": "Extracted Topic Name",
  "knowledge_level": "none|beginner|intermediate|advanced",
  "existing_coverage": ["subtopics already covered"],
  "gaps": ["specific subtopics NOT yet in vault"],
  "suggested_learning_path": ["ordered list"],
  "search_queries": ["5-8 targeted search queries"]
}
```

## SKILL: scout_resources

**When:** Given a topic and knowledge gaps.
**Task:** Find high-quality learning resources.

Rules:
- Prefer official docs, established courses, well-known YouTube educators
- Mix of: docs, videos, articles, papers
- Each resource maps to a specific gap
- URLs must be real — do not fabricate

Output: JSON object (no fences):
```
{
  "resources": [
    { "title": "Title", "url": "https://...", "type": "youtube|article|documentation|paper", "covers": "which gap" }
  ]
}
```

## SKILL: plan_notes

**When:** Given research content, a topic, and a VAULT INDEX of existing notes.
**Task:** Plan 8-12 individual notes organizing research into focused concepts.

Rules:
- Each note = ONE focused concept
- Name as concepts, not chapters ("CSharp Async Await" not "Chapter 1")
- Every note links to at least 2 others in the plan
- Max 12 notes — focused > exhaustive
- Names should make good [[wikilinks]]
- **FILENAMES:** Must be safe for Windows/Mac/Linux. Replace `#` with `Sharp` (e.g. `C#` → `CSharp`), replace `/` with ` - ` (e.g. `I/O` → `I-O` or `IO`). No `:`, `*`, `?`, `<`, `>`, `|`, `"` characters.
- **DEDUPLICATION:** Check the vault index provided. Do NOT create notes for topics already covered. Instead, link to existing notes.
- **LINKING:** Include `links_to` entries for both new notes in the plan AND existing notes from the vault index.
- If content partially overlaps with an existing note, name the new note to cover ONLY the new aspect.

Output: JSON array (no fences):
```
[
  { "filename": "Concept Name.md", "title": "Concept Name", "summary": "One sentence", "links_to": ["Other Concept"] }
]
```

## SKILL: write_note

**When:** Given a title, summary, source research, and related notes.
**Task:** Write a single Obsidian markdown note that captures the FULL depth of the source material for this concept.

**CRITICAL: Output raw markdown. NO ```markdown fences wrapping the output.**

Use the obsidian-markdown skill for proper Obsidian syntax (wikilinks, callouts, properties, etc).

### Philosophy
The note should be a **comprehensive reference** someone can return to and fully understand the concept without re-reading the original source. DO NOT water down or summarize — transfer the knowledge with full detail, examples, and nuance.

### Structure:
1. YAML frontmatter (--- delimited) with: title, created, tags, status
2. `# Title`
3. 1-2 sentence overview that contextualizes the concept
4. Main body sections — use whatever headings fit the content naturally. DON'T force a rigid template. Some notes might have:
   - `## Overview` → what it is and why it matters
   - `## How It Works` → mechanics, with code examples
   - `## Categories / Types` → breakdowns with details for each
   - `## Practical Usage` → when and how to use it
   - `## Common Pitfalls` → mistakes and gotchas
   - `## Relationship to X` → how it connects to other concepts
5. `## Key Takeaways` — 3-5 essential points
6. `## Questions & Gaps` — anything unclear, debated, or needing deeper research
7. `## Sources` — numbered list with URLs

### Content depth:
- **400-800 words** for the main body (more if the source material warrants it)
- Include ALL specific details from the source: numbers, ranges, types, categories
- Include code examples where relevant — use proper syntax-highlighted code blocks
- Preserve technical precision — if the source says "32-bit signed integer ranging from -2 billion to +2 billion", keep that detail
- Use tables where they organize information naturally (type comparisons, flag lists, etc.)
- Don't genericize — "there are several types" is useless, list them with details

### Linking:
- Use `[[wikilinks]]` on first mention of related concepts
- Link to notes in the plan AND concepts that might exist elsewhere in vault
- Prefer `[[Note Title]]` over `[[Note Title|display text]]`

### Quality:
- `> [!tip]` for practical advice and best practices
- `> [!warning]` for common pitfalls and performance concerns
- `> [!example]` for illustrative examples
- Inline `code` for types, methods, keywords, and commands
- Code blocks for multi-line examples
- Be specific and technical — this is a knowledge base, not a blog post

## VAULT RULES (always apply)

- NEVER delete existing notes
- ALWAYS add `ai-generated` and `needs-review` tags
- One concept per note — link related concepts
- Fill gaps, don't duplicate
- When called via `claude -p`: output ONLY the requested format (JSON or markdown), no conversation