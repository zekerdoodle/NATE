# Nate Embedding & Semantic Search Specification

## 1. Objectives
- Replace the current keyword-based `search` tool with an embedding-powered semantic retrieval layer that returns precise, contextual snippets.
- Support the full `/docs` corpus (including tickets, SOPs, tech notes, CSV rosters, and JSON payloads) with deterministic chunking and traceability metadata (path + line span).
- Operate on CPU-only infrastructure while keeping operational cost low and throughput high enough for nightly re-indexing plus on-demand updates.

## 2. Embedding Model Decision
### 2.1 Evaluation Criteria
- **Search accuracy:** ability to surface precise procedural guidance from SOP-style documents.
- **Latency / throughput on CPU:** the server lacks GPUs, so inference must remain fast on CPU cores.
- **Cost profile:** prefer zero or minimal recurring cost; only adopt paid APIs if they provide a clear accuracy win.
- **Multilingual robustness:** handle occasional mixed English/Spanish documents.
- **Vector dimensionality:** balance storage footprint with semantic richness.

### 2.2 Candidate Snapshot (Nov 2025)
| Option | Access | Dim | Cost (USD / 1M tokens) | Notes |
| --- | --- | --- | --- | --- |
| OpenAI `text-embedding-3-small` | API | 1,536 | $0.02 | Strong general accuracy, but dependence on paid API; recent independent benchmarks place it mid-pack for retrieval accuracy (AIMultiple, Oct 2025).
| Cohere `embed-v4` | API | 1,024 | $0.12 | Higher cost; marginal accuracy gains vs. cheaper alternatives according to Document360 review (Jul 2025).
| Mistral `mistral-embed` | API | 1,024 | $0.10 | Top accuracy in AIMultiple 2025 benchmark but still paid.
| Voyage `voyage-3.5-lite` | API | 1,024 | $0.02 | Competitive accuracy, but API usage still incurs cost and adds latency.
| **BAAI `bge-m3` (open weights)** | Self-hosted | 1,024 | Free | 1.8B parameter multilingual model; top-tier open-source retrieval quality; runs acceptably on CPU with quantisation.
| **Nomic `nomic-embed-text-v1.5` (open weights)** | Self-hosted | 768 | Free | 137M parameter model; optimised for CPU inference (Ollama, 2025) and strong recall for knowledge-base style content.
| IBM `granite-embedding-107m` (GGUF) | Self-hosted | 768 | Free | Demonstrated 300 RPS short-query throughput on 6-core CPU (paulw.tokyo, Sep 2025); accuracy slightly below BGE/Nomic in most public benchmarks.

### 2.3 Final Selection: Hybrid Local Stack
- **Primary model:** `nomic-embed-text-v1.5` (0.768k dims). Reasons:
  - Optimised for CPU inference; easily hosted via `sentence-transformers` or Ollama.
  - Free to run; cost-effective while still outperforming legacy MiniLM baselines.
  - Handles both short prompts and long SOP-style paragraphs without re-ranking.
- **Fallback / future upgrade path:** `bge-m3` (quantised to 4-bit) for higher-recall multilingual search if future accuracy audits show gaps. Keep abstraction in `embed.py` so swapping models only changes the config.
- **Rationale against API-only models:** Paid APIs offer modest precision gains but impose ongoing cost, network dependency, and rate limits. Given our dataset’s size (< few GB) and the availability of robust open models, the marginal benefit does not justify recurring spend right now. If future quality reviews highlight misses, revisit `mistral-embed` as the first paid candidate.

## 3. Data Ingestion & Chunking Strategy
### 3.1 Supported File Types
- Markdown (`.md`), plain text (`.txt`), JSON (`.json`), CSV (`.csv`), log files.
- HTML/PDF are out of scope for phase 1 (flag in backlog).

### 3.2 Normalisation Pipeline
1. **Path filtering:** recurse `docs/` and `archive/` but skip `docs/tickets/*.json` older than 90 days unless explicitly refreshed (configurable).
2. **Text extraction:**
   - Markdown: strip front-matter, convert headings to inline markers (`## Heading ::`), collapse code blocks into fenced markers.
   - CSV: treat header row + each data row as joined sentences; skip large numeric columns.
   - JSON: flatten key paths using dot notation (`field.subfield: value`).
3. **Whitespace canonicalisation:** collapse multiple blank lines; remove zero-width / non-ASCII control characters.
4. **Chunking:**
   - Target 450 tokens (~1,800 characters) per chunk with 80-token overlap to preserve context.
   - Align chunk boundaries with sentence ends when possible using `nltk.sent_tokenize` or SpaCy sentenciser. Fallback to simple regex splitting.
   - Annotate each chunk with `start_line`, `end_line`, and `token_count` to allow precise snippet references.
5. **Metadata augmentation:** compute SHA-256 hash of the raw chunk text; store file mtime and chunk index for change detection.

### 3.3 Initial Embedding Bootstrapping
- `embed.py` gains a CLI (`python embed.py --rebuild`) that:
  1. Scans eligible files, generates chunks, and persists embeddings.
  2. Stores progress in a state DB (`data/embeddings.sqlite`) to resume after failures.
  3. Emits summary stats (total files, tokens, vectors, elapsed time).

## 4. Storage & Retrieval Architecture
### 4.1 Persistence
- **Vector store:** FAISS IndexFlatIP (cosine similarity) persisted to `data/vector.index`. This lightweight index performs well for ≤1M vectors on CPU and has zero external dependencies.
- **Metadata store:** SQLite database `data/embeddings.sqlite` with tables:
  - `files (id, path, checksum, mtime, size_bytes, last_indexed_at)`
  - `chunks (id, file_id, chunk_idx, start_line, end_line, token_count, text_hash, text)`
  - `embeddings (chunk_id, vector BLOB)` — stored as raw float32 array (FAISS also holds vectors; DB layer enables diffing and re-chunking).
- Keep chunk text in DB for quick snippet rendering; optionally gzip-compress for space.

### 4.2 Query Flow (`tools/search.py` Replacement)
1. Accept parameters `query`, `source`, `title`, `limit`, `min_score`, `return_content`.
2. Generate embedding for the query through the same model.
3. Use FAISS to fetch top `k_candidates = max(limit * 3, 20)` results.
4. Apply source/title filters using metadata prior to ranking.
5. Rescore with cosine similarity; discard results below `min_score` (default 0.25).
6. For each retained chunk:
   - Produce snippet by highlighting the top-matching sentence (via simple keyword highlight based on query terms) within the stored chunk text.
   - Return `path`, `score`, `start_line`, `end_line`, `snippet`, and `chunk_id`.
7. If no chunk passes threshold, respond with empty array and suggestions (e.g., reduce filters).

### 4.3 Response Shape Example
```json
{
  "query": "reset domain password",
  "results": [
    {
      "path": "docs/it_docs/How to Reset a Domain Password.txt",
      "chunk_id": "bbb9a8a7",
      "score": 0.82,
      "start_line": 12,
      "end_line": 38,
      "snippet": "...Navigate to Active Directory Users and Computers... right-click the account and select Reset Password...",
      "metadata": {
        "source": "documents",
        "updated_at": "2025-11-10T15:04:12Z"
      }
    }
  ],
  "result_count": 1,
  "limit": 5
}
```
- Snippets intentionally omit raw chunk text beyond ±200 characters around best-matching sentence to keep responses succinct.
- Model-facing schema mirrors current tool output, ensuring minimal change to `model_call.py`.

### 4.4 Semantic Thresholds & Limits
- Default cosine cutoff `min_score = 0.25`; allow override per query to widen/narrow results.
- Hard cap of 8 returned results to maintain prompt budget.
- If all scores fall <0.25 but >0.18, downranked chunks appear under `results_low_confidence` so the model can optionally inspect via `read_file`.

## 5. Embedding Lifecycle Management
### 5.1 Change Detection
- Monitor `mtime` and file `checksum` (SHA-256). If unchanged, skip re-chunking.
- If a file shrinks/grows by >20%, force full re-chunk to avoid orphaned chunk metadata.
- For CSV/JSON with frequent changes, allow per-directory override to always re-process.

### 5.2 Update Triggers
- **Initial bootstrap:** run `embed.py --rebuild` after deployment.
- **Scheduled refresh:** nightly cron (e.g., `schtasks` on Windows or systemd timer on Linux) invoking `embed.py --sync` which:
  - Scans for files with newer `mtime` than `last_indexed_at`.
  - Updates embeddings incrementally; rebuilds FAISS index from affected vectors only.
- **On-demand sync:** integrate with ticket ingester so when a new ticket JSON lands in `docs/tickets/`, trigger `embed.py --sync --paths docs/tickets/<id>.json`.
- **Manual CLI:** `embed.py --drop docs/it_docs/foo.txt` to remove retired docs.

### 5.3 Reliability Safeguards
- Write-ahead temp files during index rebuild to avoid corruption on interruption.
- Keep rolling 3-day snapshots of the index/DB (`data/backups/YYYYMMDD`).
- Provide dry-run mode (`--plan`) that reports pending actions without executing.

## 6. Integration Touchpoints
- `embed.py`: implement CLI, chunker, embedding runner, FAISS persistence, and change detection.
- `tools/search.py`: replace keyword search with vector retrieval, but keep function signature and error semantics so `model_call.py` requires minimal adjustment.
- `requirements.txt`: add `sentence-transformers`, `faiss-cpu`, `sqlite-utils`, `nltk` (for sentence splitting). Include install notes for Windows (`pip install faiss-cpu==1.8.0.post1`) and download of NLTK punkt data in bootstrap routine.
- `dev_docs/` (optional): create `embedding_playbook.md` capturing operational runbooks (out-of-scope for this spec).

## 7. Implementation Roadmap
1. **Scaffolding (est. 0.5 day)**
   - Flesh out `embed.py` with CLI argument parsing and logging.
   - Set up data directories (`data/`, `data/backups/`).
2. **Chunker & Normaliser (1 day)**
   - Implement file loaders per type, sentence-aware chunking, metadata hashing.
3. **Embedding Runner (0.5 day)**
   - Integrate `nomic-embed-text` via `sentence-transformers`.
   - Batch embeddings (256 chunks per batch) to maximise CPU throughput.
4. **Persistence Layer (0.5 day)**
   - Create SQLite schema and FAISS index builder.
5. **Search Tool Rewrite (0.75 day)**
   - Query embedding, FAISS lookup, snippet rendering, confidence thresholds.
6. **Testing & Validation (0.5 day)**
   - Unit tests for chunking, incremental updates, and search ranking (see `tests/test_ticket_pipeline.py` for fixture patterns).
7. **Operationalisation (0.25 day)**
   - Document CLI usage, add scheduled task template, seed NLTK models.

## 8. Open Questions & Future Enhancements
- Should we store embeddings for private/internal-only docs separately to support redaction toggles (align with "private mode" requirement)? Potential follow-up.
- Consider integrating a cross-encoder re-ranker (e.g., `bge-reranker-v2-m3`) once base retrieval is stable; adds ~30 ms per query.
- Explore storing vector norms to accelerate similarity scoring if the corpus grows beyond ~500k chunks.
- Add evaluation harness comparing current keyword search vs. embedding recall on curated query set.

---
Prepared: 2025-11-11
Maintainer: Nate Platform Team (GitHub Copilot draft)
