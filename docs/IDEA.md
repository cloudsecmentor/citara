# PRD: Hermes Knowledge Vault

## 1. Product summary

Hermes Knowledge Vault is an open-source, personal-first knowledge system for storing, indexing, searching, and citing a user’s private knowledge.

The primary frontend is not a traditional web app. The primary frontend is a personal agent, called Hermes, which interacts with the system through MCP tools. A lightweight web UI should still exist for testing, debugging, uploads, reviewing sources, inspecting ingestion jobs, and experimenting with retrieval quality.

The system must support personal local use first, but the architecture must be shaped so it can later become a hosted multi-tenant service.

Core promise:

> A private AI-searchable knowledge vault for documents, books, screenshots, notes, web pages, podcasts, transcripts, and other sources — with reliable citations back to the original source.

## 2. Important source preservation rule

The system must distinguish between uploaded files and external linked sources.

For uploaded files:

* Preserve the original file.
* Never store only extracted text.
* The original file is the canonical source of truth.
* Extracted text, chunks, embeddings, OCR output, and summaries are derived artifacts.

For external links:

* Store the canonical source URL.
* Store source metadata.
* Store extracted/transcribed text.
* Store timestamp/page/section anchors when available.
* Do not download or permanently store the original media file by default.
* For podcasts, do not store the audio file by default. Store the podcast URL, episode URL, transcript, timestamps, speaker labels if available, and timestamped citation links when possible.
* The system may optionally cache temporary media during transcription, but temporary files must be removable after processing.

For podcast citations:

* Each cited transcript segment must include:

  * podcast/show title
  * episode title
  * original episode URL
  * start timestamp
  * end timestamp when available
  * transcript text
  * generated timestamp URL when supported by the source platform
  * fallback citation if timestamp URL generation is not supported

Example citation:

```text
Source: Example Podcast
Episode: “The History of X”
Timestamp: 00:14:32–00:15:08
Link: original_episode_url?t=872
Quote: “...”
```

Timestamp link generation must be provider-aware. Different platforms may use different timestamp URL patterns. If the provider is unknown, show the original episode URL plus the timestamp as plain text.

## 3. Goals

### 3.1 User goals

The user should be able to:

1. Give Hermes a source:

   * uploaded PDF
   * image/photo of a page
   * screenshot
   * markdown/text note
   * podcast episode URL
   * podcast website URL
   * podcast RSS feed
   * web article URL

2. Ask Hermes to ingest the source.

3. Ask questions across:

   * all sources
   * selected collections
   * selected source types
   * selected books/podcasts/documents
   * selected date ranges

4. Receive answers with citations.

5. Open or inspect cited evidence.

6. Use a lightweight UI to review:

   * sources
   * transcripts
   * chunks
   * failed ingestion jobs
   * citations
   * search results

7. Run locally for personal use.

8. Later migrate to a multi-tenant hosted service without rewriting the core architecture.

## 4. Non-goals for MVP

Do not build these in the first version:

* Full mobile app
* Team collaboration
* Enterprise SSO
* Complex billing
* Full Notion/Obsidian replacement
* PDF/book ingestion
* Screenshot/image ingestion
* General web article ingestion
* Perfect handwriting OCR
* Universal support for every podcast platform
* Perfect podcast timestamp URLs for every provider
* Large-scale enterprise permission mirroring
* Real-time collaborative editing
* Social sharing

## 4.1 Current implementation priority

For the first working vertical slice, prioritize controlled text-like sources before broad document and web ingestion.

Build first:

* markdown/text notes
* normalized transcript fixtures
* podcast transcript ingestion
* podcast URL/RSS discovery after fixture-backed transcript handling works
* keyword search and context packs with citations over those sources

Move later:

* PDFs/books
* screenshots/images/OCR
* general web articles

Reason:

The first implementation should prove the source/chunk/search/citation architecture with deterministic fixtures before adding extraction-heavy sources. PDF parsing, OCR, and general web extraction introduce provider variability, flaky tests, and broader dependency choices that should not block the core ingestion design.

## 5. Primary interface strategy

The system must expose two interfaces over the same core logic:

### 5.1 MCP interface

Primary interface for Hermes.

Use FastMCP to expose tools such as:

* `add_source`
* `ingest_url`
* `ingest_podcast`
* `search_knowledge`
* `retrieve_context_pack`
* `get_source`
* `get_citation`
* `list_sources`
* `list_collections`
* `get_ingestion_job_status`
* `retry_ingestion_job`
* `delete_source`

The MCP server should be usable in two modes:

1. Local STDIO mode
   For personal/local use. Hermes can start the MCP server when needed.

2. HTTP MCP mode
   For future hosted or always-on service mode.

### 5.2 FastAPI interface

Secondary but important interface.

Use FastAPI for:

* lightweight web UI
* file upload
* source review
* ingestion job inspection
* local admin tools
* OpenAPI documentation
* future SaaS service API
* health checks
* metrics/debug endpoints
* webhook-style integrations later

FastAPI should call the same core services as the MCP server.

Do not duplicate logic between FastAPI and FastMCP.

## 6. Recommended backend structure

Use a core Python package with thin adapters.

```text
hermes_knowledge/
  core/
    ingestion/
    extraction/
    transcription/
    chunking/
    embeddings/
    retrieval/
    citations/
    sources/
    jobs/
    tenants/
    config/
  adapters/
    mcp/
      server.py
      tools.py
    api/
      main.py
      routes/
    cli/
      main.py
  ui/
    lightweight_frontend/
  workers/
    worker.py
  storage/
    db/
    object_store/
  tests/
```

Core rule:

```text
Business logic lives in core/.
FastAPI and FastMCP are adapters only.
```

## 7. Architecture overview

```text
Hermes Personal Agent
        |
        v
FastMCP Server
        |
        v
Hermes Knowledge Core
        |
        |-------------------------------
        |                              |
Lightweight FastAPI UI/API             |
        |                              |
        v                              v
Postgres + pgvector              Object Storage
metadata, jobs, chunks,          uploaded PDFs/images,
embeddings, citations            source snapshots if allowed
        |
        v
Ingestion Worker
OCR, parsing, podcast discovery,
transcription, chunking, embeddings
```

## 8. Deployment modes

### 8.1 Local personal mode

Use Docker Compose.

Services:

```text
postgres
api
mcp
worker
frontend
```

For very lightweight local mode, MCP may run on demand through STDIO, but the database and worker must be stable enough to handle long-running ingestion jobs.

### 8.2 Local agent-only mode

Hermes starts the MCP server when needed.

This mode is good for:

* search
* retrieval
* citation lookup
* adding simple URLs
* checking job status

This mode is not ideal for long-running transcription/OCR unless a separate worker is already running or the task is explicitly run inline.

### 8.3 Future SaaS mode

Use the same core logic.

Changes needed later:

* hosted Postgres
* S3-compatible object storage
* real tenant/user auth
* persistent workers
* queue system
* monitoring
* billing/quotas
* tenant isolation
* audit logs

## 9. Multi-tenant readiness

Even in local mode, every table must include tenant/workspace awareness.

Minimum required identifiers:

```text
tenant_id
user_id
workspace_id
collection_id
source_id
job_id
```

In local mode:

```text
tenant_id = "local"
user_id = "owner"
workspace_id = "default"
```

Never hardcode single-user assumptions into the data model.

## 10. Data model

### 10.1 Tenant

Represents one local installation now, future account/company later.

Fields:

```text
id
name
created_at
updated_at
```

### 10.2 User

Represents the owner or future team member.

Fields:

```text
id
tenant_id
email nullable
display_name
role
created_at
updated_at
```

### 10.3 Collection

A logical grouping of sources.

Examples:

```text
Bible study
Psychology books
Podcasts
Medical notes
Work documentation
```

Fields:

```text
id
tenant_id
user_id
name
description
created_at
updated_at
```

### 10.4 Source

Represents the original knowledge source.

Fields:

```text
id
tenant_id
user_id
collection_id nullable
source_type
title
author nullable
canonical_url nullable
provider nullable
external_id nullable
original_asset_id nullable
status
language nullable
metadata_json
created_at
updated_at
```

Allowed `source_type` values:

```text
pdf
image
screenshot
epub
markdown
text_note
web_article
podcast_show
podcast_episode
podcast_transcript
rss_feed
youtube_transcript
unknown_url
```

### 10.5 Asset

Represents stored local files.

Used for uploaded files and optional snapshots.

Fields:

```text
id
tenant_id
source_id
asset_type
storage_uri
mime_type
size_bytes
checksum
created_at
```

Important:

* Uploaded PDFs/images must have an asset.
* External podcast episodes should not have an audio asset by default.
* Temporary assets should have a cleanup policy.

### 10.6 TranscriptSegment

Used for podcasts, videos, and any time-based media.

Fields:

```text
id
tenant_id
source_id
start_ms
end_ms nullable
speaker nullable
text
confidence nullable
metadata_json
created_at
```

### 10.7 Page

Used for PDFs, books, screenshots, scanned pages.

Fields:

```text
id
tenant_id
source_id
page_number nullable
image_asset_id nullable
text
ocr_confidence nullable
metadata_json
created_at
```

### 10.8 Chunk

Used for retrieval.

Fields:

```text
id
tenant_id
source_id
page_id nullable
transcript_segment_id nullable
chunk_index
text
heading nullable
start_char nullable
end_char nullable
start_ms nullable
end_ms nullable
metadata_json
created_at
```

### 10.9 Embedding

Fields:

```text
id
tenant_id
chunk_id
embedding_model
vector
created_at
```

### 10.10 Citation

Fields:

```text
id
tenant_id
source_id
chunk_id
citation_type
label
canonical_url nullable
timestamp_url nullable
page_number nullable
start_ms nullable
end_ms nullable
quote_text
metadata_json
created_at
```

Allowed `citation_type` values:

```text
page
timestamp
section
url
quote
```

### 10.11 IngestionJob

Fields:

```text
id
tenant_id
user_id
source_id nullable
job_type
status
input_json
result_json nullable
error_message nullable
created_at
started_at nullable
finished_at nullable
```

Allowed `status` values:

```text
queued
running
succeeded
failed
cancelled
```

## 11. Ingestion pipeline

### 11.1 General pipeline

Every source should go through this pipeline:

```text
1. Receive source input
2. Classify source type
3. Create ingestion job
4. Discover metadata
5. Fetch or extract content
6. Normalize content
7. Create source record
8. Create pages/transcript segments/raw text records
9. Chunk content
10. Generate embeddings
11. Create citation anchors
12. Mark job as succeeded or failed
```

### 11.2 Ingestion input types

The ingestion system must support these input shapes:

```json
{
  "input_type": "url",
  "url": "https://example.com/podcast/episode-1",
  "collection_id": "optional",
  "tags": ["optional"]
}
```

```json
{
  "input_type": "file",
  "file_path": "/uploads/file.pdf",
  "collection_id": "optional",
  "tags": ["optional"]
}
```

```json
{
  "input_type": "text",
  "title": "My note",
  "text": "Note content",
  "collection_id": "optional"
}
```

## 12. Podcast ingestion skill

The podcast ingestion skill is a first-class skill.

The user should be able to give Hermes:

* a podcast website URL
* a podcast show page
* a podcast episode URL
* an RSS feed URL
* a page containing embedded podcast players
* a transcript page

Hermes should call the ingestion tool and the backend should discover the best available source.

### 12.1 Podcast ingestion flow

```text
User gives URL
        |
        v
Classify URL
        |
        |-- RSS feed?
        |-- episode page?
        |-- show page?
        |-- transcript page?
        |-- embedded player page?
        v
Discover podcast metadata
        |
        v
Discover episodes
        |
        v
For selected episode(s):
  - find transcript if available
  - otherwise find audio URL if allowed for temporary processing
  - transcribe audio if no transcript exists
  - store transcript segments with timestamps
  - create chunks
  - create embeddings
  - create timestamp citations
```

### 12.2 Podcast parser responsibilities

The parser should attempt, in order:

1. Detect RSS feed link in page metadata.
2. Parse RSS feed if available.
3. Extract show title, episode title, publish date, description, episode URL, audio enclosure URL.
4. Search page for transcript links.
5. Extract visible transcript if available.
6. Detect structured data such as JSON-LD if present.
7. Detect embedded players.
8. Use provider-specific adapters where needed.
9. Fall back to generic HTML extraction.
10. If no transcript exists, optionally transcribe from temporary audio.

### 12.3 Podcast metadata fields

For podcast shows:

```text
show_title
show_description
publisher
rss_url
website_url
language
image_url
```

For podcast episodes:

```text
episode_title
episode_description
episode_url
audio_url nullable
transcript_url nullable
publish_date nullable
duration nullable
episode_number nullable
season_number nullable
guid nullable
```

### 12.4 Transcript handling

If transcript exists:

* fetch transcript
* preserve transcript source URL
* parse timestamps if available
* normalize timestamps into milliseconds
* preserve speaker labels if available
* store transcript segments

If transcript does not exist:

* optionally download audio temporarily
* transcribe with selected transcription provider
* segment transcript by timestamp
* store transcript segments
* delete temporary audio after processing unless explicit caching is enabled

### 12.5 Timestamp citation generation

Implement provider-specific timestamp URL builders.

Interface:

```python
class TimestampLinkBuilder:
    def supports(self, source: Source) -> bool:
        ...

    def build(self, canonical_url: str, start_ms: int) -> str | None:
        ...
```

Fallback behavior:

```text
If timestamp URL cannot be generated:
  show canonical episode URL
  show timestamp as text
```

Example fallback citation:

```text
Episode URL: https://example.com/episode
Timestamp: 00:14:32
```

## 13. Retrieval

The retrieval system must support hybrid search.

Minimum retrieval methods:

```text
keyword search
semantic vector search
metadata filtering
collection filtering
source filtering
source_type filtering
date filtering
```

A retrieval result must include:

```text
chunk_id
source_id
source_title
source_type
text
score
page_number nullable
start_ms nullable
end_ms nullable
canonical_url nullable
timestamp_url nullable
citation_label
```

## 14. Context pack

Hermes should be able to request a compact context pack.

Tool:

```text
retrieve_context_pack
```

Input:

```json
{
  "query": "What does my material say about procrastination?",
  "collection_ids": ["optional"],
  "source_ids": ["optional"],
  "source_types": ["optional"],
  "max_chunks": 8,
  "include_citations": true
}
```

Output:

```json
{
  "query": "...",
  "chunks": [
    {
      "chunk_id": "...",
      "text": "...",
      "citation": {
        "label": "Podcast Name, Episode Title, 00:14:32",
        "source_url": "...",
        "timestamp_url": "...",
        "page_number": null,
        "start_ms": 872000,
        "end_ms": 908000
      }
    }
  ]
}
```

## 15. Citation requirements

Every answer generated from knowledge vault content must be source-grounded.

The system must not return vague citations like:

```text
Source: uploaded document
```

Instead, it must return precise citations:

For PDFs/books:

```text
Book title, page 42
```

For podcast episodes:

```text
Podcast title, episode title, 00:14:32
```

For web articles:

```text
Article title, section heading, URL
```

For screenshots/images:

```text
Image title, OCR region/page if available
```

## 16. MCP tools

### 16.1 `ingest_url`

Purpose:

Ingest a URL, including podcast websites, podcast episodes, RSS feeds, transcripts, and web articles.

Input:

```json
{
  "url": "string",
  "collection_id": "string | null",
  "mode": "auto | podcast | web_article | rss",
  "transcribe_if_needed": true,
  "store_original_media": false
}
```

Output:

```json
{
  "job_id": "string",
  "status": "queued",
  "message": "Ingestion job created"
}
```

### 16.2 `ingest_podcast`

Purpose:

Specialized podcast ingestion.

Input:

```json
{
  "url": "string",
  "episode_limit": 1,
  "episode_filter": "latest | all | title_match | date_range",
  "title_query": "string | null",
  "transcribe_if_needed": true,
  "store_audio_file": false
}
```

Output:

```json
{
  "job_id": "string",
  "detected_type": "rss_feed | show_page | episode_page | transcript_page | unknown",
  "status": "queued"
}
```

### 16.3 `get_ingestion_job_status`

Input:

```json
{
  "job_id": "string"
}
```

Output:

```json
{
  "job_id": "string",
  "status": "queued | running | succeeded | failed",
  "progress": 0.0,
  "source_id": "string | null",
  "error_message": "string | null"
}
```

### 16.4 `search_knowledge`

Input:

```json
{
  "query": "string",
  "collection_ids": ["string"],
  "source_types": ["podcast_episode", "pdf"],
  "limit": 10
}
```

Output:

```json
{
  "results": [
    {
      "chunk_id": "string",
      "source_title": "string",
      "text": "string",
      "citation_label": "string",
      "source_url": "string | null",
      "timestamp_url": "string | null",
      "score": 0.82
    }
  ]
}
```

### 16.5 `retrieve_context_pack`

Purpose:

Return curated context for Hermes to use in an answer.

Input:

```json
{
  "query": "string",
  "limit": 8,
  "include_quotes": true,
  "include_citations": true
}
```

Output:

```json
{
  "context_pack": [
    {
      "text": "string",
      "citation": "string",
      "source_url": "string | null",
      "timestamp_url": "string | null"
    }
  ]
}
```

### 16.6 `list_sources`

Input:

```json
{
  "collection_id": "string | null",
  "source_type": "string | null",
  "limit": 50
}
```

Output:

```json
{
  "sources": [
    {
      "source_id": "string",
      "title": "string",
      "source_type": "string",
      "status": "string",
      "canonical_url": "string | null"
    }
  ]
}
```

### 16.7 `get_source`

Input:

```json
{
  "source_id": "string"
}
```

Output:

```json
{
  "source_id": "string",
  "title": "string",
  "source_type": "string",
  "canonical_url": "string | null",
  "metadata": {},
  "citations_available": true
}
```

## 17. Lightweight frontend

The frontend is secondary.

Purpose:

* test ingestion
* upload files
* add URLs
* inspect source records
* inspect transcripts
* inspect chunks
* inspect citations
* inspect retrieval results
* compare keyword/vector/hybrid search
* retry failed jobs

The frontend should not try to become the main user experience.

Pages:

```text
/
Sources
Source detail
Add source
Ingestion jobs
Search playground
Context pack playground
Settings
```

## 18. Storage strategy

### 18.1 Local mode

Use:

```text
Postgres + pgvector
local filesystem or MinIO-compatible storage
```

### 18.2 Future hosted mode

Use:

```text
managed Postgres
S3-compatible object storage
queue system
worker pool
```

### 18.3 Storage abstraction

All file/object operations must go through an object storage abstraction.

Interface:

```python
class ObjectStore:
    def put(self, key: str, data: bytes, content_type: str) -> str:
        ...

    def get(self, uri: str) -> bytes:
        ...

    def delete(self, uri: str) -> None:
        ...
```

Do not spread direct filesystem paths across the codebase.

## 19. Ingestion worker

Long-running ingestion should be handled by a worker, not by the agent process.

Worker responsibilities:

* process queued jobs
* fetch URLs
* parse feeds/pages
* run OCR
* run transcription
* chunk content
* generate embeddings
* create citations
* update job status

For MVP, a simple database-backed job loop is acceptable.

Future version may replace this with Redis/RQ/Celery/Temporal/etc.

## 20. Configuration

Use environment variables.

Required configuration:

```text
DATABASE_URL
OBJECT_STORE_TYPE=local|s3
OBJECT_STORE_PATH
EMBEDDING_PROVIDER
EMBEDDING_MODEL
TRANSCRIPTION_PROVIDER
OCR_PROVIDER
DEFAULT_TENANT_ID=local
DEFAULT_USER_ID=owner
```

Optional configuration:

```text
OPENAI_API_KEY
LOCAL_EMBEDDING_MODEL_PATH
WHISPER_MODEL
MAX_UPLOAD_SIZE_MB
ALLOW_TEMP_AUDIO_DOWNLOAD=true
STORE_EXTERNAL_MEDIA_BY_DEFAULT=false
```

## 21. Security and privacy

Default privacy posture:

* Local-first
* No external media storage by default
* No podcast audio persistence by default
* Store only links, metadata, transcript, chunks, embeddings, and citations for external podcasts
* Preserve uploaded original files
* Support deletion of a source and all derived artifacts
* Support export later

Security requirements:

* Never expose local API publicly by default.
* Require auth for future hosted mode.
* Tenant isolation must be enforced in all queries.
* Every query must filter by `tenant_id`.
* Do not allow arbitrary filesystem reads through MCP tools.
* Validate URLs before fetching.
* Add allow/deny rules later for private network URL fetching.

## 22. Acceptance criteria for MVP

### MVP must support:

1. Start with Docker Compose.
2. Add markdown/text notes.
3. Preserve source text and metadata.
4. Chunk and embed ingested text.
5. Add normalized podcast transcript fixtures.
6. Store transcript with timestamps.
7. Add a podcast episode URL after fixture-backed transcript ingestion works.
8. Detect episode metadata.
9. Find transcript if available.
10. If no transcript exists, optionally transcribe from temporary audio.
11. Do not store podcast audio by default.
12. Search across text notes and podcast transcripts.
13. Return citations for text notes.
14. Return citations with timestamps for podcast transcripts.
15. Expose MCP tools for Hermes.
16. Expose FastAPI endpoints for lightweight UI.
17. Show sources and jobs in lightweight UI.
18. Include `tenant_id` and `user_id` in all core tables.
19. Support local single-user mode using `tenant_id=local`.
20. All business logic must be in the core package, not duplicated between MCP and FastAPI.

Deferred MVP+ acceptance criteria:

* Upload a PDF.
* Preserve the original PDF.
* Extract PDF text.
* Return citations with page numbers for PDF.
* Ingest screenshots/images with OCR.
* Ingest general web articles.

## 23. Suggested first implementation milestones

### Milestone 1: Skeleton

* repository structure
* config
* database connection
* migrations
* source/job models
* FastAPI health endpoint
* FastMCP server with one test tool
* Docker Compose

### Milestone 2: Basic source ingestion

* upload text/markdown
* create source
* chunk text
* embed chunks
* search chunks
* return citations

### Milestone 3: Context packs and citations

* build `retrieve_context_pack`
* return compact chunks for Hermes
* include citations for text notes
* include timestamp citations for transcript segments
* add fixture-backed tests for citation formatting

### Milestone 4: Podcast URL ingestion

* accept podcast URL
* classify URL
* discover RSS/transcript/audio
* store source metadata
* parse transcript if available
* store transcript segments
* create timestamp citations

### Milestone 5: Podcast transcription

* temporary audio fetch
* transcribe
* segment transcript
* delete temporary audio by default
* create chunks and embeddings

### Milestone 6: Hermes-ready MCP

* `ingest_url`
* `ingest_podcast`
* `search_knowledge`
* `retrieve_context_pack`
* `get_source`
* `get_ingestion_job_status`

### Milestone 7: Lightweight UI

* add source page
* source list
* source detail
* transcript viewer
* job viewer
* search playground

### Milestone 8: PDF ingestion

* upload PDF
* store original file
* extract text by page
* create page records
* create chunks
* page-level citations

### Milestone 9: Screenshots, images, and web articles

* screenshot/image upload
* OCR extraction
* OCR-region citations where available
* general web article extraction
* URL/section citations for web articles

## 24. Coding rules for the agent

When implementing:

1. Keep core logic framework-independent.
2. Do not put ingestion logic directly inside FastAPI route handlers.
3. Do not put ingestion logic directly inside MCP tool functions.
4. MCP tools should call core services.
5. FastAPI routes should call core services.
6. All DB queries must include `tenant_id`.
7. Do not store external podcast audio by default.
8. Preserve uploaded files.
9. Store canonical URLs for external sources.
10. Store timestamp anchors for podcast transcript segments.
11. Return structured JSON from all tools.
12. Add tests for ingestion classification, podcast parsing, chunking, and citation generation.
13. Keep MVP simple; do not build enterprise features yet.

## 25. Example end-to-end scenario

User says to Hermes:

```text
Please ingest this podcast episode:
https://example.com/podcast/episode-123
```

Hermes calls:

```text
ingest_podcast(url="https://example.com/podcast/episode-123")
```

System creates job:

```json
{
  "job_id": "job_123",
  "status": "queued"
}
```

Worker processes job:

```text
- classifies URL as podcast episode
- discovers episode title
- finds transcript URL
- parses transcript timestamps
- stores transcript segments
- chunks transcript
- generates embeddings
- creates timestamp citations
```

Later user asks Hermes:

```text
What did that episode say about emotional regulation?
```

Hermes calls:

```text
retrieve_context_pack(query="emotional regulation", source_types=["podcast_episode"])
```

System returns:

```json
{
  "context_pack": [
    {
      "text": "The speaker explains that emotional regulation starts with noticing the body response before reacting.",
      "citation": "Example Podcast, Episode 123, 00:18:42",
      "source_url": "https://example.com/podcast/episode-123",
      "timestamp_url": "https://example.com/podcast/episode-123?t=1122"
    }
  ]
}
```

Hermes answers with cited evidence.

## 26. Final architecture decision

Use both FastAPI and FastMCP.

FastMCP is the primary agent interface.

FastAPI is the service/debug/UI/future-SaaS interface.

The long-term product is not a web app first. It is a knowledge backend for Hermes and other agents, with a lightweight UI for visibility and control.
