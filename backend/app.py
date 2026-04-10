#!/usr/bin/env python3
"""
Openmesh Support Agent — RAG backend.

This is the reference Python service for a forkable, RAG-powered docs
assistant deployed as a NixOS container on a sovereign Openmesh Xnode.

Design notes for future maintainers and forks:

  - Multi-source ingestion. DOCS_PATHS is a colon-separated list of
    directories to recursively scan for .md and .mdx files. Add new doc
    sources by editing the flake.nix to mount more paths and listing them
    here. The service does NOT care about the source — it just chunks and
    embeds whatever it finds.

  - All configuration via environment variables. Anyone forking this
    repo should be able to point it at their own docs without touching
    Python code: edit flake.nix, set new env vars, redeploy.

  - Idempotent ingestion. Every startup truncates the chunks table and
    re-ingests. This means re-deploying the flake (with updated docs)
    always produces a fresh corpus, no cache invalidation bugs.

  - Stable error envelope. /api/chat returns
        { "answer": str, "sources": [...] }
    on success or
        { "error": str }
    on failure. Frontend and any future MCP tool should depend on this
    shape.

  - No authentication. The service binds to 127.0.0.1; nginx (in the same
    container) is the only thing that can talk to it, and nginx is
    fronted by the xnode-manager reverse proxy which handles auth via
    xnode-auth if the operator configures it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable

import psycopg2
import requests
from flask import Flask, jsonify, request, Response, stream_with_context
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------------
# Configuration (environment variables — see flake.nix)
# ---------------------------------------------------------------------------

DOCS_PATHS = [
    Path(p)
    for p in os.environ.get("DOCS_PATHS", "/var/lib/support-agent/docs").split(":")
    if p
]
DATABASE_URL = os.environ["DATABASE_URL"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "llama3.2:1b")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
PORT = int(os.environ.get("PORT", "5000"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
TOP_K = int(os.environ.get("TOP_K", "5"))
EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))  # nomic-embed-text default
BRAND_NAME = os.environ.get("BRAND_NAME", "Openmesh Support Agent")

# Customise the personality of the assistant by overriding this whole
# string via the SYSTEM_PROMPT env var. The {brand} placeholder is filled
# from BRAND_NAME.
DEFAULT_SYSTEM_PROMPT = """You are the {brand}, a friendly assistant that helps developers and AI agents deploy and manage applications on sovereign Openmesh Xnodes via the `om` CLI. You answer questions about the CLI, deployment patterns, error codes, NixOS containers, and Claude Code integration.

RULES:
1. Answer ONLY from the provided documentation chunks below. If the answer is not there, say "I do not have that in the docs — please check https://github.com/johnforfar/openmesh-cli or the linked source."
2. Cite the source filename in square brackets like [01-deploy.md] when you use info from a chunk.
3. Be concise. Hackathon students do not have time for fluff.
4. When showing CLI commands, use code blocks.
5. Never invent flag names, URLs, or behaviour. If you are unsure, say so.

DOCUMENTATION CHUNKS:
"""
SYSTEM_PROMPT_TEMPLATE = os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[support-agent] {msg}", flush=True)


def wait_for(check_fn, name: str, max_attempts: int = 60, delay: float = 2.0) -> None:
    """Block until `check_fn` returns without raising, or give up."""
    for attempt in range(1, max_attempts + 1):
        try:
            check_fn()
            log(f"{name} is ready")
            return
        except Exception as e:  # noqa: BLE001
            log(f"waiting for {name} ({attempt}/{max_attempts}): {e}")
            time.sleep(delay)
    raise RuntimeError(f"{name} did not become ready in time")


def db_connect():
    return psycopg2.connect(DATABASE_URL)


def check_db() -> None:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")


def check_ollama() -> None:
    r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
    r.raise_for_status()


def vec_to_pg(v: list[float]) -> str:
    """pgvector accepts the bracket-string format `[a,b,c]` on cast to vector.

    Doing it this way means the service has zero hard dependency on the
    `pgvector` Python package — keeping the nix closure small.
    """
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def embed(text: str) -> list[float]:
    r = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["embedding"]


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter from MDX/markdown files (Nextra-style)."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :]
    return text


def chunk_text(text: str, target_chars: int = 1500, overlap: int = 200) -> list[str]:
    """Paragraph-aware chunker.

    Splits on blank lines and accumulates paragraphs until reaching the
    target size. Carries an overlap from the tail of the previous chunk
    so context isn't lost at boundaries.
    """
    paras = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for p in paras:
        # Hard-split paragraphs that are themselves bigger than target_chars
        if len(p) > target_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(p), target_chars):
                chunks.append(p[i : i + target_chars])
            continue

        if not current or len(current) + len(p) + 2 <= target_chars:
            current = (current + "\n\n" + p).strip() if current else p
        else:
            chunks.append(current)
            tail = current[-overlap:] if overlap and len(current) > overlap else ""
            current = (tail + "\n\n" + p).strip()
    if current:
        chunks.append(current)
    return chunks


def discover_docs(roots: Iterable[Path]) -> list[Path]:
    """Recursively find every .md and .mdx file under any of the given roots."""
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            log(f"WARNING: docs path {root} does not exist, skipping")
            continue
        for ext in ("*.md", "*.mdx"):
            found.extend(sorted(root.rglob(ext)))
    return found


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def ensure_schema() -> None:
    """Idempotent schema creation. Safe to call on every startup.

    NOTE: this function does NOT create the pgvector extension. That happens
    in the postgres `initialScript` defined in flake.nix because creating
    extensions requires postgres superuser privileges, which the application
    role (peer-auth as `supportagent`) does not have. The application role
    only owns the database (via `ensureDBOwnership = true`) and can create
    its own tables and indexes.

    See `openmesh-cli/ENGINEERING/PIPELINE-LESSONS.md` Lesson #5.
    """
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS chunks (
                id SERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                chunk_index INT NOT NULL,
                content TEXT NOT NULL,
                embedding vector({EMBED_DIM})
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS chunks_embedding_idx "
            "ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )
        conn.commit()


def ingest_docs() -> int:
    """Read every .md/.mdx under DOCS_PATHS, chunk, embed, replace in postgres."""
    log(f"ingesting from: {[str(p) for p in DOCS_PATHS]}")
    files = discover_docs(DOCS_PATHS)
    log(f"found {len(files)} markdown files")

    if not files:
        log("WARNING: no docs found — agent will be useless. Check DOCS_PATHS.")
        return 0

    rows: list[tuple[str, int, str, str]] = []
    for path in files:
        try:
            text = strip_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            log(f"  skipping {path}: {e}")
            continue
        chunks = chunk_text(text)
        # Use a stable, human-readable source label that includes the
        # last two path segments so multi-source corpora are
        # disambiguated in citations.
        label = "/".join(path.parts[-2:])
        log(f"  {label}: {len(text):>6} chars -> {len(chunks)} chunks")
        for i, chunk in enumerate(chunks):
            try:
                emb = embed(chunk)
            except Exception as e:  # noqa: BLE001
                log(f"    embedding failed for chunk {i} of {label}: {e}")
                continue
            if len(emb) != EMBED_DIM:
                log(f"    WARN: embedding dim {len(emb)} != configured {EMBED_DIM}")
            rows.append((label, i, chunk, vec_to_pg(emb)))

    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE chunks RESTART IDENTITY")
        if rows:
            execute_values(
                cur,
                "INSERT INTO chunks (source, chunk_index, content, embedding) "
                "VALUES %s",
                rows,
                template="(%s, %s, %s, %s::vector)",
            )
        conn.commit()
    log(f"ingested {len(rows)} chunks total")
    return len(rows)


# ---------------------------------------------------------------------------
# Retrieval + chat
# ---------------------------------------------------------------------------


def retrieve(query: str, k: int = TOP_K) -> list[dict]:
    qvec = embed(query)
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source, chunk_index, content, embedding <=> %s::vector AS distance "
            "FROM chunks ORDER BY distance LIMIT %s",
            (vec_to_pg(qvec), k),
        )
        return [
            {
                "source": row[0],
                "chunk_index": row[1],
                "content": row[2],
                "distance": float(row[3]),
            }
            for row in cur.fetchall()
        ]


def build_prompt(query: str, chunks: list[dict]) -> str:
    system = SYSTEM_PROMPT_TEMPLATE.format(brand=BRAND_NAME)
    context = "\n\n---\n\n".join(
        f"[{c['source']}]\n{c['content']}" for c in chunks
    )
    return f"{system}{context}\n\nUSER QUESTION: {query}\n\nANSWER:"


def chat(query: str) -> dict:
    chunks = retrieve(query)
    prompt = build_prompt(query, chunks)
    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": CHAT_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 250},
        },
        timeout=300,
    )
    r.raise_for_status()
    answer = r.json().get("response", "").strip()
    return {
        "answer": answer,
        "sources": [
            {"source": c["source"], "chunk_index": c["chunk_index"], "distance": c["distance"]}
            for c in chunks
        ],
    }


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/api/health")
def health():
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM chunks")
        chunk_count = cur.fetchone()[0]
    return jsonify(
        {
            "status": "ok",
            "brand": BRAND_NAME,
            "chat_model": CHAT_MODEL,
            "embed_model": EMBED_MODEL,
            "docs_paths": [str(p) for p in DOCS_PATHS],
            "chunks_loaded": chunk_count,
        }
    )


@app.route("/api/chat", methods=["POST"])
def chat_endpoint():
    """Non-streaming JSON chat endpoint (kept for compatibility)."""
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "empty query"}), 400
    if len(query) > 2000:
        return jsonify({"error": "query too long (max 2000 chars)"}), 400
    try:
        return jsonify(chat(query))
    except Exception as e:  # noqa: BLE001
        log(f"chat error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat/stream", methods=["POST"])
def chat_stream_endpoint():
    """Server-Sent Events streaming endpoint.

    Sends three event types:
        sources: <json>     once at the start, the retrieved chunks
        token:   <text>     repeatedly, individual model output tokens
        done:    {}         when the model finishes
        error:   <text>     if anything fails

    The frontend can render tokens as they arrive instead of waiting for the
    full response — much better UX for slow CPU LLMs.
    """
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "empty query"}), 400
    if len(query) > 2000:
        return jsonify({"error": "query too long (max 2000 chars)"}), 400

    def generate():
        try:
            chunks = retrieve(query)
            sources_payload = json.dumps(
                [
                    {
                        "source": c["source"],
                        "chunk_index": c["chunk_index"],
                        "distance": c["distance"],
                    }
                    for c in chunks
                ]
            )
            yield f"event: sources\ndata: {sources_payload}\n\n"

            prompt = build_prompt(query, chunks)
            with requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": CHAT_MODEL,
                    "prompt": prompt,
                    "stream": True,
                    "options": {"temperature": 0.3, "num_predict": 250},
                },
                stream=True,
                timeout=600,
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    token = obj.get("response", "")
                    if token:
                        # Escape newlines for SSE format (multi-line data
                        # would otherwise be parsed as separate fields)
                        safe = token.replace("\\", "\\\\").replace("\n", "\\n")
                        yield f"event: token\ndata: {safe}\n\n"
                    if obj.get("done"):
                        yield "event: done\ndata: {}\n\n"
                        return
        except Exception as e:  # noqa: BLE001
            log(f"chat stream error: {e}")
            err = json.dumps({"error": str(e)})
            yield f"event: error\ndata: {err}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tells nginx not to buffer
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def main() -> int:
    log(f"{BRAND_NAME} starting up")
    log(f"  chat model:  {CHAT_MODEL}")
    log(f"  embed model: {EMBED_MODEL}")
    log(f"  docs paths:  {[str(p) for p in DOCS_PATHS]}")

    wait_for(check_db, "postgres")
    wait_for(check_ollama, "ollama")
    ensure_schema()
    ingest_docs()

    log(f"listening on {BIND_HOST}:{PORT}")
    app.run(host=BIND_HOST, port=PORT, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
