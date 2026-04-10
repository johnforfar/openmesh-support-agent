# Openmesh Support Agent

A RAG-powered docs assistant that helps developers and AI agents deploy
apps to sovereign Openmesh Xnodes via the [`om` CLI](https://github.com/johnforfar/openmesh-cli).

Live at: **https://chat.build.openmesh.cloud**

Runs entirely on a sovereign decentralized Xnode. No SaaS, no telemetry,
no OpenAI key. Local LLM, local vector database, local web server.

---

## What it does

You ask a question about the `om` CLI, deployment patterns, error codes,
NixOS containers, or Claude Code integration. The agent retrieves the most
relevant chunks from its document corpus, feeds them to a local
`llama3.2:1b` running on the same xnode, and returns an answer with source
citations.

The corpus is multi-source:

1. The **canonical CLI docs** from `johnforfar/openmesh-cli`
2. The **OpenxAI documentation** from `OpenxAI-Network/openxai-docs`
3. This repo's own `docs/` folder (instance-specific notes)

All three are pulled in as flake inputs at build time, so the corpus is
versioned with the deployment.

---

## How it was deployed

Two `om` commands. That's the whole story:

```bash
om app deploy support-agent \
  --flake github:johnforfar/openmesh-support-agent

om app expose support-agent \
  --domain chat.build.openmesh.cloud \
  --port 80
```

If you want to do the same thing for your own project, read
[OPENMESH-SKILLS.md](https://github.com/johnforfar/openmesh-cli/blob/main/OPENMESH-SKILLS.md)
in the openmesh-cli repo. It's the canonical guide to deploying anything
on a sovereign Xnode via Claude Code or any AI agent.

---

## Architecture

One nixos-container with four services:

```
┌─────────────────── nixos-container ────────────────────┐
│                                                        │
│   nginx :80                                            │
│   ├── /              → static frontend (one HTML file) │
│   └── /api/*         → 127.0.0.1:5000 (python backend) │
│                                                        │
│   python backend :5000 (Flask)                         │
│   ├── /api/health    → status + chunk count           │
│   └── /api/chat      → embed → retrieve → LLM         │
│           │                                            │
│           ├──→ postgres :5432 (pgvector)               │
│           │       table: chunks(source, content,       │
│           │                     embedding vector(768)) │
│           │                                            │
│           └──→ ollama  :11434                          │
│                  ├── llama3.2:1b      (chat, ~1.3GB)   │
│                  └── nomic-embed-text (embed, ~270MB)  │
│                                                        │
└────────────────────────────────────────────────────────┘
                          │
                          ▼
            xnode-manager reverse proxy
                  (TLS via ACME)
                          │
                          ▼
                chat.build.openmesh.cloud
```

**Memory budget:** ~5 GB total at runtime. Fits comfortably on the
8 vCPU / 16 GB Xnode.

**On startup the backend:**
1. Waits for postgres + ollama to come up
2. Creates schema if missing (`CREATE EXTENSION vector`,
   `CREATE TABLE chunks`)
3. Truncates `chunks` and re-ingests every `.md`/`.mdx` file under the
   configured `DOCS_PATHS` directories
4. Starts serving HTTP requests

Re-deploying the flake (with updated docs) always produces a fresh
corpus — no cache invalidation bugs.

---

## Repo layout

```
.
├── flake.nix                  # nixos-container module + flake inputs
├── flake.lock
├── backend/
│   └── app.py                 # ~330 lines: ingestion + retrieval + Flask
├── frontend/
│   └── index.html             # vanilla HTML+CSS+JS, no build step
├── docs/                      # this repo's local doc corpus
│   └── 01-openmesh-cli-quick.md
├── tests/
│   └── verify_deployment.py   # 16 end-to-end checks for live deployments
└── README.md
```

The Python backend is intentionally a single file (~330 lines) so anyone
who knows basic Python can read the whole RAG implementation in 10
minutes. No frameworks, no abstraction layers, no magic.

---

## Verifying a deployment

After `om app expose ...` succeeds, run the verification suite:

```bash
python tests/verify_deployment.py https://chat.build.openmesh.cloud
```

It checks:

- **Transport:** HTTPS reachable, TLS cert valid
- **Health:** `/api/health` returns ok with `chunks_loaded > 0`
- **Latency:** health endpoint < 5s, cold chat < 90s, warm chat < 60s
- **Input validation:** empty/oversize queries return 4xx
- **Knowledge:** 5 questions whose answers are in the docs must produce
  responses with the right keywords AND at least one cited source
- **Negative tests:** off-topic questions (Bitcoin price, write a poem)
  must be politely refused, not hallucinated

Exit code is 0 on full pass. Use `--json` for CI consumption.

---

## Reusing this for your own docs

The shortest path is **fork-and-edit**:

1. Fork this repo
2. Edit `flake.nix`:
   - Change the `inputs.openxai-docs` and `inputs.openmesh-cli-docs`
     entries to point at your own docs flakes (or just remove them and
     add files to `docs/`)
   - Change `BRAND_NAME` and the system prompt
3. Commit and push
4. Deploy:
   ```bash
   om app deploy my-docs --flake github:youruser/your-fork
   om app expose my-docs --domain support.yourproject.com --port 80
   ```

Or — and this is the actual point of the project — **don't fork at all.**
Use `om` directly on whatever app you're already building. The support
agent is one example; your own app is the actual goal.

---

## Why local LLMs over GPT-5 / Claude

Three reasons:

1. **Sovereignty.** Your docs and queries never leave your infrastructure.
   No vendor account, no API key, no terms of service that change next quarter.
2. **Cost.** A 1B parameter model on CPU is free at the margin. Hosted
   APIs are pennies per query but they add up — and stop working when
   the vendor rate-limits you the day before your hackathon demo.
3. **It actually works.** RAG with a small grounded model is competitive
   with much larger ungrounded models on docs Q&A. The model doesn't have
   to know the answer; it just has to summarise the chunks the retriever
   handed it.

When the corpus grows past what `nomic-embed-text` and `llama3.2:1b` can
handle, swap in `nomic-embed-text-v1.5` (still small) and `llama3.2:3b`
or `qwen2.5:7b`. The architecture doesn't change.

---

## Credits

- **Openmesh** — sovereign Xnode infrastructure
- **OpenxAI** — open-source AI alignment + the docs corpus that grounds half the agent
- **`om` CLI** — what makes the whole thing deployable in two commands ([repo](https://github.com/johnforfar/openmesh-cli))
- **ollama** — making local LLMs effortless
- **pgvector** — the simplest vector database that works
- **Claude Code** — the AI pair-programmer that built and deployed all of this

---

*Deployed with Openmesh CLI v2.0 via Claude Code — [John Forfar](https://github.com/johnforfar)*
