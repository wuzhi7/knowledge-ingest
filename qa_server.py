#!/usr/bin/env python3
"""Local RAG HTTP service for the Wanweigang Obsidian vault."""
from __future__ import annotations

import array
import json
import math
import os
import re
import sqlite3
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from indexer import DB_PATH, OLLAMA, EMBED_MODEL, LOCAL_OPENER, db_connect, ollama_embed

HOST = "127.0.0.1"
PORT = int(os.environ.get("WANWEI_QA_PORT", "8765"))
CHAT_MODEL = os.environ.get("WANWEI_CHAT_MODEL", "qwen3:4b")


def fts_query(question: str) -> str:
    tokens = []
    for run in re.findall(r"[\u3400-\u9fff]{2,}|[A-Za-z0-9_-]{3,}", question):
        if re.fullmatch(r"[\u3400-\u9fff]+", run) and len(run) > 3:
            tokens.extend(run[i:i + 3] for i in range(len(run) - 2))
        else:
            tokens.append(run)
    tokens = list(dict.fromkeys(tokens))[:24]
    return " OR ".join('"' + t.replace('"', '') + '"' for t in tokens)


def unpack_vector(blob: bytes) -> array.array:
    values = array.array("f")
    values.frombytes(blob)
    return values


def retrieve(question: str, limit: int = 8) -> list[dict]:
    db = db_connect()
    keyword, semantic = [], []
    query = fts_query(question)
    if query:
        try:
            keyword = db.execute("""
              SELECT c.chunk_id,c.document_id,c.ordinal,c.content,d.path,d.title,d.book,
                     bm25(chunks_fts, 0.0, 3.0, 1.5, 1.0) AS rank
              FROM chunks_fts f JOIN chunks c ON c.chunk_id=f.chunk_id
              JOIN documents d ON d.document_id=c.document_id
              WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 50
            """, (query,)).fetchall()
        except sqlite3.OperationalError:
            keyword = []
    try:
        qvec_values = ollama_embed([question])[0]
        qvec = array.array("f", qvec_values)
        qnorm = math.sqrt(sum(v * v for v in qvec)) or 1.0
        candidates = db.execute("""
          SELECT c.chunk_id,c.document_id,c.ordinal,c.content,c.embedding,c.embedding_norm,
                 d.path,d.title,d.book FROM chunks c JOIN documents d USING(document_id)
          WHERE c.embedding IS NOT NULL
        """).fetchall()
        scored = []
        for row in candidates:
            vec = unpack_vector(row["embedding"])
            score = sum(a * b for a, b in zip(qvec, vec)) / (qnorm * (row["embedding_norm"] or 1.0))
            scored.append((score, row))
        semantic = [row for _, row in sorted(scored, key=lambda x: x[0], reverse=True)[:50]]
    except Exception:
        semantic = []

    scores: dict[str, float] = {}
    rows: dict[str, sqlite3.Row] = {}
    for rank, row in enumerate(keyword):
        rows[row["chunk_id"]] = row
        scores[row["chunk_id"]] = scores.get(row["chunk_id"], 0) + 1.0 / (60 + rank)
    for rank, row in enumerate(semantic):
        rows[row["chunk_id"]] = row
        scores[row["chunk_id"]] = scores.get(row["chunk_id"], 0) + 1.2 / (60 + rank)
    ordered = sorted(scores, key=scores.get, reverse=True)
    result, per_doc = [], {}
    for chunk_id in ordered:
        row = rows[chunk_id]
        if per_doc.get(row["document_id"], 0) >= 2:
            continue
        per_doc[row["document_id"]] = per_doc.get(row["document_id"], 0) + 1
        result.append({"chunk_id": chunk_id, "document_id": row["document_id"],
                       "ordinal": row["ordinal"], "content": row["content"],
                       "path": row["path"], "title": row["title"], "book": row["book"]})
        if len(result) >= limit:
            break
    db.close()
    return result


def chat(question: str, sources: list[dict]) -> str:
    context = []
    for i, source in enumerate(sources, 1):
        context.append(
            f"[S{i}] 书名：{source['book']}\n文章：{source['title']}\n"
            f"Obsidian路径：{source['path']}\n内容：\n{source['content']}"
        )
    system = (
        "你是万维钢个人知识库的研究助手。只能根据给出的资料回答，不得假装自己是万维钢。"
        "区分原文观点和你的综合归纳；每个重要结论都用 [S1] 这样的编号引用来源。"
        "资料不足就明确说资料不足，不要编造。使用简体中文，先直接回答问题。"
    )
    prompt = f"问题：{question}\n\n检索资料：\n\n" + "\n\n---\n\n".join(context)
    payload = json.dumps({
        "model": CHAT_MODEL, "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "options": {"temperature": 0.2, "num_ctx": 16384}
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=payload, headers={"Content-Type": "application/json"})
    with LOCAL_OPENER.open(req, timeout=600) as response:
        result = json.load(response)
    answer = result.get("message", {}).get("content", "").strip()
    answer = re.sub(r"(?s)<think>.*?</think>\s*", "", answer)
    return answer or "模型没有返回答案。"


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, value: dict):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "app://obsidian.md")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers(); self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "app://obsidian.md")
        self.send_header("Access-Control-Allow-Methods", "POST,GET,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            try:
                db = db_connect()
                row = db.execute("SELECT count(*) docs,(SELECT count(*) FROM chunks) chunks,(SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embedded FROM documents").fetchone()
                db.close()
                self.send_json(200, {"ok": True, "model": CHAT_MODEL, **dict(row)})
            except Exception as exc:
                self.send_json(500, {"ok": False, "error": str(exc)})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size) or b"{}")
            question = str(body.get("question", "")).strip()
            if not question:
                return self.send_json(400, {"error": "问题不能为空"})
            sources = retrieve(question, int(body.get("limit", 8)))
            if self.path == "/search":
                return self.send_json(200, {"sources": sources})
            if self.path != "/ask":
                return self.send_json(404, {"error": "not found"})
            if not sources:
                return self.send_json(200, {"answer": "知识库中没有找到足够相关的内容。", "sources": []})
            answer = chat(question, sources)
            citations, seen = [], set()
            for i, source in enumerate(sources, 1):
                if source["path"] in seen: continue
                seen.add(source["path"])
                citations.append({"label": f"S{i}", "title": source["title"], "book": source["book"], "path": source["path"]})
            self.send_json(200, {"answer": answer, "sources": citations})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


if __name__ == "__main__":
    print(f"Wanweigang QA: http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
