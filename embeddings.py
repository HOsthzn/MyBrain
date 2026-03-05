#!/usr/bin/env python3
"""
MY BRAIN — Embeddings Module
Persistent semantic search using hnswlib + numpy + plain JSON.

No ChromaDB, no pydantic — works on Python 3.14+.

Storage (inside the vault):
    .embeddings/
        index.bin       ← hnswlib HNSW vector index
        metadata.json   ← id → {path, title} mapping

The index survives server restarts completely. On startup the index.bin
is loaded from disk in milliseconds — no re-embedding needed.

Usage:
    from embeddings import get_embeddings, is_available

    emb = get_embeddings(vault_path)   # None if packages missing
    if emb:
        emb.upsert_note(rel_path, title, content)
        results = emb.search("concurrency patterns", n=10)
        similar = emb.find_similar("00 - Inbox/My Note.md", n=5)
        stats   = emb.stats()
        emb.sync_with_vault(index_dict)
"""

from pathlib import Path
import json
import re

# ── Optional dependency guard ─────────────────────────────────────────────────
_AVAILABLE = False
_UNAVAILABLE_REASON = ""

try:
    import numpy as np
    import hnswlib
    from sentence_transformers import SentenceTransformer
    _AVAILABLE = True
except (ImportError, Exception) as e:
    _UNAVAILABLE_REASON = str(e)


class EmbeddingsUnavailable(RuntimeError):
    pass


EMBEDDING_DIM = 384
MAX_VAULT_NOTES = 10_000

_MODEL = None

def _get_model():
    global _MODEL
    if not _AVAILABLE:
        raise EmbeddingsUnavailable(
            f"hnswlib/numpy/sentence-transformers not installed: {_UNAVAILABLE_REASON}\n"
            "Run: pip install hnswlib numpy sentence-transformers"
        )
    if _MODEL is None:
        print("  [embeddings] Loading model (first run — will cache locally)...")
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        print("  [embeddings] Model ready.")
    return _MODEL


def _note_to_text(title: str, content: str) -> str:
    text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, flags=re.DOTALL)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return f"{title}\n{title}\n\n{text}"[:8000]


class VaultEmbeddings:
    """
    Persistent vector store for an Obsidian vault.

    Files on disk:
        <vault>/.embeddings/index.bin      — hnswlib HNSW index (binary)
        <vault>/.embeddings/metadata.json  — int_id <-> path/title mapping

    hnswlib uses integer IDs, so we maintain a bidirectional mapping between
    relative paths and integer IDs in metadata.json. Deleted notes are removed
    from metadata and filtered out of results (hnswlib has no true delete).
    When the index fills up it rebuilds from scratch automatically.
    """

    EMBED_DIR = ".embeddings"

    def __init__(self, vault: Path):
        if not _AVAILABLE:
            raise EmbeddingsUnavailable(_UNAVAILABLE_REASON)

        self.vault = Path(vault)
        self._dir = self.vault / self.EMBED_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

        self._index_path = self._dir / "index.bin"
        self._meta_path  = self._dir / "metadata.json"

        self._meta  = self._load_meta()
        self._index = self._load_index()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_meta(self) -> dict:
        if self._meta_path.exists():
            try:
                return json.loads(self._meta_path.read_text("utf-8"))
            except Exception:
                pass
        return {"next_id": 0, "by_id": {}, "by_path": {}}

    def _save_meta(self):
        self._meta_path.write_text(
            json.dumps(self._meta, ensure_ascii=False), encoding="utf-8"
        )

    def _load_index(self):
        idx = hnswlib.Index(space="cosine", dim=EMBEDDING_DIM)
        if self._index_path.exists() and self._meta["next_id"] > 0:
            try:
                idx.load_index(str(self._index_path), max_elements=MAX_VAULT_NOTES)
                return idx
            except Exception as e:
                print(f"  [embeddings] Index load failed ({e}), starting fresh.")
        idx.init_index(max_elements=MAX_VAULT_NOTES, ef_construction=200, M=16)
        idx.set_ef(50)
        return idx

    def _save_index(self):
        self._index.save_index(str(self._index_path))

    def _rebuild_index(self, notes: list):
        print(f"  [embeddings] Rebuilding index ({len(notes)} notes)...")
        self._meta  = {"next_id": 0, "by_id": {}, "by_path": {}}
        self._index = hnswlib.Index(space="cosine", dim=EMBEDDING_DIM)
        self._index.init_index(max_elements=MAX_VAULT_NOTES, ef_construction=200, M=16)
        self._index.set_ef(50)
        self.upsert_batch(notes)

    # ── ID management ─────────────────────────────────────────────────────────

    def _get_or_assign_id(self, rel_path: str) -> int:
        if rel_path in self._meta["by_path"]:
            return int(self._meta["by_path"][rel_path])
        new_id = self._meta["next_id"]
        self._meta["next_id"] += 1
        self._meta["by_path"][rel_path] = str(new_id)
        return new_id

    def _register(self, int_id: int, rel_path: str, title: str):
        self._meta["by_id"][str(int_id)] = {"path": rel_path, "title": title}
        self._meta["by_path"][rel_path]  = str(int_id)

    # ── Write ─────────────────────────────────────────────────────────────────

    def upsert_note(self, rel_path: str, title: str, content: str) -> None:
        """Embed and store (or update) a single note. Idempotent."""
        model = _get_model()
        vec   = model.encode(_note_to_text(title, content),
                             show_progress_bar=False).astype("float32")
        int_id = self._get_or_assign_id(rel_path)
        self._register(int_id, rel_path, title)
        self._index.add_items(vec.reshape(1, -1), ids=[int_id])
        self._save_index()
        self._save_meta()

    def upsert_batch(self, notes: list) -> int:
        """
        Embed a list of notes in one batch pass — much faster than one-at-a-time.
        Each item: {"rel_path": str, "title": str, "content": str}
        """
        if not notes:
            return 0

        model  = _get_model()
        texts  = [_note_to_text(n["title"], n["content"]) for n in notes]
        print(f"  [embeddings] Embedding {len(texts)} notes...")
        vecs   = model.encode(texts, show_progress_bar=len(texts) > 20,
                              batch_size=32).astype("float32")

        ids = [self._get_or_assign_id(n["rel_path"]) for n in notes]
        for i, n in enumerate(notes):
            self._register(ids[i], n["rel_path"], n["title"])

        self._index.add_items(vecs, ids=ids)
        self._save_index()
        self._save_meta()
        return len(notes)

    def remove_note(self, rel_path: str) -> None:
        """
        Remove a note. hnswlib has no true delete — we drop it from metadata
        so it is filtered out of every future search result.
        """
        str_id = self._meta["by_path"].pop(rel_path, None)
        if str_id:
            self._meta["by_id"].pop(str_id, None)
            self._save_meta()

    # ── Read ──────────────────────────────────────────────────────────────────

    def search(self, query: str, n: int = 10) -> list:
        """
        Semantic search — top-n notes most similar to the query string.
        Returns [{"path", "title", "score"}, ...], score in [0, 1].
        """
        if self._index.get_current_count() == 0:
            return []

        model = _get_model()
        q_vec = model.encode(query, show_progress_bar=False).astype("float32")

        # Over-fetch to account for stale (deleted) IDs being filtered out
        k = min(n * 2, self._index.get_current_count())
        labels, distances = self._index.knn_query(q_vec.reshape(1, -1), k=k)

        results = []
        for label, dist in zip(labels[0], distances[0]):
            info = self._meta["by_id"].get(str(label))
            if not info:
                continue   # stale — skip
            results.append({
                "path":  info["path"],
                "title": info["title"],
                # hnswlib cosine space: distance = 1 - cosine_similarity
                "score": round(1.0 - float(dist), 4),
            })
            if len(results) >= n:
                break

        return results

    def find_similar(self, rel_path: str, n: int = 5) -> list:
        """
        Find notes semantically similar to an existing note.
        Useful for surfacing wikilinks Claude might have missed.
        """
        str_id = self._meta["by_path"].get(rel_path)
        if not str_id:
            return []

        note_path = self.vault / rel_path
        if not note_path.exists():
            return []

        try:
            content = note_path.read_text("utf-8", errors="ignore")
            title   = self._meta["by_id"][str_id]["title"]
        except Exception:
            return []

        model = _get_model()
        vec   = model.encode(_note_to_text(title, content),
                             show_progress_bar=False).astype("float32")

        k = min(n + 1, self._index.get_current_count())
        labels, distances = self._index.knn_query(vec.reshape(1, -1), k=k)

        results = []
        for label, dist in zip(labels[0], distances[0]):
            if str(label) == str_id:
                continue   # skip self
            info = self._meta["by_id"].get(str(label))
            if not info:
                continue   # stale
            results.append({
                "path":  info["path"],
                "title": info["title"],
                "score": round(1.0 - float(dist), 4),
            })
            if len(results) >= n:
                break

        return results

    # ── Sync ──────────────────────────────────────────────────────────────────

    def sync_with_vault(self, index: dict) -> dict:
        """
        Incremental sync against the vault index dict (from build_index).

        - Embeds notes not yet stored (new notes only — fast restarts).
        - Removes metadata for notes deleted from the vault.
        - Rebuilds from scratch if capacity would be exceeded.

        Returns {"added": int, "removed": int, "skipped": int}
        """
        current_paths = set(self._meta["by_path"].keys())
        target_paths  = set(index.keys())

        # Clean up deleted notes
        removed = 0
        for path in current_paths - target_paths:
            self.remove_note(path)
            removed += 1

        # Collect notes that need embedding
        to_add = []
        for rel_path in target_paths - current_paths:
            note_path = self.vault / rel_path
            if not note_path.exists():
                continue
            try:
                content = note_path.read_text("utf-8", errors="ignore")
                title   = index[rel_path].get("title", note_path.stem)
                to_add.append({"rel_path": rel_path, "title": title, "content": content})
            except Exception:
                pass

        # Full rebuild if we'd exceed the index capacity
        if self._meta["next_id"] + len(to_add) >= MAX_VAULT_NOTES:
            all_notes = []
            for rel_path, info in index.items():
                note_path = self.vault / rel_path
                if note_path.exists():
                    try:
                        content = note_path.read_text("utf-8", errors="ignore")
                        all_notes.append({
                            "rel_path": rel_path,
                            "title":    info.get("title", note_path.stem),
                            "content":  content,
                        })
                    except Exception:
                        pass
            self._rebuild_index(all_notes)
            return {"added": len(all_notes), "removed": removed, "skipped": 0}

        added   = self.upsert_batch(to_add)
        skipped = len(target_paths) - len(to_add)
        return {"added": added, "removed": removed, "skipped": skipped}

    # ── Info ──────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "available":       True,
            "embedded_notes":  len(self._meta["by_path"]),
            "index_capacity":  MAX_VAULT_NOTES,
            "model":           "all-MiniLM-L6-v2",
            "storage":         str(self._dir),
        }


# ── Module-level convenience ──────────────────────────────────────────────────

def is_available() -> bool:
    return _AVAILABLE


def get_embeddings(vault: Path):
    """Return a VaultEmbeddings instance, or None if packages aren't installed."""
    if not _AVAILABLE:
        return None
    try:
        return VaultEmbeddings(vault)
    except Exception as e:
        print(f"  [embeddings] Failed to initialise: {e}")
        return None