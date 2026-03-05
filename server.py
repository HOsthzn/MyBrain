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
import hashlib
import threading
import time
from pathlib import Path
from datetime import datetime
from textwrap import dedent
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context

# Embeddings — optional, degrades gracefully if packages not installed
try:
    from embeddings import get_embeddings, is_available as embeddings_available, EmbeddingsUnavailable
except ImportError:
    def get_embeddings(_vault): return None
    def embeddings_available(): return False
    class EmbeddingsUnavailable(RuntimeError): pass

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
    vp = cfg.get("vault_path", "")
    if vp:
        cfg["_vault"] = str(Path(vp).expanduser().resolve())
    return cfg


def save_config(cfg: dict):
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
        cfg.get("learning_folder", "05 - Learning"),
    ]
    for f in folders:
        (vault / f).mkdir(parents=True, exist_ok=True)

    cm = vault / "CLAUDE.md"
    if CLAUDE_MD_SRC.exists():
        if not cm.exists() or CLAUDE_MD_SRC.stat().st_mtime > cm.stat().st_mtime:
            shutil.copy2(CLAUDE_MD_SRC, cm)

    src_skills = SCRIPT_DIR / ".claude"
    dst_skills = vault / ".claude"
    if src_skills.exists():
        if dst_skills.exists():
            shutil.rmtree(dst_skills)
        shutil.copytree(src_skills, dst_skills)


def validate_config() -> list[str]:
    """
    Check config for common issues. Returns list of warning strings.
    Printed on startup and surfaced via /api/health.
    """
    warnings = []
    cfg = load_config()

    if not cfg.get("vault_path"):
        warnings.append("vault_path not set — configure it in the UI (⚙ Config tab)")
        return warnings

    vault = get_vault()
    if not vault:
        warnings.append(f"vault_path '{cfg.get('vault_path')}' does not exist on disk")
        return warnings

    for key, default in [
        ("notes_folder", "00 - Inbox"),
        ("staging_folder", "04 - Staging"),
        ("knowledge_folder", "01 - Knowledge"),
    ]:
        folder = vault / cfg.get(key, default)
        if not folder.exists():
            warnings.append(f"Folder missing: {folder.name} — will be created on first use")

    if not check_claude():
        warnings.append("Claude CLI not found — install with: npm i -g @anthropic-ai/claude-code")

    cm = vault / "CLAUDE.md"
    if CLAUDE_MD_SRC.exists() and cm.exists():
        if CLAUDE_MD_SRC.stat().st_mtime > cm.stat().st_mtime:
            warnings.append("CLAUDE.md in vault is outdated — will sync on next vault use")

    return warnings


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
    existing = {}
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
        kw_score = sum(3 if kw in md.stem.lower() else 0 for kw in keywords)
        kw_score += sum(1 if kw in cl else 0 for kw in keywords)

        if kw_score > 0:
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
            rel_path = str(md.relative_to(vault))
            existing[rel_path] = {
                "path": rel_path,
                "name": md.stem,
                "relevance": float(kw_score),
                "semantic_score": 0.0,
                "tags": note_tags,
                "links": links[:10],
                "size": len(content),
            }

    emb = get_embeddings(vault)
    semantic_used = False
    if emb:
        try:
            sem_results = emb.search(topic, n=20)
            semantic_used = True
            for r in sem_results:
                p = r["path"]
                bonus = r["score"] * 10.0
                if p in existing:
                    existing[p]["relevance"] += bonus
                    existing[p]["semantic_score"] = r["score"]
                else:
                    md_path = vault / p
                    if md_path.exists():
                        try:
                            content = md_path.read_text("utf-8", errors="ignore")
                            links = re.findall(r"\[\[([^\]]+)\]\]", content)
                            existing[p] = {
                                "path": p,
                                "name": md_path.stem,
                                "relevance": bonus,
                                "semantic_score": r["score"],
                                "tags": [],
                                "links": links[:10],
                                "size": len(content),
                            }
                        except Exception:
                            pass
        except Exception as e:
            print(f"  [embeddings] Semantic search failed: {e}")

    results = sorted(existing.values(), key=lambda x: x["relevance"], reverse=True)
    return {
        "topic": topic,
        "total_notes": total,
        "related_count": len(results),
        "related": results[:20],
        "tags": sorted(tags),
        "semantic_search_used": semantic_used,
    }


# ──────────────────────────────────────────────────────────────
# VAULT INDEX
# ──────────────────────────────────────────────────────────────

INDEX_FILE = "_index.json"
_SKIP_DIRS = {".claude", ".obsidian", ".git", ".trash", "node_modules"}


def build_index(vault: Path) -> dict:
    index = {}

    for md in vault.rglob("*.md"):
        if not md.is_file() or md.name.startswith(".") or md.name == "CLAUDE.md":
            continue
        rel_parts = md.relative_to(vault).parts
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if md.name in ("_index.json", "_index.md"):
            continue

        rel_path = str(md.relative_to(vault))
        try:
            content = md.read_text("utf-8", errors="ignore")
        except Exception:
            continue

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

        links = list(set(re.findall(r"\[\[([^\]|]+)", content)))
        headings = re.findall(r"^#{1,3}\s+(.+)$", content, re.MULTILINE)
        keywords = [h.strip().strip("#").strip() for h in headings if len(h.strip()) > 2]
        keywords = [k for k in keywords if k.lower() != title.lower()]

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
    p = vault / INDEX_FILE
    p.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

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
    p = vault / INDEX_FILE
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {}


def get_index_for_prompt(vault: Path, max_chars: int = 12000) -> str:
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


def run_claude(prompt: str, cwd: str, timeout: int = 300, retries: int = 2) -> dict:
    """
    Run Claude Code in print mode. Returns {ok, output, error}.
    Retries up to `retries` times on rate-limit errors with 30s back-off.
    """
    rate_limit_phrases = ["rate limit", "too many requests", "usage limit", "overloaded"]

    for attempt in range(1, retries + 1):
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
                stderr_lower = stderr.lower()
                if attempt < retries and any(p in stderr_lower for p in rate_limit_phrases):
                    print(f"  [claude] Rate limit hit (attempt {attempt}/{retries}), waiting 30s...")
                    time.sleep(30)
                    continue
                return {"ok": False, "output": stdout, "error": stderr[:500]}

            return {"ok": True, "output": stdout, "error": None}

        except FileNotFoundError:
            return {"ok": False, "output": "", "error": "Claude Code CLI not found"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": "", "error": f"Timed out after {timeout}s"}
        except Exception as e:
            return {"ok": False, "output": "", "error": str(e)[:500]}

    return {"ok": False, "output": "", "error": "Max retries reached"}


def parse_json_response(text: str):
    """Try to extract JSON from Claude's response."""
    cleaned = re.sub(r"```json\s*", "", text)
    cleaned = re.sub(r"```\s*", "", cleaned)
    for pattern in [r'\[.*\]', r'\{.*\}']:
        m = re.search(pattern, cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return json.loads(cleaned)


def sanitize_filename(name: str) -> str:
    replacements = {
        "#": "Sharp", "/": " - ", "\\": " - ", ":": " -",
        "*": "", "?": "", '"': "'", "<": "", ">": "", "|": " - ",
    }
    for char, replacement in replacements.items():
        name = name.replace(char, replacement)
    name = re.sub(r'\bC\s*Sharp\b', 'CSharp', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'\s*-\s*-\s*', ' - ', name)
    name = name.strip('. ')
    if not name.endswith('.md'):
        name = name + '.md'
    return name


def clean_note_content(text: str) -> str:
    text = text.strip()
    pattern = r'^```(?:markdown|md)?\s*\n(.*?)```\s*$'
    m = re.match(pattern, text, re.DOTALL)
    if m:
        return m.group(1).strip()
    if text.startswith("```markdown"):
        text = text[len("```markdown"):].strip()
    elif text.startswith("```md"):
        text = text[len("```md"):].strip()
    elif text.startswith("```") and not text.startswith("```\n---"):
        first_line = text.split("\n")[0]
        if len(first_line) < 20:
            text = text[len(first_line):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def score_note_quality(content: str) -> dict:
    """
    Heuristic quality score for a generated note (0-100, no extra Claude call).
    Checks word count, wikilinks, section headers, callouts, and code examples.
    """
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, flags=re.DOTALL)
    word_count = len(body.split())

    if word_count >= 400:
        depth = 25
    elif word_count >= 200:
        depth = 15
    else:
        depth = 5

    wikilinks = len(re.findall(r"\[\[", content))
    linking = min(20, wikilinks * 5)

    headers = len(re.findall(r"^#{2,3} ", content, re.MULTILINE))
    structure = min(20, headers * 5)

    callouts = len(re.findall(r"> \[!", content))
    callout_score = min(15, callouts * 5)

    code_blocks = len(re.findall(r"```", content)) // 2
    inline_code = len(re.findall(r"`[^`]+`", content))
    examples = min(20, (code_blocks * 5) + (inline_code * 2))

    total = depth + linking + structure + callout_score + examples
    grade = "A" if total >= 80 else "B" if total >= 60 else "C" if total >= 40 else "D"

    return {
        "total": total,
        "grade": grade,
        "breakdown": {
            "depth": depth,
            "linking": linking,
            "structure": structure,
            "callouts": callout_score,
            "examples": examples,
        },
        "word_count": word_count,
        "wikilinks": wikilinks,
    }


def extract_relevant_content(content: str, title: str, max_chars: int = 12000) -> str:
    """
    Extract content most relevant to a note title from a multi-source research blob.

    Strategy:
    1. Split by '--- Source: ... ---' boundaries
    2. Score each whole source file for relevance to the title
    3. Take top sources first, fill remaining budget with the most relevant
       paragraphs from the next source
    """
    title_lower = title.lower()
    keywords = [w for w in title_lower.split() if len(w) > 3]

    source_pattern = re.compile(r'--- Source: .+? ---\n', re.MULTILINE)
    parts = source_pattern.split(content)
    headers = source_pattern.findall(content)

    sources = []
    if parts[0].strip():
        sources.append(("", parts[0]))
    for h, p in zip(headers, parts[1:]):
        sources.append((h, p))

    scored_sources = []
    for header, body in sources:
        body_lower = body.lower()
        score = (20 if title_lower in body_lower else 0)
        score += sum(body_lower.count(kw) * 2 for kw in keywords)
        scored_sources.append((score, header, body))

    scored_sources.sort(key=lambda x: -x[0])

    result_parts = []
    char_count = 0
    for score, header, body in scored_sources:
        chunk = header + body
        if char_count + len(chunk) <= max_chars:
            result_parts.append(header + body)
            char_count += len(chunk)
        else:
            # Take the most relevant paragraphs from this source to fill budget
            remaining = max_chars - char_count
            if remaining > 200:
                paragraphs = body.split("\n\n")
                para_scored = []
                for pi, para in enumerate(paragraphs):
                    pl = para.lower()
                    ps = (20 if title_lower in pl else 0) + sum(2 for kw in keywords if kw in pl)
                    para_scored.append((ps, pi, para))
                para_scored.sort(key=lambda x: (-x[0], x[1]))
                snippet_parts = []
                used = 0
                for _, _, para in para_scored:
                    if used + len(para) > remaining:
                        break
                    snippet_parts.append(para)
                    used += len(para)
                if snippet_parts:
                    result_parts.append(header + "\n\n".join(snippet_parts))
            break

    return "\n\n".join(result_parts) if result_parts else content[:max_chars]


# ──────────────────────────────────────────────────────────────
# STAGING HELPERS
# ──────────────────────────────────────────────────────────────

def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def staging_checksums(staging: Path) -> dict:
    p = staging / ".staging_checksums.json"
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {}


def save_staging_checksums(staging: Path, checksums: dict):
    p = staging / ".staging_checksums.json"
    p.write_text(json.dumps(checksums), encoding="utf-8")


# ──────────────────────────────────────────────────────────────
# SSE HELPER
# ──────────────────────────────────────────────────────────────

def sse_event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ──────────────────────────────────────────────────────────────
# API ROUTES
# ──────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    vault = get_vault()
    emb_stats = None
    if vault:
        emb = get_embeddings(vault)
        if emb:
            try:
                emb_stats = emb.stats()
            except Exception:
                pass

    return jsonify({
        "claude_installed": check_claude(),
        "vault_exists": vault is not None,
        "vault_path": str(vault) if vault else None,
        "embeddings_available": embeddings_available(),
        "embeddings": emb_stats,
        "warnings": validate_config(),
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

    for md in vault.rglob("*.md"):
        if not md.is_file() or md.name.startswith("."):
            continue
        if md.name == "CLAUDE.md" and md.parent == vault:
            continue
        if md.name in ("_index.json", "_index.md"):
            continue
        rel_parts = md.relative_to(vault).parts
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel_parts[:-1]):
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
        # Same guards as vault_stats — skip system/hidden files
        if not md.is_file() or md.name.startswith("."):
            continue
        if md.name == "CLAUDE.md" and md.parent == vault:
            continue
        if md.name in ("_index.json", "_index.md"):
            continue
        rel_parts = md.relative_to(vault).parts
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel_parts[:-1]):
            continue
        try:
            head = md.read_text("utf-8", errors="ignore")[:500]
            if "needs-review" in head:
                notes.append({"path": str(md.relative_to(vault)), "name": md.stem})
        except Exception:
            pass

    return jsonify({"notes": notes})


@app.route("/api/vault/cleanup", methods=["POST"])
def vault_cleanup():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    garbage_phrases = [
        "hit your limit", "rate limit", "resets 5pm",
        "usage cap", "too many requests", "you've hit your",
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
                        "path": rel, "name": md.stem,
                        "match": phrase, "preview": content[:200].strip(),
                    })
                    if not dry_run:
                        md.unlink()
                        removed.append(rel)
                    break
        except Exception:
            pass

    return jsonify({"found": found, "removed": removed, "dry_run": dry_run, "count": len(found)})


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

    embed_stats = None
    emb = get_embeddings(vault)
    if emb:
        try:
            embed_stats = emb.sync_with_vault(index)
        except Exception as e:
            embed_stats = {"error": str(e)}

    return jsonify({"count": len(index), "rebuilt": True, "embeddings": embed_stats})


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

    index = build_index(vault)
    save_index(vault, index)
    index_text = get_index_for_prompt(vault, max_chars=10000)
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

    topic = analysis.pop("topic", raw_input).strip()

    if topic.lower() != raw_input.lower():
        scan = scan_vault(vault, topic)

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

    prompt_folder = vault / cfg.get("prompts_folder", "03 - NotebookLM Prompts")
    prompt_folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^\w\s-]', '', topic).strip().replace(' ', '-')
    pf = prompt_folder / f"{datetime.now().strftime('%Y-%m-%d')}_{safe}.md"
    pf.write_text(prompt_text, encoding="utf-8")

    state["topics"][topic]["phase"] = "awaiting_notebooklm"
    state["topics"][topic]["prompt_file"] = str(pf.relative_to(vault))
    save_state(vault, state)

    return jsonify({"prompt": prompt_text, "urls": urls_only, "saved_to": str(pf.relative_to(vault))})


# ── PHASE 3: STAGING ──

@app.route("/api/staging/list")
def staging_list():
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
    """Upload files to staging. Skips duplicates by MD5 checksum."""
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    staging = vault / cfg.get("staging_folder", "04 - Staging")
    staging.mkdir(parents=True, exist_ok=True)
    checksums = staging_checksums(staging)

    uploaded = []
    skipped = []

    for f in request.files.getlist("files"):
        dest = staging / f.filename
        tmp = staging / f".tmp_{f.filename}"
        f.save(str(tmp))

        new_md5 = file_md5(tmp)
        if new_md5 in checksums.values():
            tmp.unlink()
            skipped.append({"name": f.filename, "reason": "duplicate"})
        else:
            tmp.rename(dest)
            checksums[f.filename] = new_md5
            uploaded.append(f.filename)

    save_staging_checksums(staging, checksums)
    return jsonify({"uploaded": uploaded, "skipped": skipped})


@app.route("/api/staging/paste", methods=["POST"])
def staging_paste():
    """Save pasted text as a staging file. Skips duplicates by MD5."""
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    staging = vault / cfg.get("staging_folder", "04 - Staging")
    staging.mkdir(parents=True, exist_ok=True)

    text = request.json.get("text", "")
    name = request.json.get("name", f"paste_{datetime.now().strftime('%H%M%S')}.md")

    checksums = staging_checksums(staging)
    new_md5 = hashlib.md5(text.encode("utf-8")).hexdigest()

    if new_md5 in checksums.values():
        return jsonify({"skipped": True, "reason": "duplicate content already in staging"})

    dest = staging / name
    dest.write_text(text, encoding="utf-8")
    checksums[name] = new_md5
    save_staging_checksums(staging, checksums)

    return jsonify({"saved": name, "size": len(text)})


# ── PHASE 3a: PLAN NOTES ──

@app.route("/api/ingest/plan", methods=["POST"])
def ingest_plan():
    """
    Plan which notes to create from staged research.
    Returns the plan for human review. Saves to state for /api/ingest/write.
    Accepts optional 'topic' in body; falls back to active_topic from state.
    """
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    topic = (request.json or {}).get("topic", "").strip()
    state = load_state(vault)
    if not topic:
        topic = state.get("active_topic", "Research")

    staging = vault / cfg.get("staging_folder", "04 - Staging")

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
    td = state.get("topics", {}).get(topic, {})
    gaps = td.get("analysis", {}).get("gaps", [])

    index = build_index(vault)
    save_index(vault, index)
    index_text = get_index_for_prompt(vault, max_chars=10000)

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

    if "topics" not in state:
        state["topics"] = {}
    if topic not in state["topics"]:
        state["topics"][topic] = {}
    state["topics"][topic]["note_plan"] = note_plan
    state["topics"][topic]["phase"] = "planned"
    state["active_topic"] = topic
    save_state(vault, state)

    return jsonify({"topic": topic, "plan": note_plan, "count": len(note_plan)})


# ── PHASE 3b: WRITE NOTES — SSE streaming ──

@app.route("/api/ingest/write", methods=["POST"])
def ingest_write():
    """
    Write notes from the saved plan. Streams progress via SSE.
    Skips notes that already exist on disk (resumable).
    Accepts optional 'plan_override' to use an edited plan from the UI.
    """
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    body = request.json or {}
    topic = body.get("topic", "").strip()
    plan_override = body.get("plan_override")

    state = load_state(vault)
    if not topic:
        topic = state.get("active_topic", "Research")

    td = state.get("topics", {}).get(topic, {})
    note_plan = plan_override or td.get("note_plan")

    if not note_plan:
        return jsonify({"error": "No plan found — run /api/ingest/plan first"}), 400

    staging = vault / cfg.get("staging_folder", "04 - Staging")
    inbox = cfg.get("notes_folder", "00 - Inbox")
    resources_folder = cfg.get("resources_folder", "02 - Resources")
    default_tags = cfg.get("default_tags", ["ai-generated", "needs-review"])
    gaps = td.get("analysis", {}).get("gaps", [])
    resources = td.get("resources", [])

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
    all_titles = [n["title"] for n in note_plan]

    def generate():
        written = 0
        errors = []
        notes_written = []

        yield sse_event({"type": "start", "total": len(note_plan), "topic": topic})

        index_compact = get_index_for_prompt(vault, max_chars=6000)

        for i, ni in enumerate(note_plan):
            title = ni["title"]
            filename = sanitize_filename(ni.get("filename", f"{title}.md"))
            summary = ni.get("summary", "")
            others = ", ".join(f"[[{t}]]" for t in all_titles if t != title)

            dest_path = vault / inbox / filename

            # Resumable: skip if note already exists
            if dest_path.exists():
                yield sse_event({
                    "type": "skip",
                    "index": i + 1, "total": len(note_plan),
                    "title": title, "reason": "already exists",
                })
                notes_written.append({
                    "title": title,
                    "file": str(dest_path.relative_to(vault)),
                    "skipped": True,
                })
                continue

            yield sse_event({
                "type": "progress",
                "index": i + 1, "total": len(note_plan),
                "title": title, "status": "writing",
            })

            relevant_content = extract_relevant_content(content, title, max_chars=12000)

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
            Other notes in this batch: {others}

            {index_compact}

            CRITICAL — LINKING & DEDUPLICATION:
            1. Use [[wikilinks]] to link to EXISTING notes whenever you mention a related concept.
            2. Also link to the other notes in this batch.
            3. Do NOT re-explain concepts that already have their own note — just link.
            4. Focus ONLY on what's new if this overlaps with an existing note.

            Source research (extract everything relevant to "{title}"):
            ---
            {relevant_content}
            ---

            Output ONLY the raw markdown note starting with --- frontmatter. No fences.
            """)

            r = run_claude(write_prompt, str(vault), timeout=180)

            if r["ok"] and r["output"]:
                note_content = clean_note_content(r["output"])

                rejection_phrases = [
                    "hit your limit", "rate limit", "resets ", "try again",
                    "usage cap", "too many requests", "apologize",
                    "I can't", "I cannot", "as an AI",
                ]
                is_garbage = (
                    len(note_content) < 100
                    or not note_content.lstrip().startswith("---")
                    or any(p in note_content.lower() for p in rejection_phrases)
                )

                if is_garbage:
                    err = {"title": title, "error": f"Invalid content: {note_content[:80]}..."}
                    errors.append(err)
                    yield sse_event({"type": "error", "index": i + 1, "total": len(note_plan), **err})
                else:
                    quality = score_note_quality(note_content)
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    dest_path.write_text(note_content, encoding="utf-8")
                    written += 1
                    notes_written.append({
                        "title": title,
                        "file": str(dest_path.relative_to(vault)),
                        "quality": quality,
                    })
                    yield sse_event({
                        "type": "note_written",
                        "index": i + 1, "total": len(note_plan),
                        "title": title,
                        "file": str(dest_path.relative_to(vault)),
                        "quality": quality,
                    })
            else:
                err = {"title": title, "error": r.get("error", "Empty response")}
                errors.append(err)
                yield sse_event({"type": "error", "index": i + 1, "total": len(note_plan), **err})

        # MOC
        yield sse_event({"type": "progress", "status": "writing MOC",
                         "index": len(note_plan), "total": len(note_plan), "title": "Map of Content"})
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
        for res in resources[:10]:
            moc_lines.append(f"- [{res.get('title', 'Link')}]({res.get('url', '')})")
        moc_lines += ["", "## Gaps", ""]
        for g in gaps:
            moc_lines.append(f"- [ ] {g}")
        moc_path = vault / inbox / sanitize_filename(f"{topic} MOC.md")
        moc_path.write_text("\n".join(moc_lines), encoding="utf-8")

        # Resources note
        res_lines = [
            "---", f'title: "{topic} Resources"',
            f"created: {datetime.now().strftime('%Y-%m-%d')}",
            f"tags: [{', '.join(default_tags)}, resources]",
            "status: draft", "---", "",
            f"# {topic} — Resources", "",
        ]
        for res in resources:
            icon = {"youtube": "🎬", "article": "📄", "documentation": "📚", "paper": "📑"}.get(res.get("type", ""), "🔗")
            res_lines.append(f"{icon} [{res.get('title', '')}]({res.get('url', '')})")
            res_lines.append(f"   Covers: {res.get('covers', 'general')}")
            res_lines.append("")
        rp = vault / resources_folder / sanitize_filename(f"{topic} Resources.md")
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text("\n".join(res_lines), encoding="utf-8")

        # Clean staging
        for f in staging.iterdir():
            if f.is_file() and not f.name.startswith("."):
                f.unlink()
        cs_path = staging / ".staging_checksums.json"
        if cs_path.exists():
            cs_path.unlink()

        # Rebuild index + embeddings
        yield sse_event({"type": "progress", "status": "rebuilding index",
                         "index": len(note_plan), "total": len(note_plan), "title": "Index"})
        updated_index = build_index(vault)
        save_index(vault, updated_index)

        embed_stats = None
        emb = get_embeddings(vault)
        if emb:
            try:
                embed_stats = emb.sync_with_vault(updated_index)
            except Exception as e:
                embed_stats = {"error": str(e)}

        # Update state
        fresh_state = load_state(vault)
        if "topics" not in fresh_state:
            fresh_state["topics"] = {}
        if topic not in fresh_state["topics"]:
            fresh_state["topics"][topic] = {}
        fresh_state["topics"][topic]["phase"] = "complete"
        fresh_state["topics"][topic]["notes_written"] = written
        fresh_state["active_topic"] = topic
        save_state(vault, fresh_state)

        yield sse_event({
            "type": "done",
            "written": written,
            "errors": errors,
            "notes": notes_written,
            "moc": str(moc_path.relative_to(vault)),
            "embeddings": embed_stats,
        })

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── LEGACY /api/ingest — kept for Direct Ingest mode ──

@app.route("/api/ingest", methods=["POST"])
def ingest():
    """
    Legacy blocking ingest used by Direct Ingest mode.
    For the full pipeline, prefer /api/ingest/plan + /api/ingest/write (SSE).
    """
    vault = get_vault()
    cfg = load_config()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    topic = (request.json or {}).get("topic", "").strip()
    state = load_state(vault)
    if not topic:
        topic = state.get("active_topic", "Research")

    staging = vault / cfg.get("staging_folder", "04 - Staging")
    inbox = cfg.get("notes_folder", "00 - Inbox")
    resources_folder = cfg.get("resources_folder", "02 - Resources")
    default_tags = cfg.get("default_tags", ["ai-generated", "needs-review"])

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
    td = state.get("topics", {}).get(topic, {})
    gaps = td.get("analysis", {}).get("gaps", [])
    resources = td.get("resources", [])

    results = {"notes": [], "errors": []}

    index = build_index(vault)
    save_index(vault, index)
    index_text = get_index_for_prompt(vault, max_chars=10000)

    plan_prompt = dedent(f"""\
    Use SKILL: plan_notes from .claude/skills/my-brain/SKILL.md.

    Topic: "{topic}"
    Gaps to address: {json.dumps(gaps)}

    {index_text}

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
    written = 0
    index_compact = get_index_for_prompt(vault, max_chars=6000)

    for ni in note_plan:
        title = ni["title"]
        filename = sanitize_filename(ni.get("filename", f"{title}.md"))
        summary = ni.get("summary", "")
        others = ", ".join(f"[[{t}]]" for t in all_titles if t != title)

        dest_path = vault / inbox / filename
        if dest_path.exists():
            results["notes"].append({
                "title": title,
                "file": str(dest_path.relative_to(vault)),
                "skipped": True,
            })
            continue

        relevant_content = extract_relevant_content(content, title, max_chars=12000)

        write_prompt = dedent(f"""\
        Use SKILL: write_note from .claude/skills/my-brain/SKILL.md.
        Use .claude/skills/obsidian-markdown/SKILL.md for proper Obsidian syntax.

        Write a COMPREHENSIVE note. Title: "{title}", Topic: "{topic}", Covers: {summary}
        Created: {datetime.now().strftime('%Y-%m-%d')}, Tags: {json.dumps(default_tags)}
        Other notes in this batch: {others}

        {index_compact}

        Source research:
        ---
        {relevant_content}
        ---

        Output ONLY the raw markdown note starting with --- frontmatter. No fences.
        """)

        r = run_claude(write_prompt, str(vault), timeout=180)

        if r["ok"] and r["output"]:
            note_content = clean_note_content(r["output"])
            rejection_phrases = [
                "hit your limit", "rate limit", "resets ", "try again",
                "usage cap", "too many requests", "apologize",
                "I can't", "I cannot", "as an AI",
            ]
            is_garbage = (
                len(note_content) < 100
                or not note_content.lstrip().startswith("---")
                or any(p in note_content.lower() for p in rejection_phrases)
            )

            if is_garbage:
                results["errors"].append({"title": title, "error": f"Invalid content: {note_content[:100]}..."})
            else:
                quality = score_note_quality(note_content)
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_text(note_content, encoding="utf-8")
                results["notes"].append({
                    "title": title,
                    "file": str(dest_path.relative_to(vault)),
                    "quality": quality,
                })
                written += 1
        else:
            results["errors"].append({"title": title, "error": r.get("error", "Empty response")})

    # MOC
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
    for res in resources[:10]:
        moc_lines.append(f"- [{res.get('title', 'Link')}]({res.get('url', '')})")
    moc_lines += ["", "## Gaps", ""]
    for g in gaps:
        moc_lines.append(f"- [ ] {g}")
    moc_path = vault / inbox / sanitize_filename(f"{topic} MOC.md")
    moc_path.write_text("\n".join(moc_lines), encoding="utf-8")

    # Resources note
    res_lines = [
        "---", f'title: "{topic} Resources"',
        f"created: {datetime.now().strftime('%Y-%m-%d')}",
        f"tags: [{', '.join(default_tags)}, resources]",
        "status: draft", "---", "",
        f"# {topic} — Resources", "",
    ]
    for res in resources:
        icon = {"youtube": "🎬", "article": "📄", "documentation": "📚", "paper": "📑"}.get(res.get("type", ""), "🔗")
        res_lines.append(f"{icon} [{res.get('title', '')}]({res.get('url', '')})")
        res_lines.append(f"   Covers: {res.get('covers', 'general')}")
        res_lines.append("")
    rp = vault / resources_folder / sanitize_filename(f"{topic} Resources.md")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("\n".join(res_lines), encoding="utf-8")

    # Clean staging
    for f in staging.iterdir():
        if f.is_file() and not f.name.startswith("."):
            f.unlink()
    cs_path = staging / ".staging_checksums.json"
    if cs_path.exists():
        cs_path.unlink()

    updated_index = build_index(vault)
    save_index(vault, updated_index)

    emb = get_embeddings(vault)
    if emb:
        try:
            results["embeddings"] = emb.sync_with_vault(updated_index)
        except Exception as e:
            results["embeddings"] = {"error": str(e)}

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


# ── EMBEDDINGS API ──

# ──────────────────────────────────────────────────────────────
# LEARN — Flashcards, Canvas & Quiz from validated vault notes
# ──────────────────────────────────────────────────────────────

def _get_topic_notes(vault, knowledge_folder, topic):
    """Return list of {title, path, content, summary, links} for a topic from 01-Knowledge."""
    topic_lower = topic.lower()
    topic_words = [w for w in topic_lower.split() if len(w) > 3]
    notes = []
    for md in sorted(knowledge_folder.rglob("*.md")):
        if md.name.startswith("."):
            continue
        try:
            content = md.read_text("utf-8", errors="ignore")
        except Exception:
            continue
        tags, note_title = [], md.stem
        fm = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if fm:
            try:
                meta = yaml.safe_load(fm.group(1))
                if isinstance(meta, dict):
                    t = meta.get("tags", [])
                    tags = t if isinstance(t, list) else [str(t)]
                    note_title = meta.get("title", md.stem)
            except Exception:
                pass
        tags_lower = [t.lower().replace("-", " ").replace("_", " ") for t in tags]
        stem_lower = md.stem.lower()
        if (any(topic_lower in tag for tag in tags_lower)
                or any(w in stem_lower for w in topic_words)):
            body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, flags=re.DOTALL)
            body = re.sub(r"^#.*\n+", "", body).strip()
            summary = body.split("\n\n")[0].strip()[:200] if body else ""
            links = re.findall(r"\[\[([^\]|]+)", content)
            notes.append({
                "title": note_title,
                "path": str(md.relative_to(vault)),
                "content": content,
                "summary": summary,
                "links": links[:10],
            })
    return notes


@app.route("/api/learn/topics")
def learn_topics():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400
    cfg = load_config()
    knowledge = vault / cfg.get("knowledge_folder", "01 - Knowledge")
    if not knowledge.exists():
        return jsonify({"topics": []})

    topic_map = {}
    system_tags = {"ai-generated", "needs-review", "validated", "flashcards", "MOC", "resources"}
    for md in sorted(knowledge.rglob("*.md")):
        if md.name.startswith(".") or md.name == "CLAUDE.md":
            continue
        try:
            content = md.read_text("utf-8", errors="ignore")
        except Exception:
            continue
        title, tags = md.stem, []
        fm = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if fm:
            try:
                meta = yaml.safe_load(fm.group(1))
                if isinstance(meta, dict):
                    title = meta.get("title", md.stem)
                    t = meta.get("tags", [])
                    tags = t if isinstance(t, list) else [str(t)]
            except Exception:
                pass
        topic_tags = [t for t in tags if t not in system_tags and not t.startswith("status")]
        topic = topic_tags[0] if topic_tags else md.stem.split(" ")[0]
        topic = topic.replace("-", " ").replace("_", " ").title()
        topic_map.setdefault(topic, []).append({"title": title, "path": str(md.relative_to(vault))})

    topics = [{"name": n, "note_count": len(v), "notes": v} for n, v in sorted(topic_map.items())]
    return jsonify({"topics": topics})


@app.route("/api/learn/flashcards", methods=["POST"])
def learn_flashcards():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400
    cfg = load_config()
    topic = (request.json or {}).get("topic", "").strip()
    if not topic:
        return jsonify({"error": "No topic provided"}), 400

    knowledge = vault / cfg.get("knowledge_folder", "01 - Knowledge")
    learning = vault / cfg.get("learning_folder", "05 - Learning")
    learning.mkdir(parents=True, exist_ok=True)

    notes = _get_topic_notes(vault, knowledge, topic)
    if not notes:
        return jsonify({"error": f"No validated notes found for '{topic}' in 01 - Knowledge"}), 404

    combined = "\n\n".join(f"=== {n['title']} ===\n{n['content']}" for n in notes)[:20000]
    prompt = dedent(f"""\
    Generate Obsidian spaced-repetition flashcards from these validated knowledge base notes.
    Topic: "{topic}"
    Source notes:
    ---
    {combined}
    ---
    Generate 15-25 flashcards. Use ONLY info from the notes above.
    Mix question types: definition, how-it-works, compare/contrast, give-an-example.
    Answers: concise, 2-5 sentences. Include [[wikilinks]] where relevant.

    FORMAT — exactly this for each card:
    #flashcard
    [Question]
    ?
    [Answer]

    Start with YAML frontmatter: title, created ({datetime.now().strftime('%Y-%m-%d')}), tags (topic + flashcards), status: active
    Then one intro line, then cards.
    Output raw markdown only — no fences.
    """)

    result = run_claude(prompt, str(vault), timeout=180)
    if not result["ok"] or not result["output"]:
        return jsonify({"error": result.get("error", "Empty response")}), 500
    content = clean_note_content(result["output"])
    if len(content) < 100 or "#flashcard" not in content:
        return jsonify({"error": f"Invalid flashcard output: {content[:200]}"}), 500

    filename = sanitize_filename(f"{topic} Flashcards")
    dest = learning / filename
    dest.write_text(content, encoding="utf-8")
    return jsonify({
        "topic": topic,
        "file": str(dest.relative_to(vault)),
        "card_count": content.count("#flashcard"),
        "notes_used": len(notes),
    })


@app.route("/api/learn/flashcards/load", methods=["POST"])
def learn_flashcards_load():
    """Parse a saved flashcard file and return cards as JSON for in-UI review."""
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400
    cfg = load_config()
    topic = (request.json or {}).get("topic", "").strip()
    if not topic:
        return jsonify({"error": "No topic"}), 400

    learning = vault / cfg.get("learning_folder", "05 - Learning")
    filename = sanitize_filename(f"{topic} Flashcards")
    fc_file = learning / filename
    if not fc_file.exists():
        return jsonify({"error": "Flashcard file not found — generate flashcards first"}), 404

    raw = fc_file.read_text("utf-8", errors="ignore")

    # Parse cards: each block starts with #flashcard, has Q, then ?, then A
    cards = []
    blocks = re.split(r'\n(?=#flashcard)', raw)
    for block in blocks:
        if "#flashcard" not in block:
            continue
        # Remove #flashcard tag line
        block = re.sub(r'^#flashcard\s*\n?', '', block.strip())
        if '?' not in block:
            continue
        parts = block.split('?', 1)
        question = parts[0].strip()
        answer = parts[1].strip() if len(parts) > 1 else ""
        # Clean wikilinks to plain text for display
        question = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', question)
        answer = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', answer)
        if question and answer:
            cards.append({"question": question, "answer": answer})

    return jsonify({"topic": topic, "cards": cards, "count": len(cards)})


@app.route("/api/learn/canvas", methods=["POST"])
def learn_canvas():
    import math
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400
    cfg = load_config()
    topic = (request.json or {}).get("topic", "").strip()
    if not topic:
        return jsonify({"error": "No topic provided"}), 400

    knowledge = vault / cfg.get("knowledge_folder", "01 - Knowledge")
    learning = vault / cfg.get("learning_folder", "05 - Learning")
    learning.mkdir(parents=True, exist_ok=True)

    topic_notes = _get_topic_notes(vault, knowledge, topic)
    if not topic_notes:
        return jsonify({"error": f"No validated notes found for '{topic}' in 01 - Knowledge"}), 404

    nodes, edges, node_id_map = [], [], {}
    nodes.append({"id": "node-center", "type": "text", "text": f"# {topic}",
                  "x": 0, "y": 0, "width": 200, "height": 60, "color": "1"})

    count = len(topic_notes)
    radius = max(320, count * 55)
    for i, note in enumerate(topic_notes):
        angle = (2 * math.pi * i / count) - (math.pi / 2)
        x = int(radius * math.cos(angle)) - 130
        y = int(radius * math.sin(angle)) - 50
        nid = f"node-{i}"
        node_id_map[note["title"].lower()] = nid
        text = f"**[[{note['title']}]]**"
        if note["summary"]:
            text += f"\n\n{note['summary'][:100]}{'...' if len(note['summary']) > 100 else ''}"
        nodes.append({"id": nid, "type": "text", "text": text,
                      "x": x, "y": y, "width": 260, "height": 100})
        edges.append({"id": f"edge-c-{i}", "fromNode": "node-center", "fromSide": "right",
                      "toNode": nid, "toSide": "left"})

    edge_set = set()
    for i, note in enumerate(topic_notes):
        for link in note["links"]:
            dst = node_id_map.get(link.lower())
            if dst and dst != f"node-{i}":
                key = tuple(sorted([f"node-{i}", dst]))
                if key not in edge_set:
                    edge_set.add(key)
                    edges.append({"id": f"edge-l-{len(edge_set)}", "fromNode": f"node-{i}",
                                  "fromSide": "bottom", "toNode": dst, "toSide": "bottom", "color": "4"})

    canvas_name = re.sub(r'[#/\\*?"<>|]', "", topic).strip()
    dest = learning / f"{canvas_name} Mind Map.canvas"
    dest.write_text(json.dumps({"nodes": nodes, "edges": edges}, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"topic": topic, "file": str(dest.relative_to(vault)),
                    "node_count": len(topic_notes), "edge_count": len(edges)})


@app.route("/api/learn/quiz/generate", methods=["POST"])
def learn_quiz_generate():
    """Generate a multiple-choice quiz from validated vault notes. Returns JSON quiz data."""
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400
    cfg = load_config()
    topic = (request.json or {}).get("topic", "").strip()
    if not topic:
        return jsonify({"error": "No topic provided"}), 400

    knowledge = vault / cfg.get("knowledge_folder", "01 - Knowledge")
    notes = _get_topic_notes(vault, knowledge, topic)
    if not notes:
        return jsonify({"error": f"No validated notes found for '{topic}' in 01 - Knowledge"}), 404

    combined = "\n\n".join(f"=== {n['title']} ===\n{n['content']}" for n in notes)[:18000]

    prompt = dedent(f"""\
    Generate a multiple-choice quiz from these validated knowledge base notes.
    Topic: "{topic}"
    Source notes:
    ---
    {combined}
    ---

    Generate exactly 10 questions. Use ONLY facts from the notes above.
    Each question must have exactly 4 options (A, B, C, D). Only one is correct.
    Mix difficulty: 3 easy, 4 medium, 3 hard.
    Make wrong answers plausible — not obviously silly.

    Respond with ONLY a JSON array, no fences, no explanation:
    [
      {{
        "question": "Question text here?",
        "options": ["Option A", "Option B", "Option C", "Option D"],
        "correct": 0,
        "explanation": "Why this answer is correct, referencing the source note."
      }}
    ]

    "correct" is the 0-based index of the correct option in the options array.
    """)

    result = run_claude(prompt, str(vault), timeout=180)
    if not result["ok"] or not result["output"]:
        return jsonify({"error": result.get("error", "Empty response")}), 500

    try:
        questions = parse_json_response(result["output"])
        if not isinstance(questions, list) or len(questions) == 0:
            raise ValueError("Not a list")
        # Validate structure
        for q in questions:
            assert "question" in q and "options" in q and "correct" in q
            assert len(q["options"]) == 4
    except Exception as e:
        return jsonify({"error": f"Invalid quiz JSON: {str(e)} — {result['output'][:200]}"}), 500

    return jsonify({"topic": topic, "questions": questions, "count": len(questions)})


@app.route("/api/vault/embed/stats")
def embed_stats():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400
    if not embeddings_available():
        return jsonify({
            "available": False,
            "reason": "sentence-transformers and hnswlib not installed",
            "install": "pip install sentence-transformers hnswlib numpy",
        })
    emb = get_embeddings(vault)
    if not emb:
        return jsonify({"available": False, "reason": "Failed to initialise"})
    return jsonify(emb.stats())


@app.route("/api/vault/embed/sync", methods=["POST"])
def embed_sync():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400
    if not embeddings_available():
        return jsonify({"error": "sentence-transformers / hnswlib not installed"}), 400

    index = load_index(vault)
    if not index:
        index = build_index(vault)
        save_index(vault, index)

    emb = get_embeddings(vault)
    if not emb:
        return jsonify({"error": "Failed to initialise embeddings"}), 500

    try:
        stats = emb.sync_with_vault(index)
        stats["total_in_index"] = len(index)
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vault/search", methods=["POST"])
def semantic_search():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    query = (request.json or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    n = min(int((request.json or {}).get("n", 10)), 50)

    if not embeddings_available():
        scan = scan_vault(vault, query)
        return jsonify({"results": scan["related"][:n], "method": "keyword_fallback", "semantic_available": False})

    emb = get_embeddings(vault)
    if not emb or emb.stats()["embedded_notes"] == 0:
        scan = scan_vault(vault, query)
        return jsonify({
            "results": scan["related"][:n],
            "method": "keyword_fallback",
            "semantic_available": embeddings_available(),
            "note": "Run 'Sync Embeddings' to enable semantic search",
        })

    try:
        results = emb.search(query, n=n)
        return jsonify({"results": results, "method": "semantic", "semantic_available": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vault/similar", methods=["POST"])
def find_similar_notes():
    vault = get_vault()
    if not vault:
        return jsonify({"error": "No vault"}), 400

    rel_path = (request.json or {}).get("path", "").strip()
    if not rel_path:
        return jsonify({"error": "No path provided"}), 400

    n = min(int((request.json or {}).get("n", 5)), 20)

    if not embeddings_available():
        return jsonify({"error": "Embeddings not available", "results": []})

    emb = get_embeddings(vault)
    if not emb:
        return jsonify({"error": "Failed to initialise embeddings", "results": []})

    try:
        results = emb.find_similar(rel_path, n=n)
        return jsonify({"results": results, "for": rel_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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

    warnings = validate_config()
    if warnings:
        print("\n  ⚠ Startup warnings:")
        for w in warnings:
            print(f"    • {w}")
        print()

    print(f"  Static: {STATIC_DIR}")
    print(f"  Claude CLI: {'✓ Found' if check_claude() else '✗ Not found'}")
    print(f"\n  → http://localhost:5000\n")

    webbrowser.open("http://localhost:5000")
    app.run(debug=False, port=5000)