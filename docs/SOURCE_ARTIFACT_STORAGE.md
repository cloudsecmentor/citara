# Source artifact storage design

This document defines how Hermes Knowledge Vault should store raw source-transcript artifacts so the retrieval database can be deleted, rebuilt, rechunked, or re-embedded without repeating expensive transcription or rediscovery work.

## Goals

- Preserve enough source information to re-import any item into the retrieval database.
- Avoid storing large raw audio/video files by default.
- Use generic terminology that works for podcasts, books, YouTube channels, courses, sermons, lectures, interviews, and notes.
- Keep the retrieval database disposable: chunks, embeddings, and import jobs can be rebuilt from durable artifacts.
- Keep third-party content out of Git. Local artifacts live under ignored `data/` paths or an external object store.

## Core principle

Store generated or discovered transcripts as durable **source artifacts**. Treat Postgres rows, chunks, embeddings, and retrieval indexes as derived state.

```text
source discovery / metadata
        ↓
raw transcript artifacts
        ↓
normalized import payload
        ↓
Postgres sources / transcript_segments / chunks / embeddings
```

Raw media files are optional temporary cache files only. The default durable artifact is the transcript plus metadata, not GBs of audio/video.

## Repository boundary and configurable storage root

Source artifacts and live databases should **not** be committed to the repository.

The repository should contain:

- application code;
- schema migrations;
- documentation;
- tests;
- tiny synthetic or rights-cleared fixtures under `tests/fixtures/`.

The repository should not contain:

- personal or third-party transcript corpora;
- generated Whisper JSON for real sources;
- import payloads for real copyrighted sources;
- Postgres/SQLite databases;
- raw audio/video except tiny test fixtures, if ever needed.

Defaults should point outside the repository so real corpora are not accidentally committed. The recommended local default is a sibling `../hkb` directory:

```dotenv
SOURCE_ARTIFACT_ROOT=../hkb/source-artifacts
SOURCE_STATE_ROOT=../hkb/import-state
```

For a larger real personal corpus, configure an absolute source-artifact root in `.env`:

```dotenv
SOURCE_ARTIFACT_ROOT=/path/to/hkb/source-artifacts
SOURCE_STATE_ROOT=/path/to/hkb/import-state
```

or on a mounted/external volume:

```dotenv
SOURCE_ARTIFACT_ROOT=/Volumes/HKB/source-artifacts
SOURCE_STATE_ROOT=/Volumes/HKB/import-state
```

`.env` itself is ignored by Git. Commit only `.env.example` with safe defaults and comments.

Importer code should resolve artifact paths from `SOURCE_ARTIFACT_ROOT` instead of assuming any repository-relative `data/import-artifacts/sources` path. Repo-relative `./data/...` paths are still ignored by Git and can be used for throwaway experiments, but new generic source importers should default to `../hkb/...` or an explicit absolute path.

The supported organizer command is:

```bash
uv run python scripts/organize_source_artifacts.py
```

It writes `organization-manifest.json` beside the source-artifact and import-state roots, records source/target paths, byte sizes, and SHA-256 hashes, and rewrites known legacy state-file artifact references such as `data/import-artifacts/<tree>/pdf/<item>.pdf` to logical `source-artifacts://...` URIs.

Preview destructive artifact/database maintenance before executing it:

```bash
uv run python scripts/hkb_maintenance.py reset --dry-run \
  --remove-tree <source-tree-slug> \
  --reset-sqlite \
  --reset-docker-db
```

Then execute with `--yes` only after reviewing the printed actions.

## Generic source tree layout

Use a source-tree name that represents the collection, creator, channel, book, course, or show. Do not bake podcast-specific assumptions into the top-level layout.

```text
<SOURCE_STATE_ROOT>/
  <source-tree-slug>_pipeline_state.json

<SOURCE_ARTIFACT_ROOT>/
  <source-tree-slug>/
    source-tree.json
    items/
      <item-slug-or-id>/
        source.json
        transcript.raw.json
        transcript.normalized.json
        transcript.txt
        transcript.vtt
        import-payload.json
        import-result.json
```

Default development roots:

```text
SOURCE_ARTIFACT_ROOT=../hkb/source-artifacts
SOURCE_STATE_ROOT=../hkb/import-state
```

Examples with the default sibling root:

```text
../hkb/source-artifacts/bema/items/001-trust-the-story/
../hkb/source-artifacts/bibleproject/items/god-e1-god-or-gods/
../hkb/source-artifacts/tim-mackie-youtube/items/yt-dq1x45abcde/
../hkb/source-artifacts/surprised-by-hope/items/chapter-03/
../hkb/source-artifacts/personal-notes/items/daily-reflection/
```

Equivalent absolute-path examples:

```text
/path/to/hkb/source-artifacts/bema/items/001-trust-the-story/
/path/to/hkb/source-artifacts/tim-mackie-youtube/items/yt-dq1x45abcde/
/path/to/hkb/source-artifacts/surprised-by-hope/items/chapter-03/
```

Current podcast-specific paths such as `data/import-artifacts/podcasts/<slug>/` may continue to work during migration, but new generic pipelines should prefer `data/import-artifacts/sources/<source-tree-slug>/`.

## Naming conventions

| Concept | Meaning | Examples |
|---|---|---|
| `source-tree-slug` | Stable collection/creator/container name | `bema`, `bibleproject`, `tim-mackie-youtube`, `surprised-by-hope` |
| `item-slug-or-id` | Stable source item inside the tree | episode slug, YouTube video ID, chapter number, note slug |
| `source-tree.json` | Metadata shared by the collection | author/channel/show/book/course metadata |
| `source.json` | Metadata for one importable item | canonical URL, title, dates, provenance, duration |
| `transcript.raw.json` | Provider-native transcript output | Whisper JSON, downloaded transcript JSON, OCR JSON |
| `transcript.normalized.json` | HKB-normalized transcript segments | `start_ms`, `end_ms`, `speaker`, `text` |
| `transcript.txt` | Human-readable full transcript | debugging, diffing, manual review |
| `transcript.vtt` | Optional timestamped text format | playback, citation QA, portability |
| `import-payload.json` | Exact payload sent to HKB import API | repeatable re-import |
| `import-result.json` | API/importer result and source IDs | audit and dedupe |

## Required item metadata: `source.json`

Each item should have metadata that allows re-import without rediscovering the source page or feed.

```json
{
  "source_tree_slug": "bema",
  "source_tree_type": "podcast",
  "item_id": "bema-001",
  "item_type": "podcast_episode",
  "title": "Trust the Story",
  "author_or_creator": "BEMA Discipleship",
  "canonical_url": "https://www.bemadiscipleship.com/1",
  "published_at": "2016-01-01",
  "duration_seconds": 3721,
  "language": "en",
  "transcript_provenance": "whisper",
  "transcript_provider": "faster-whisper",
  "transcription_model": "large-v3",
  "artifact_version": 1,
  "external_ids": {
    "rss_guid": "...",
    "youtube_video_id": null,
    "isbn": null
  }
}
```

Use broad `source_tree_type` / `item_type` values rather than podcast-only terms. Suggested values:

| `source_tree_type` | Example `item_type` values |
|---|---|
| `podcast` | `podcast_episode` |
| `youtube_channel` | `youtube_video` |
| `book` | `book_chapter`, `book_section` |
| `course` | `lesson`, `lecture` |
| `website` | `web_page`, `article` |
| `notes` | `note` |
| `interview_series` | `interview` |

## Transcript artifacts

### `transcript.raw.json`

For Whisper-generated transcripts, keep the raw Whisper-style output as close as possible to the provider result:

```json
{
  "text": "Full transcript...",
  "language": "en",
  "duration": 3721.4,
  "segments": [
    {
      "id": 0,
      "start": 0.0,
      "end": 8.42,
      "text": "Welcome back..."
    }
  ]
}
```

For other providers, preserve their native fields instead of forcing them into the Whisper shape. The raw file is for auditability and future normalization improvements.

### `transcript.normalized.json`

This is the canonical generated artifact for HKB re-import. It should use milliseconds and stable segment order:

```json
{
  "schema": "hkb.transcript.normalized.v1",
  "language": "en",
  "segments": [
    {
      "segment_index": 0,
      "start_ms": 0,
      "end_ms": 8420,
      "speaker": null,
      "text": "Welcome back..."
    }
  ]
}
```

If no timestamps exist, generate approximate timestamps only when enough duration information is available. Mark the approximation in metadata:

```json
{
  "timestamps": {
    "mode": "approximate",
    "method": "proportional_character_offset"
  }
}
```

### `transcript.txt`

Store a plain-text transcript for review and diffing. It may be generated from normalized segments, but keeping it as a file makes manual inspection and backups easier.

### `transcript.vtt`

Store VTT when timestamps are available or generated. This is optional but useful for playback alignment and testing citation links.

## Import payload

`import-payload.json` should be the exact request body needed to recreate the source in HKB, normally for `POST /sources/transcript`.

It should include:

- title and source type;
- canonical URL;
- normalized segments;
- timestamp URLs when possible;
- metadata linking back to artifact paths/URIs;
- provenance fields such as transcript provider/model;
- stable external IDs for deduplication.

Example shape:

```json
{
  "title": "BEMA 001: Trust the Story",
  "canonical_url": "https://www.bemadiscipleship.com/1",
  "segments": [
    {
      "start_ms": 0,
      "end_ms": 8420,
      "speaker": null,
      "text": "Welcome back...",
      "timestamp_url": "https://www.bemadiscipleship.com/1?t=0"
    }
  ],
  "metadata": {
    "source_tree_slug": "bema",
    "source_tree_type": "podcast",
    "item_id": "bema-001",
    "item_type": "podcast_episode",
    "transcript_provenance": "whisper",
    "transcript_provider": "faster-whisper",
    "transcription_model": "large-v3",
    "artifact_uri": "data/import-artifacts/sources/bema/items/001-trust-the-story/transcript.raw.json",
    "normalized_transcript_uri": "data/import-artifacts/sources/bema/items/001-trust-the-story/transcript.normalized.json",
    "payload_uri": "data/import-artifacts/sources/bema/items/001-trust-the-story/import-payload.json",
    "external_id": "podcast:bema:bema-001:whisper",
    "source_fingerprint": "sha256:..."
  }
}
```

## Import result

`import-result.json` records what happened when the payload was imported.

```json
{
  "source_id": "...",
  "job_id": "...",
  "imported_at": "2026-07-02T12:00:00Z",
  "chunk_count": 184,
  "embedding_count": 184,
  "database_url_label": "local-postgres"
}
```

This file is not the source of truth for the transcript, but it is useful for audits and cleanup scripts.

## Database metadata

The HKB database should store enough pointers to reconnect rows to durable artifacts, but it should not be the only copy of generated transcripts.

At source level, store fields like:

```json
{
  "source_tree_slug": "bema",
  "source_tree_type": "podcast",
  "item_id": "bema-001",
  "item_type": "podcast_episode",
  "external_id": "podcast:bema:bema-001:whisper",
  "transcript_provenance": "whisper",
  "artifact_uri": "data/import-artifacts/sources/bema/items/001-trust-the-story/transcript.raw.json",
  "normalized_transcript_uri": "data/import-artifacts/sources/bema/items/001-trust-the-story/transcript.normalized.json",
  "payload_uri": "data/import-artifacts/sources/bema/items/001-trust-the-story/import-payload.json",
  "source_fingerprint": "sha256:..."
}
```

At segment/chunk level, preserve:

- `source_id`;
- segment or chunk index;
- `start_ms` / `end_ms` when available;
- speaker when available;
- citation label;
- timestamp URL when available;
- embedding model/version for derived embeddings.

## Re-import workflow

To re-import after a database reset, chunking change, or embedding-model change:

1. Locate the item artifact directory.
2. Prefer `import-payload.json` if the API contract has not changed.
3. If the API contract changed, regenerate `import-payload.json` from `source.json` and `transcript.normalized.json`.
4. POST the payload to `POST /sources/transcript` or the current import API.
5. Record the response in `import-result.json`.
6. Verify source count, segment count, chunk count, embeddings, and at least one citation link.

Do not rerun Whisper unless `transcript.raw.json` / `transcript.normalized.json` is missing, known-bad, or intentionally superseded.

## Deduplication and versioning

Use stable external IDs and fingerprints so importers can skip or replace safely.

Recommended external ID pattern:

```text
<source_tree_type>:<source_tree_slug>:<item_id>:<transcript_provenance-or-version>
```

Examples:

```text
podcast:bema:bema-001:whisper
youtube_channel:tim-mackie-youtube:yt-dq1x45abcde:whisper
book:surprised-by-hope:chapter-03:manual
notes:personal-notes:daily-reflection:markdown
```

Use `source_fingerprint` to detect content changes. A reasonable fingerprint input is the normalized transcript text plus the canonical URL, item ID, and transcript provenance.

When a transcript is corrected or regenerated, either:

- replace the same artifact directory and increment `artifact_version`; or
- create a versioned subdirectory such as `versions/v2/` if both versions should remain auditable.

## Object storage compatibility

The same logical layout should work on local disk or object storage.

Local URI:

```text
data/import-artifacts/sources/bema/items/001-trust-the-story/transcript.raw.json
```

Object-store URI:

```text
s3://hkb-artifacts/sources/bema/items/001-trust-the-story/transcript.raw.json
```

Importer code should treat artifact paths as URIs where possible.

## Git and content safety

`data/` is ignored by Git. Keep third-party transcripts, generated transcripts, and import payloads out of commits unless they are intentionally tiny fixtures with rights-cleared text.

For tests, use separate fixture paths under `tests/fixtures/` with synthetic or rights-cleared content.

## Migration note for existing podcast pipelines

Existing scripts currently store podcast artifacts under paths such as:

```text
data/import-artifacts/podcasts/<podcast-slug>/
data/import-state/podcasts/<podcast-slug>_pipeline_state.json
```

Future generic source ingestion should move toward:

```text
data/import-artifacts/sources/<source-tree-slug>/items/<item-slug-or-id>/
data/import-state/sources/<source-tree-slug>_pipeline_state.json
```

During migration, importers may support both paths. New code should write the generic layout unless a legacy script explicitly requires the older podcast layout.
