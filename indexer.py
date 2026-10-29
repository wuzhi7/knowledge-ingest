#!/usr/bin/env python3
"""Incremental keyword + semantic index for the Obsidian knowledge vault."""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VAULT = ROOT / "output"
INDEX_DIR = ROOT / "index"
DB_PATH = INDEX_DIR / "knowledge.db"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
EMBED_MODEL = os.environ.get("WANWEI_EMBED_MODEL", "bge-m3")
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def db_connect() -> sqlite3.Connection:
    INDEX_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    CREATE TABLE IF NOT EXISTS documents (
      document_id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, mtime_ns INTEGER NOT NULL,
      size INTEGER NOT NULL, content_hash TEXT NOT NULL, title TEXT, book TEXT,
      author TEXT, column_name TEXT, source_file TEXT, metadata_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS chunks (
      chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
      content TEXT NOT NULL, embedding BLOB, embedding_norm REAL,
      FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id, ordinal);
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
      chunk_id UNINDEXED, title, book, content, tokenize='trigram'
    );
    """)
    return db


def parse_markdown(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            for line in text[4:end].splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                value = value.strip()
                try:
                    meta[key.strip()] = json.loads(value)
                except Exception:
                    meta[key.strip()] = value.strip('"\'')
            body = text[end + 5:]
    body = re.sub(r"(?m)^#\s+.*?\n+", "", body, count=1).strip()
    return meta, body


def split_long_paragraph(paragraph: str, limit: int = 900) -> list[str]:
    if len(paragraph) <= limit:
        return [paragraph]
    sentences = [s for s in re.split(r"(?<=[。！？!?；;])", paragraph) if s]
    if len(sentences) == 1:
        return [paragraph[i:i + limit] for i in range(0, len(paragraph), limit)]
    result, current = [], ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > limit:
            result.append(current.strip()); current = ""
        current += sentence
    if current.strip(): result.append(current.strip())
    return result


def make_chunks(title: str, body: str, target: int = 800, maximum: int = 1100) -> list[str]:
    paragraphs = []
    for p in re.split(r"\n\s*\n", body):
        p = re.sub(r"\s*\n\s*", " ", p).strip()
        if p:
            paragraphs.extend(split_long_paragraph(p))
    chunks, current = [], []
    current_len = 0
    for p in paragraphs:
        extra = len(p) + (2 if current else 0)
        if current and (current_len >= target or current_len + extra > maximum):
            chunks.append(f"标题：{title}\n\n" + "\n\n".join(current))
            # A short paragraph overlap preserves context without duplicating too much.
            overlap = current[-1:] if len(current[-1]) <= 160 else []
            current, current_len = overlap[:], sum(map(len, overlap))
        current.append(p); current_len += extra
    if current:
        chunks.append(f"标题：{title}\n\n" + "\n\n".join(current))
    return chunks


def ollama_embed(texts: list[str]) -> list[list[float]]:
    payload = json.dumps({"model": EMBED_MODEL, "input": texts, "truncate": True}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/embed", data=payload, headers={"Content-Type": "application/json"})
    with LOCAL_OPENER.open(req, timeout=300) as response:
        data = json.load(response)
    embeddings = data.get("embeddings") or []
    if len(embeddings) != len(texts):
        raise RuntimeError(f"嵌入返回数量不符：需要 {len(texts)}，得到 {len(embeddings)}")
    return embeddings


def pack_embedding(values: list[float]) -> tuple[bytes, float]:
    vec = array.array("f", values)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return vec.tobytes(), norm


def iter_documents(paths: list[Path] | None = None):
    candidates = paths if paths else list(VAULT.rglob("*.md"))
    for path in sorted(candidates):
        if path.is_dir():
            yield from iter_documents(list(path.rglob("*.md")))
            continue
        if path.name == "index.md" or path.name.startswith("001-原书目录") or ".obsidian" in path.parts:
            continue
        yield path


def remove_document(db: sqlite3.Connection, document_id: str):
    ids = [r[0] for r in db.execute("SELECT chunk_id FROM chunks WHERE document_id=?", (document_id,))]
    for chunk_id in ids:
        db.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk_id,))
    db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
    db.execute("DELETE FROM documents WHERE document_id=?", (document_id,))


def update_index(paths: list[Path] | None = None, semantic: bool = True) -> dict:
    db = db_connect()
    changed, unchanged, total_chunks = 0, 0, 0
    embed_available = semantic
    embed_error = ""
    seen_paths = set()
    for path in iter_documents(paths):
        path = path.resolve()
        rel = path.relative_to(VAULT.resolve()).as_posix()
        seen_paths.add(rel)
        stat = path.stat()
        old = db.execute("SELECT * FROM documents WHERE path=?", (rel,)).fetchone()
        if old and old["mtime_ns"] == stat.st_mtime_ns and old["size"] == stat.st_size:
            unchanged += 1; continue
        meta, body = parse_markdown(path)
        title = str(meta.get("title") or path.stem)
        document_id = str(meta.get("document_id") or hashlib.sha1(rel.encode()).hexdigest()[:16])
        content_hash = hashlib.sha256(re.sub(r"\s+", "", body).encode()).hexdigest()
        if old:
            remove_document(db, old["document_id"])
        collision = db.execute("SELECT path FROM documents WHERE document_id=?", (document_id,)).fetchone()
        if collision and collision["path"] != rel:
            document_id = hashlib.sha1(rel.encode()).hexdigest()[:16]
        db.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, rel, stat.st_mtime_ns, stat.st_size, content_hash, title,
             str(meta.get("book", "")), str(meta.get("author", "")), str(meta.get("column", "")),
             str(meta.get("original_file", "")), json.dumps(meta, ensure_ascii=False)),
        )
        chunks = make_chunks(title, body)
        embeddings: list[list[float] | None] = [None] * len(chunks)
        if semantic and embed_available and chunks:
            try:
                for start in range(0, len(chunks), 16):
                    embeddings[start:start + 16] = ollama_embed(chunks[start:start + 16])
            except Exception as exc:
                embed_available = False
                embed_error = str(exc)
        for ordinal, content in enumerate(chunks):
            chunk_id = f"{document_id}:{ordinal:04d}"
            blob = norm = None
            if embeddings[ordinal] is not None:
                blob, norm = pack_embedding(embeddings[ordinal])
            db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)", (chunk_id, document_id, ordinal, content, blob, norm))
            db.execute("INSERT INTO chunks_fts VALUES(?,?,?,?)", (chunk_id, title, str(meta.get("book", "")), content))
        changed += 1; total_chunks += len(chunks)
        db.commit()

    if paths is None:
        for row in db.execute("SELECT document_id,path FROM documents").fetchall():
            if row["path"] not in seen_paths or not (VAULT / row["path"]).exists():
                remove_document(db, row["document_id"])
        db.commit()
    stats = db.execute("SELECT count(*) docs, (SELECT count(*) FROM chunks) chunks, (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embedded FROM documents").fetchone()
    db.close()
    return {"changed": changed, "unchanged": unchanged, "new_chunks": total_chunks,
            "documents": stats["docs"], "chunks": stats["chunks"], "embedded": stats["embedded"],
            "embedding_error": embed_error}


def embed_missing() -> dict:
    db = db_connect()
    rows = db.execute("SELECT chunk_id,content FROM chunks WHERE embedding IS NULL ORDER BY chunk_id").fetchall()
    done = 0
    for start in range(0, len(rows), 16):
        batch = rows[start:start + 16]
        vectors = ollama_embed([r["content"] for r in batch])
        for row, vector in zip(batch, vectors):
            blob, norm = pack_embedding(vector)
            db.execute("UPDATE chunks SET embedding=?,embedding_norm=? WHERE chunk_id=?", (blob, norm, row["chunk_id"]))
            done += 1
        db.commit()
    db.close()
    return {"embedded": done}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-semantic", action="store_true")
    ap.add_argument("--embed-missing", action="store_true")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args()
    if args.embed_missing:
        result = embed_missing()
    else:
        paths = [Path(p).resolve() for p in args.paths] or None
        result = update_index(paths, semantic=not args.no_semantic)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
