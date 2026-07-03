# Ingestion Contract

This document defines the deterministic fixture-backed contract for the first Citara ingestion slices.

## Source priorities

Build and test in this order:

1. Markdown/text notes
2. Normalized podcast transcript fixtures
3. Podcast URL/RSS discovery
4. Podcast transcription provider integration
5. MCP tools and lightweight inspection API

PDFs, screenshots/images/OCR, and general web articles are deferred.

## Text note input

```json
{
  "input_type": "text",
  "title": "Procrastination Note",
  "text": "...",
  "collection_id": null
}
```

Expected behavior:

- Create a `Source` with `source_type=text_note`.
- Preserve the source text in derived chunk rows for MVP.
- Split text into deterministic chunks.
- Create simple citation labels of the form `<source title>, chunk <n>`.
- Search must return chunk id, source id/title/type, chunk text, score, and citation label.

## Normalized transcript input

```json
{
  "show_title": "Test Podcast",
  "episode_title": "Ambiguity and Action",
  "episode_url": "https://example.com/podcast/ambiguity-action",
  "segments": [
    {
      "start_ms": 1000,
      "end_ms": 5000,
      "speaker": "Host",
      "text": "Procrastination often happens when the next action is unclear."
    }
  ]
}
```

Expected behavior:

- Create a `Source` with `source_type=podcast_episode`.
- Store each segment with timestamps and optional speaker labels.
- Create chunks linked to transcript segments.
- Citation labels must include show title, episode title, and timestamp.
- Timestamp URLs are optional and provider-aware later; fallback to source URL + timestamp text.

## Search contract

Keyword search is deterministic for the first slice:

- Case-insensitive token matching.
- Results sorted by descending match count, then source title, then chunk index.
- Results must always include tenant filtering.

## Context pack contract

`retrieve_context_pack` returns compact evidence for the calling agent:

```json
{
  "query": "next action",
  "chunks": [
    {
      "chunk_id": "...",
      "text": "...",
      "citation": {
        "label": "Procrastination Note, chunk 1",
        "source_url": null,
        "timestamp_url": null,
        "page_number": null,
        "start_ms": null,
        "end_ms": null
      }
    }
  ]
}
```
