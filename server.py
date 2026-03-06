#!/usr/bin/env python3
"""
MY BRAIN — Server
Flask app that orchestrates Claude Code × Obsidian × NotebookLM
"""

import subprocess
import sys
import os
import json
import yaml
import re
import shutil
import threading
import time
from pathlib import Path
from datetime import datetime
from textwrap import dedent
from flask import Flask, request, jsonify, send_from_directory

# ──────────────────────────────────────────────────────────────
# PATHS
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
CLAUDE_MD_SRC = SCRIPT_DIR / "CLAUDE.md"
STATIC_DIR = SCRIPT_DIR / "static"

# Ensure static dir exists
if not STATIC_DIR.exists():
    print(f"  ✗ Static folder not found: {STATIC_DIR}")
    print(f"    Expected: {STATIC_DIR / 'index.html'}")
    sys.exit(1)

app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f) or {}
    # Resolve vault path
    vp = cfg.get("vault_path", "")
    if vp:
        cfg["_vault"] = str(Path(vp).expanduser().resolve())
    return cfg


def save_config(cfg: dict):
    # Don't save internal keys
    to_save = {k: v for k, v in cfg.items() if not k.startswith("_")}
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(to_save, f, default_flow_style=False)


def get_vault() -> Path | None:
    cfg = load_config()
    vp = cfg.get("_vault")
    if vp and Path(vp).exists():
        return Path(vp)
    return None


def ensure_vault_structure(vault: Path, cfg: dict):
    folders = [
        cfg.get("notes_folder", "00 - Inbox"),
        cfg.get("knowledge_folder", "01 - Knowledge"),
        cfg.get("resources_folder", "02 - Resources"),
        cfg.get("staging_folder", "04 - Staging"),
        cfg.get("prompts_folder", "03 - NotebookLM Prompts"),
        cfg.get("templates_folder", "Templates"),
    ]
    for f in folders:
        (vault / f).mkdir(parents=True, exist_ok=True)

    # Sync CLAUDE.md to vault
    cm = vault / "CLAUDE.md"
    if CLAUDE_MD_SRC.exists():
        if not cm.exists() or CLAUDE_MD_SRC.stat().st_mtime > cm.stat().st_mtime:
            shutil.copy2(CLAUDE_MD_SRC, cm)

    # Sync .claude/skills/ to vault
    src_skills = SCRIPT_DIR / ".claude"
    dst_skills = vault / ".claude"
    if src_skills.exists():
        if dst_skills.exists():
            shutil.rmtree(dst_skills)
        shutil.copytree(src_skills, dst_skills)


# ──────────────────────────────────────────────────────────────
# STATE — per-topic tracking
# ──────────────────────────────────────────────────────────────

def state_path(vault: Path) -> Path:
    return vault / ".brain_state.json"


def load_state(vault: Path) -> dict:
    p = state_path(vault)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {"topics": {}, "active_topic": None}


def save_state(vault: Path, state: dict):
    state_path(vault).write_text(json.dumps(state, indent=2), encoding="utf-8")


# ──────────────────────────────────────────────────────────────
# VAULT SCANNER
# ──────────────────────────────────────────────────────────────

def scan_vault(vault: Path, topic: str) -> dict:
    keywords = topic.lower().split()
    existing = []
    tags = set()
    total = 0

    for md in vault.rglob("*.md"):
        if md.name.startswith(".") or md.name == "CLAUDE.md":
            continue
        total += 1
        try:
            content = md.read_text("utf-8", errors="ignore")
        except Exception:
            continue

        cl = content.lower()
        rel = sum(3 if kw in md.stem.lower() else 0 for kw in keywords)
        rel += sum(1 if kw in cl else 0 for kw in keywords)

        if rel > 0:
            # Extract tags from frontmatter
            note_tags = []
            fm = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
            if fm:
                try:
                    meta = yaml.safe_load(fm.group(1))
                    if isinstance(meta, dict) and "tags" in meta:
                        t = meta["tags"]
                        note_tags = t if isinstance(t, list) else [t]
                        tags.update(note_tags)
                except Exception:
                    pass

            links = re.findall(r"\[\[([^\]]+)\]\]", content)
            existing.append({
                "path": str(md.relative_to(vault)),
                "name": md.stem,
                "relevance": rel,
                "tags": note_tags,
                "links": links[:10],
                "size": len(content),
            })

    existing.sort(key=lambda x: x["relevance"], reverse=True)
    return {
        "topic": topic,
        "total_notes": total,
        "related_count": len(existing),
        "related": existing[:20],
        "tags": sorted(tags),
    }


# ──────────────────────────────────────────────────────────────
# VAULT INDEX
# ──────────────────────────────────────────────────────────────

INDEX_FILE = "_index.json"
skip_dirs = {".claude", ".obsidian", ".git", ".trash", "node_modules"}


def build_index(vault: Path) -> dict:
    """
    Build a comprehensive index of every note in the vault.
    Returns a dict keyed by relative path with metadata for each note.
    """
    index = {}
    skip_dirs = {".claude", ".obsidian", ".git", ".trash", "node_modules"}

    for md in vault.rglob("*.md"):
        if not md.is_file() or md.name.startswith(".") or md.name == "CLAUDE.md":
            continue
        rel_parts = md.relative_to(vault).parts
        if any(part.startswith(".") or part in skip_dirs for part in rel_parts[:-1]):
            continue
        # Skip index files
        if md.name in ("_index.json", "_index.md"):
            continue

        rel_path = str(md.relative_to(vault))
        try:
            content = md.read_text("utf-8", errors="ignore")
        except Exception:
            continue

        # Extract frontmatter
        title = md.stem
        note_tags = []
        status = "unknown"
        fm = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if fm:
            try:
                meta = yaml.safe_load(fm.group(1))
                if isinstance(meta, dict):
                    title = meta.get("title", md.stem)
                    t = meta.get("tags", [])
                    note_tags = t if isinstance(t, list) else [str(t)]
                    status = meta.get("status", "unknown")
            except Exception:
                pass

        # Extract wikilinks
        links = list(set(re.findall(r"\[\[([^\]|]+)", content)))

        # Extract headings as keywords/topics
        headings = re.findall(r"^#{1,3}\s+(.+)$", content, re.MULTILINE)
        # Clean headings
        keywords = [h.strip().strip("#").strip() for h in headings if len(h.strip()) > 2]
        # Remove the title itself from keywords
        keywords = [k for k in keywords if k.lower() != title.lower()]

        # Extract first paragraph as summary (after frontmatter and title)
        body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, flags=re.DOTALL)
        body = re.sub(r"^#.*\n+", "", body).strip()
        first_para = body.split("\n\n")[0].strip() if body else ""
        summary = first_para[:200] if first_para else ""

        index[rel_path] = {
            "title": title,
            "folder": rel_parts[0] if len(rel_parts) > 1 else "root",
            "tags": note_tags,
            "links_to": links[:20],
            "keywords": keywords[:15],
            "summary": summary,
            "status": status,
            "size": len(content),
        }

    return index


def save_index(vault: Path, index: dict):
    """Save index to vault root as JSON and as a readable Markdown file."""
    # JSON version (for programmatic use)
    p = vault / INDEX_FILE
    p.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

    # Markdown version (for human readability + Claude reads this from vault)
    md_lines = [
        "---",
        "title: Vault Index",
        f"updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"total_notes: {len(index)}",
        "tags: [system, index]",
        "---",
        "",
        "# Vault Index",
        "",
        f"> Auto-generated index of all {len(index)} notes. Used by Claude to prevent duplicates and build links.",
        "",
    ]

    # Group by folder
    by_folder = {}
    for path, info in sorted(index.items()):
        folder = info.get("folder", "root")
        by_folder.setdefault(folder, []).append((path, info))

    for folder in sorted(by_folder.keys()):
        md_lines.append(f"## {folder}")
        md_lines.append("")
        for path, info in by_folder[folder]:
            title = info["title"]
            keywords = info.get("keywords", [])
            links = info.get("links_to", [])
            tags = info.get("tags", [])
            summary = info.get("summary", "")

            md_lines.append(f"### [[{title}]]")
            if keywords:
                md_lines.append(f"**Topics:** {', '.join(keywords[:10])}")
            if tags:
                clean_tags = [t for t in tags if t not in ("ai-generated", "needs-review")]
                if clean_tags:
                    md_lines.append(f"**Tags:** {', '.join(clean_tags)}")
            if links:
                md_lines.append(f"**Links:** {', '.join(f'[[{l}]]' for l in links[:8])}")
            if summary:
                md_lines.append(f"> {summary[:150]}")
            md_lines.append("")

    md_path = vault / "_index.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")


def load_index(vault: Path) -> dict:
    """Load existing index from vault."""
    p = vault / INDEX_FILE
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {}


def get_index_for_prompt(vault: Path, max_chars: int = 12000) -> str:
    """
    Get a compact text representation of the index for Claude prompts.
    Focuses on titles, keywords, links, and summaries — enough for dedup and linking.
    """
    index = load_index(vault)
    if not index:
        return "VAULT INDEX: Empty — no existing notes."

    lines = [f"VAULT INDEX ({len(index)} existing notes):"]
    char_count = 0

    for path, info in sorted(index.items()):
        title = info.get("title", "Untitled")
        folder = info.get("folder", "root")
        keywords = info.get("keywords", [])[:8]
        links = info.get("links_to", [])[:5]
        summary = info.get("summary", "")[:120]

        line = f"- [[{title}]] ({folder})"
        if keywords:
            line += f" | topics: {', '.join(keywords)}"
        if links:
            line += f" | links: {', '.join(f'[[{l}]]' for l in links)}"
        if summary:
            line += f" | {summary}"

        if char_count + len(line) > max_chars:
            remaining = len(index) - len(lines) + 1
            lines.append(f"... and {remaining} more notes (check _index.md for full list)")
            break
        lines.append(line)
        char_count += len(line)

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# CLAUDE CODE INTERFACE
# ──────────────────────────────────────────────────────────────

def check_claude() -> bool:
    cmd = "where" if os.name == "nt" else "which"
    try:
        r = subprocess.run([cmd, "claude"], capture_output=True, text=True)
        return r.returncode == 0
    except FileNotFoundError:
        try:
            r = subprocess.run(["claude", "--version"], capture_output=True, text=True)
            return r.returncode == 0
        except FileNotFoundError:
            return False


def run_claude(prompt: str, cwd: str, timeout: int = 300) -> dict:
    """Run Claude Code in print mode. Returns {ok, output, error}."""
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        r = subprocess.run(
            ["claude", "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=timeout,
            env=env,
        )
        stdout = (r.stdout or "").strip()
        stderr = (r.stderr or "").strip()

        if r.returncode != 0 and stderr:
            return {"ok": False, "output": stdout, "error": stderr[:500]}
        return {"ok": True, "output": stdout, "error": None}
    except FileNotFoundError:
        return {"ok": False, "output": "", "error": "Claude Code CLI not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": f"Timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "output": "", "error": str(e)[:500]}


def parse_json_response(text: str):
    """Try to extract JSON from Claude's response."""
    cleaned = re.sub(r"```json\s*", "", text)
    cleaned = re.sub(r"```\s*", "", cleaned)
    # Try to find JSON array or object
    for pattern in [r'\[.*\]', r'\{.*\}']:
        m = re.search(pattern, cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return json.loads(cleaned)  # last attempt, may raise


def sanitize_filename(name: str) -> str:
    """
    Make a string safe for use as a filename on Windows/Mac/Linux.
    Preserves readability while replacing illegal characters.
    """
    # Map common problematic characters to readable alternatives
    replacements = {
        "#": "Sharp",     # C# → CSharp
        "/": " - ",       # I/O → I - O
        "\\": " - ",
        ":": " -",
        "*": "",
        "?": "",
        '"': "'",
        "<": "",
        ">": "",
        "|": " - ",
    }

    for char, replacement in replacements.items():
        name = name.replace(char, replacement)

    # Special case: "C Sharp" back to "CSharp" (no space)
    name = re.sub(r'\bC\s*Sharp\b', 'CSharp', name)

    # Collapse multiple spaces/dashes
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'\s*-\s*-\s*', ' - ', name)

    # Remove leading/trailing dots and spaces (Windows issue)
    name = name.strip('. ')

    # Ensure it ends with .md if not already
    if not name.endswith('.md'):
        name = name + '.md'

    return name


def clean_note_content(text: str) -> str:
    """
    Strip markdown code fences that Claude wraps around note output.
    Ensures the note is raw markdown Obsidian can render, not a code block.
    """
    text = text.strip()

    # Remove ```markdown ... ``` or ```md ... ``` or ``` ... ```
    pattern = r'^```(?:markdown|md)?\s*\n(.*?)```\s*$'
    m = re.match(pattern, text, re.DOTALL)
    if m:
        text = m.group(1).strip()
        return text

    # Handle if just the opening fence is present
    if text.startswith("```markdown"):
        text = text[len("```markdown"):].strip()
    elif text.startswith("```md"):
        text = text[len("```md"):].strip()
    elif text.startswith("```") and not text.startswith("```\n---"):
        # Only strip if it looks like a language fence, not content
        first_line = text.split("\n")[0]
        if len(first_line) < 20:  # just ```lang
            text = text[len(first_line):].strip()

    # Remove trailing ``` if present
    if text.endswith("```"):
        text = text[:-3].strip()

    return text


# ──────────────────────────────────────────────────────────────
# API ROUTES
# ──────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    vault = get_vault()
    return jsonify({
        "claude_installed": check_claude(),
        "vault_exists": vault is not None,
        "vault_path": str(vault) if vault else None,
    })


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def update_config():
    data = request.json
    cfg = load_config()
    cfg.update(data)
    save_config(cfg)
    # Re-resolve vault
    cfg = load_config()
    vault = get_vault()
    if vault:
        ensure_vault_structure(vault, cfg)
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/state")
def get_state():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault configured"}), 400
    return jsonify(load_state(vault))


@app.route("/api/vault/stats")
def vault_stats():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault configured"}), 400

    total = 0
    needs_review = 0
    ai_generated = 0
    folders = {}

    skip_dirs = {".claude", ".obsidian", ".git", ".trash", "node_modules"}

    for md in vault.rglob("*.md"):
        # Skip non-files
        if not md.is_file():
            continue
        # Skip hidden files
        if md.name.startswith("."):
            continue
        # Skip CLAUDE.md at root
        if md.name == "CLAUDE.md" and md.parent == vault:
            continue
        # Skip index files
        if md.name in ("_index.json", "_index.md"):
            continue
        # Skip files inside hidden/system directories
        rel_parts = md.relative_to(vault).parts
        if any(part.startswith(".") or part in skip_dirs for part in rel_parts[:-1]):
            continue

        total += 1
        folder = rel_parts[0] if len(rel_parts) > 1 else "root"
        folders[folder] = folders.get(folder, 0) + 1
        try:
            head = md.read_text("utf-8", errors="ignore")[:500]
            if "needs-review" in head:
                needs_review += 1
            if "ai-generated" in head:
                ai_generated += 1
        except Exception:
            pass

    return jsonify({
        "total": total,
        "needs_review": needs_review,
        "ai_generated": ai_generated,
        "folders": folders,
    })


@app.route("/api/vault/review")
def vault_review():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    notes = []
    for md in vault.rglob("*.md"):
        try:
            head = md.read_text("utf-8", errors="ignore")[:500]
            if "needs-review" in head:
                notes.append({
                    "path": str(md.relative_to(vault)),
                    "name": md.stem,
                })
        except Exception:
            pass

    return jsonify({"notes": notes})


@app.route("/api/vault/cleanup", methods=["POST"])
def vault_cleanup():
    """Find and remove notes containing rate limit messages or other garbage."""
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    garbage_phrases = [
        "hit your limit",
        "rate limit",
        "resets 5pm",
        "usage cap",
        "too many requests",
        "you've hit your",
    ]

    dry_run = request.json.get("dry_run", True) if request.json else True
    found = []
    removed = []

    for md in vault.rglob("*.md"):
        if md.name.startswith(".") or md.name == "CLAUDE.md":
            continue
        try:
            content = md.read_text("utf-8", errors="ignore")
            cl = content.lower()
            for phrase in garbage_phrases:
                if phrase in cl:
                    rel = str(md.relative_to(vault))
                    found.append({
                        "path": rel,
                        "name": md.stem,
                        "match": phrase,
                        "preview": content[:200].strip(),
                    })
                    if not dry_run:
                        md.unlink()
                        removed.append(rel)
                    break
        except Exception:
            pass

    return jsonify({
        "found": found,
        "removed": removed,
        "dry_run": dry_run,
        "count": len(found),
    })


@app.route("/api/vault/index", methods=["GET"])
def get_vault_index():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400
    index = load_index(vault)
    return jsonify({"count": len(index), "index": index})


@app.route("/api/vault/index/rebuild", methods=["POST"])
def rebuild_vault_index():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400
    index = build_index(vault)
    save_index(vault, index)
    return jsonify({"count": len(index), "rebuilt": True})


# ── PHASE 1: ANALYZE ──

@app.route("/api/analyze", methods=["POST"])
def analyze():
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault configured"}), 400

    raw_input = request.json.get("topic", "").strip()
    if not raw_input:
        return jsonify({"error": "No topic provided"}), 400

    ensure_vault_structure(vault, cfg)

    # Rebuild index before analysis so Claude knows what exists
    index = build_index(vault)
    save_index(vault, index)
    index_text = get_index_for_prompt(vault, max_chars=10000)

    # First scan with raw input to give Claude context
    scan = scan_vault(vault, raw_input)

    prompt = dedent(f"""\
    Use SKILL: topic_extract + SKILL: analyze_vault from .claude/skills/my-brain/SKILL.md.

    User input: "{raw_input}"

    {index_text}

    Vault scan:
    {json.dumps(scan, indent=2)}

    Respond with ONLY a JSON object — no fences, no explanation.
    Include "topic" field with extracted topic name.
    """)

    result = run_claude(prompt, str(vault))

    analysis = None
    if result["ok"] and result["output"]:
        try:
            analysis = parse_json_response(result["output"])
        except Exception:
            pass

    if not analysis:
        analysis = {
            "topic": raw_input,
            "knowledge_level": "none",
            "existing_coverage": [],
            "gaps": [raw_input],
            "suggested_learning_path": [f"Introduction to {raw_input}"],
            "search_queries": [f"{raw_input} explained", f"{raw_input} tutorial"],
        }

    # Use extracted topic if Claude provided one, otherwise fall back to raw input
    topic = analysis.pop("topic", raw_input).strip()

    # Re-scan with the extracted topic for better matching
    if topic.lower() != raw_input.lower():
        scan = scan_vault(vault, topic)

    # Save state
    state = load_state(vault)
    state["active_topic"] = topic
    if "topics" not in state:
        state["topics"] = {}
    state["topics"][topic] = {
        "phase": "analyzed",
        "analysis": analysis,
        "scan": scan,
        "raw_input": raw_input,
        "timestamp": datetime.now().isoformat(),
    }
    save_state(vault, state)

    return jsonify({"scan": scan, "analysis": analysis, "topic": topic, "raw_input": raw_input})


# ── PHASE 1b: SCOUT ──

@app.route("/api/scout", methods=["POST"])
def scout():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    topic = request.json.get("topic", "").strip()
    state = load_state(vault)
    topic_data = state.get("topics", {}).get(topic, {})
    gaps = topic_data.get("analysis", {}).get("gaps", [topic])
    queries = topic_data.get("analysis", {}).get("search_queries", [f"{topic} tutorial"])

    prompt = dedent(f"""\
    Use SKILL: scout_resources from .claude/skills/my-brain/SKILL.md.

    Topic: "{topic}"
    Gaps to fill: {json.dumps(gaps)}
    Suggested queries: {json.dumps(queries)}

    Respond with ONLY a JSON object — no fences, no explanation.
    """)

    result = run_claude(prompt, str(vault))

    resources = {"resources": []}
    if result["ok"] and result["output"]:
        try:
            resources = parse_json_response(result["output"])
            if isinstance(resources, list):
                resources = {"resources": resources}
        except Exception:
            pass

    # Update state
    state["topics"][topic]["resources"] = resources.get("resources", [])
    state["topics"][topic]["phase"] = "scouted"
    save_state(vault, state)

    return jsonify(resources)


# ── PHASE 2: GENERATE NOTEBOOKLM PROMPT ──

@app.route("/api/generate-prompt", methods=["POST"])
def generate_prompt():
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    topic = request.json.get("topic", "").strip()
    state = load_state(vault)
    td = state.get("topics", {}).get(topic, {})
    gaps = td.get("analysis", {}).get("gaps", [])
    existing = td.get("analysis", {}).get("existing_coverage", [])
    resources = td.get("resources", [])

    # Build URL-only block (for NotebookLM paste) and reference list (for humans)
    urls_only = "\n".join(r.get("url", "") for r in resources if r.get("url"))
    source_ref = ""
    for i, r in enumerate(resources):
        source_ref += f"  {i+1}. {r.get('title', 'Untitled')}\n     {r.get('url', '')}\n"

    prompt_text = dedent(f"""\
═══════════════════════════════════════════════════════
NOTEBOOKLM PROMPT — My Brain
Topic: {topic}
Date:  {datetime.now().strftime('%Y-%m-%d %H:%M')}
═══════════════════════════════════════════════════════

STEP 1: Copy-paste these URLs into NotebookLM
         (one per line — ready to paste as-is):

{urls_only}

STEP 2: Paste this prompt into NotebookLM:

I'm building a knowledge base on "{topic}".

ALREADY KNOW (skip):
{chr(10).join(f'  - {x}' for x in existing) if existing else '  - Starting from scratch'}

GAPS TO FILL (focus here):
{chr(10).join(f'  - {g}' for g in gaps)}

Please:
1. For each gap, provide a clear structured explanation
2. Cross-reference across sources
3. Flag contradictions
4. Highlight key concepts and relationships
5. Note established fact vs. debated
6. Organize by subtopic with headers

Be thorough — this becomes individual Obsidian notes.

═══════════════════════════════════════════════════════
SOURCE REFERENCE (what each URL is):

{source_ref}
═══════════════════════════════════════════════════════
    """)

    # Save to vault
    prompt_folder = vault / cfg.get("prompts_folder", "03 - NotebookLM Prompts")
    prompt_folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^\w\s-]', '', topic).strip().replace(' ', '-')
    pf = prompt_folder / f"{datetime.now().strftime('%Y-%m-%d')}_{safe}.md"
    pf.write_text(prompt_text, encoding="utf-8")

    # Update state
    state["topics"][topic]["phase"] = "awaiting_notebooklm"
    state["topics"][topic]["prompt_file"] = str(pf.relative_to(vault))
    save_state(vault, state)

    return jsonify({
        "prompt": prompt_text,
        "urls": urls_only,
        "saved_to": str(pf.relative_to(vault)),
    })


# ── PHASE 3: INGEST + VAULT ──

@app.route("/api/staging/list")
def staging_list():
    """List files in the staging folder."""
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    staging = vault / cfg.get("staging_folder", "04 - Staging")
    staging.mkdir(parents=True, exist_ok=True)

    files = []
    for f in sorted(staging.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
    return jsonify({"folder": str(staging), "files": files})


@app.route("/api/staging/upload", methods=["POST"])
def staging_upload():
    """Upload files to staging folder."""
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    staging = vault / cfg.get("staging_folder", "04 - Staging")
    staging.mkdir(parents=True, exist_ok=True)

    uploaded = []
    for f in request.files.getlist("files"):
        dest = staging / f.filename
        f.save(str(dest))
        uploaded.append(f.filename)

    return jsonify({"uploaded": uploaded})


@app.route("/api/staging/paste", methods=["POST"])
def staging_paste():
    """Save pasted text as a staging file."""
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    staging = vault / cfg.get("staging_folder", "04 - Staging")
    staging.mkdir(parents=True, exist_ok=True)

    text = request.json.get("text", "")
    name = request.json.get("name", f"paste_{datetime.now().strftime('%H%M%S')}.md")

    dest = staging / name
    dest.write_text(text, encoding="utf-8")

    return jsonify({"saved": name, "size": len(text)})


@app.route("/api/ingest", methods=["POST"])
def ingest():
    """
    Phase 3: Read all staging files, plan notes, write one at a time.
    Returns progress via the response.
    """
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    topic = request.json.get("topic", "").strip()
    state = load_state(vault)
    if not topic:
        topic = state.get("active_topic", "Research")

    staging = vault / cfg.get("staging_folder", "04 - Staging")
    inbox = cfg.get("notes_folder", "00 - Inbox")
    resources_folder = cfg.get("resources_folder", "02 - Resources")
    default_tags = cfg.get("default_tags", ["ai-generated", "needs-review"])

    # Collect all staging content
    content_parts = []
    for f in sorted(staging.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            try:
                content_parts.append(f"--- Source: {f.name} ---\n{f.read_text('utf-8', errors='ignore')}")
            except Exception:
                pass

    if not content_parts:
        return jsonify({"error": "No files in staging folder"}), 400

    content = "\n\n".join(content_parts)

    # Load state for context
    state = load_state(vault)
    td = state.get("topics", {}).get(topic, {})
    gaps = td.get("analysis", {}).get("gaps", [])
    resources = td.get("resources", [])

    results = {"notes": [], "errors": []}

    # Build fresh index so Claude knows what already exists
    index = build_index(vault)
    save_index(vault, index)
    index_text = get_index_for_prompt(vault, max_chars=10000)

    # ── 3a: Plan notes ──
    plan_prompt = dedent(f"""\
    Use SKILL: plan_notes from .claude/skills/my-brain/SKILL.md.

    Topic: "{topic}"
    Gaps to address: {json.dumps(gaps)}

    {index_text}

    IMPORTANT:
    - Do NOT create notes for topics that already exist in the index above.
    - DO link new notes to existing ones where relevant.
    - If a topic partially overlaps with an existing note, name the new note
      to cover only the NEW aspect and link to the existing note.

    Research content:
    ---
    {content[:15000]}
    ---

    Respond with ONLY a JSON array — no fences, no explanation.
    """)

    plan_result = run_claude(plan_prompt, str(vault))
    note_plan = None
    if plan_result["ok"] and plan_result["output"]:
        try:
            note_plan = parse_json_response(plan_result["output"])
        except Exception:
            pass

    if not note_plan or not isinstance(note_plan, list):
        note_plan = [
            {"filename": f"{topic} Overview.md", "title": f"{topic} Overview",
             "summary": "General overview", "links_to": []}
        ]

    all_titles = [n["title"] for n in note_plan]

    # ── 3b: Write each note ──
    written = 0
    content_len = len(content)
    batch_written = []  # Track what's been written in this batch

    for i, ni in enumerate(note_plan):
        title = ni["title"]
        filename = sanitize_filename(ni.get("filename", f"{title}.md"))
        summary = ni.get("summary", "")
        others = ", ".join(f"[[{t}]]" for t in all_titles if t != title)

        # Build context of what's already been written in THIS batch
        batch_context = ""
        if batch_written:
            batch_lines = []
            for bw in batch_written:
                batch_lines.append(f"- [[{bw['title']}]]: {bw['summary']}")
            batch_context = (
                "ALREADY WRITTEN IN THIS BATCH (do NOT duplicate their content):\n"
                + "\n".join(batch_lines)
            )

        # Build list of notes still to be written
        pending = [n for n in note_plan[i+1:]]
        pending_context = ""
        if pending:
            pending_lines = [f"- [[{p['title']}]]: {p['summary']}" for p in pending]
            pending_context = (
                "WILL BE WRITTEN NEXT (don't cover their topics either):\n"
                + "\n".join(pending_lines)
            )

        # Extract the most relevant section of content for this note.
        # Search for the title/keywords in the content and grab surrounding context.
        title_lower = title.lower()
        keywords = [w for w in title_lower.split() if len(w) > 3]

        # Score each paragraph by relevance to this note
        paragraphs = content.split("\n\n")
        scored = []
        for pi, para in enumerate(paragraphs):
            pl = para.lower()
            score = 0
            if title_lower in pl:
                score += 10
            for kw in keywords:
                if kw in pl:
                    score += 2
            scored.append((score, pi, para))

        # Sort by relevance, take top paragraphs up to ~12K chars
        scored.sort(key=lambda x: (-x[0], x[1]))
        relevant_parts = []
        char_count = 0
        for score, pi, para in scored:
            if score == 0 and char_count > 4000:
                break
            relevant_parts.append((pi, para))
            char_count += len(para)
            if char_count > 12000:
                break

        # Re-sort by original order to maintain coherence
        relevant_parts.sort(key=lambda x: x[0])
        relevant_content = "\n\n".join(p for _, p in relevant_parts)

        # If we couldn't find relevant content, fall back to the full blob
        if len(relevant_content) < 200:
            relevant_content = content[:15000]

        # Compact index for linking (shorter than planning version)
        index_compact = get_index_for_prompt(vault, max_chars=6000)

        write_prompt = dedent(f"""\
        Use SKILL: write_note from .claude/skills/my-brain/SKILL.md.
        Use .claude/skills/obsidian-markdown/SKILL.md for proper Obsidian syntax.

        Write a COMPREHENSIVE note — capture the full depth, all specific details,
        examples, numbers, and nuance from the source material. This note should be
        a complete reference someone can return to.

        Title: "{title}"
        Topic: "{topic}"
        Covers: {summary}
        Created: {datetime.now().strftime('%Y-%m-%d')}
        Tags: {json.dumps(default_tags)}

        {index_compact}

        {batch_context}

        {pending_context}

        CRITICAL — SCOPE & DEDUPLICATION:
        1. This note is ONLY about "{title}" — specifically: {summary}
        2. Do NOT include sections that belong in other notes (written or pending above).
           If a topic has its own note, write "see [[Note Title]]" and move on.
        3. For example: if this note is about Control Flow and [[CSharp Operators]] exists
           or is being written, do NOT include an Operators section — just link to it.
        4. Link to existing vault notes, already-written batch notes, and pending notes freely.
        5. Stay focused on YOUR scope. It's better to link generously than to duplicate.

        Source research (extract everything relevant to "{title}"):
        ---
        {relevant_content}
        ---

        Output ONLY the raw markdown note starting with --- frontmatter. No fences.
        """)

        r = run_claude(write_prompt, str(vault), timeout=180)

        if r["ok"] and r["output"]:
            note_content = clean_note_content(r["output"])

            # Validate — reject garbage content (rate limits, errors, etc.)
            # Only check the first 300 chars for rejection phrases — these phrases
            # can legitimately appear in note body (e.g. "try again" in exception handling)
            header = note_content[:300].lower()
            rejection_phrases = [
                "hit your limit",
                "rate limit",
                "resets 5pm",
                "resets at",
                "usage cap",
                "too many requests",
                "as an ai language model",
                "as an ai assistant",
                "i'm unable to",
            ]
            is_garbage = (
                len(note_content) < 100
                or not note_content.lstrip().startswith("---")
                or any(phrase in header for phrase in rejection_phrases)
            )

            if is_garbage:
                results["errors"].append({
                    "title": title,
                    "error": f"Invalid content (rate limit or bad response): {note_content[:100]}..."
                })
            else:
                p = vault / inbox / filename
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(note_content, encoding="utf-8")
                results["notes"].append({"title": title, "file": str(p.relative_to(vault))})
                written += 1
                batch_written.append({"title": title, "summary": summary})
        else:
            results["errors"].append({"title": title, "error": r.get("error", "Empty response")})

    # ── 3c: MOC ──
    moc_lines = [
        "---", f'title: "{topic} MOC"',
        f"created: {datetime.now().strftime('%Y-%m-%d')}",
        f"tags: [{', '.join(default_tags)}, MOC]",
        "status: draft", "---", "",
        f"# {topic} — Map of Content", "", "## Notes", "",
    ]
    for n in note_plan:
        moc_lines.append(f"- [[{n['title']}]] — {n['summary']}")
    moc_lines += ["", "## Resources", ""]
    for r in resources[:10]:
        moc_lines.append(f"- [{r.get('title', 'Link')}]({r.get('url', '')})")
    moc_lines += ["", "## Gaps", ""]
    for g in gaps:
        moc_lines.append(f"- [ ] {g}")

    moc_path = vault / inbox / sanitize_filename(f"{topic} MOC.md")
    moc_path.write_text("\n".join(moc_lines), encoding="utf-8")

    # ── 3d: Resources note ──
    res_lines = [
        "---", f'title: "{topic} Resources"',
        f"created: {datetime.now().strftime('%Y-%m-%d')}",
        f"tags: [{', '.join(default_tags)}, resources]",
        "status: draft", "---", "",
        f"# {topic} — Resources", "",
    ]
    for r in resources:
        icon = {"youtube": "🎬", "article": "📄", "documentation": "📚", "paper": "📑"}.get(r.get("type", ""), "🔗")
        res_lines.append(f"{icon} [{r.get('title', '')}]({r.get('url', '')})")
        res_lines.append(f"   Covers: {r.get('covers', 'general')}")
        res_lines.append("")

    rp = vault / resources_folder / sanitize_filename(f"{topic} Resources.md")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("\n".join(res_lines), encoding="utf-8")

    # Clean staging
    for f in staging.iterdir():
        if f.is_file() and not f.name.startswith("."):
            f.unlink()

    # Rebuild index with new notes included
    updated_index = build_index(vault)
    save_index(vault, updated_index)

    # Update state
    if "topics" not in state:
        state["topics"] = {}
    if topic not in state["topics"]:
        state["topics"][topic] = {"timestamp": datetime.now().isoformat()}
    state["topics"][topic]["phase"] = "complete"
    state["topics"][topic]["notes_written"] = written
    state["active_topic"] = topic
    save_state(vault, state)

    results["written"] = written
    results["moc"] = str(moc_path.relative_to(vault))
    return jsonify(results)


# ──────────────────────────────────────────────────────────────
# SERVE STATIC
# ──────────────────────────────────────────────────────────────

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(str(STATIC_DIR), path)


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser

    print("""
    ╔═══════════════════════════════════════╗
    ║  MY BRAIN                             ║
    ║  Learn anything. Own everything.      ║
    ╚═══════════════════════════════════════╝
    """)

    cfg = load_config()
    vault = get_vault()
    if vault:
        print(f"  Vault: {vault}")
        ensure_vault_structure(vault, cfg)
    else:
        print("  ⚠ No vault configured — set it in the UI")

    print(f"  Static: {STATIC_DIR}")
    print(f"  Static exists: {STATIC_DIR.exists()}")
    print(f"  index.html exists: {(STATIC_DIR / 'index.html').exists()}")
    print(f"  Claude CLI: {'✓ Found' if check_claude() else '✗ Not found'}")
    print(f"\n  → http://localhost:5000\n")

    webbrowser.open("http://localhost:5000")
    app.run(debug=False, port=5000)