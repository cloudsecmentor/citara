from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
DEFAULT_CITARA = (REPO / ".." / "citara").resolve()
ARTIFACT_ROOT = Path(os.getenv("SOURCE_ARTIFACT_ROOT", str(DEFAULT_CITARA / "source-artifacts"))).expanduser().resolve()
STATE_ROOT = Path(os.getenv("SOURCE_STATE_ROOT", str(DEFAULT_CITARA / "import-state"))).expanduser().resolve()
Citara = ARTIFACT_ROOT.parent if ARTIFACT_ROOT.name == "source-artifacts" else ARTIFACT_ROOT
MANIFEST_PATH = Citara / "organization-manifest.json"


def configure_paths(*, repo: Path, artifact_root: Path, state_root: Path) -> None:
    global REPO, DATA, ARTIFACT_ROOT, STATE_ROOT, Citara, MANIFEST_PATH
    REPO = repo.resolve()
    DATA = REPO / "data"
    ARTIFACT_ROOT = artifact_root.expanduser().resolve()
    STATE_ROOT = state_root.expanduser().resolve()
    Citara = ARTIFACT_ROOT.parent if ARTIFACT_ROOT.name == "source-artifacts" else ARTIFACT_ROOT
    MANIFEST_PATH = Citara / "organization-manifest.json"


def slugify(value: str, max_len: int = 120) -> str:
    value = value.lower().strip()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:max_len].strip("-") or "untitled"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def relative_to_repo(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def normalized_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "citara.transcript.normalized.v1",
        "language": payload.get("language") or payload.get("metadata", {}).get("language") or "en",
        "segments": [
            {
                "segment_index": i,
                "start_ms": seg.get("start_ms"),
                "end_ms": seg.get("end_ms"),
                "speaker": seg.get("speaker"),
                "text": seg.get("text", ""),
            }
            for i, seg in enumerate(payload.get("segments", []))
        ],
    }


def plain_text_from_segments(segments: list[dict[str, Any]]) -> str:
    return "\n\n".join((seg.get("text") or "").strip() for seg in segments if (seg.get("text") or "").strip()) + "\n"


def tree_meta(slug: str, title: str, tree_type: str, source_path: str | None = None) -> None:
    target = ARTIFACT_ROOT / slug / "source-tree.json"
    if target.exists():
        return
    write_json(
        target,
        {
            "schema": "citara.source_tree.v1",
            "source_tree_slug": slug,
            "source_tree_type": tree_type,
            "title": title,
            "source_path": source_path,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )


def bema_metadata(title: str) -> dict[str, Any]:
    match = re.search(r"\bBEMA\s+(-?\d+)([A-Za-z]?)\b", title)
    metadata: dict[str, Any] = {}
    if match:
        episode_number = int(match.group(1))
        suffix = match.group(2).lower()
        metadata["episode_number"] = episode_number
        metadata["source_page_item_id"] = f"bema-{episode_number:03d}{suffix}"
    lowered = title.lower()
    if "legacy" in lowered:
        metadata["version_label"] = "legacy"
        metadata["preference_label"] = "legacy"
        metadata["retrieval_weight"] = 0.7
    elif "current" in lowered:
        metadata["version_label"] = "current"
        metadata["preference_label"] = "current"
        metadata["retrieval_weight"] = 2.0
    return metadata


def source_item_metadata(
    *,
    payload: dict[str, Any],
    src: Path,
    tree_slug: str,
    tree_type: str,
    item_slug: str,
    provenance: str,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    title = payload.get("episode_title") or payload.get("title") or src.stem
    metadata = {
        "schema": "citara.source_item.v1",
        "source_tree_slug": tree_slug,
        "source_tree_type": tree_type,
        "item_id": item_slug,
        "item_type": payload.get("source_type") or "podcast_episode",
        "title": title,
        "canonical_url": payload.get("episode_url") or payload.get("canonical_url"),
        "language": payload.get("language") or "en",
        "transcript_provenance": provenance,
        "original_path": relative_to_repo(src),
        "artifact_version": 1,
    }
    if tree_slug == "bema":
        metadata.update(bema_metadata(title))
    if extra_meta:
        metadata.update(extra_meta)
    return metadata


def organize_payload_json(
    *,
    src: Path,
    tree_slug: str,
    tree_type: str,
    provenance: str = "published_transcript",
    item_slug: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = read_json(src)
    title = payload.get("episode_title") or payload.get("title") or src.stem
    item_slug = item_slug or slugify(title)
    item_dir = ARTIFACT_ROOT / tree_slug / "items" / item_slug
    copy_file(src, item_dir / "import-payload.json")
    copy_file(src, item_dir / "transcript.raw.json")
    write_json(item_dir / "transcript.normalized.json", normalized_from_payload(payload))
    if payload.get("segments"):
        (item_dir / "transcript.txt").write_text(plain_text_from_segments(payload["segments"]))
    write_json(
        item_dir / "source.json",
        source_item_metadata(
            payload=payload,
            src=src,
            tree_slug=tree_slug,
            tree_type=tree_type,
            item_slug=item_slug,
            provenance=provenance,
            extra_meta=extra_meta,
        ),
    )
    return {
        "source": relative_to_repo(src),
        "target": str(item_dir),
        "tree": tree_slug,
        "item": item_slug,
        "kind": "payload_json",
        "bytes": src.stat().st_size,
        "sha256": sha256_file(src),
    }


def organize_bibleproject() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = DATA / "import-artifacts" / "bibleproject"
    if not root.exists():
        return out
    tree_meta("bibleproject", "BibleProject", "podcast", relative_to_repo(root))
    for payload_path in sorted((root / "payloads").glob("*.json")):
        payload = read_json(payload_path)
        title = payload.get("episode_title") or payload_path.stem
        item_slug = slugify(title)
        item_dir = ARTIFACT_ROOT / "bibleproject" / "items" / item_slug
        out.append(
            organize_payload_json(
                src=payload_path,
                tree_slug="bibleproject",
                tree_type="podcast",
                provenance="published_transcript_pdf",
            )
        )
        for subdir, dst_name in [("pdf", "transcript.source.pdf"), ("text", "transcript.source.txt")]:
            suffix = "pdf" if subdir == "pdf" else "txt"
            src = root / subdir / f"{payload_path.stem}.{suffix}"
            if src.exists():
                copy_file(src, item_dir / dst_name)
    return out


def organize_bema_transcripts() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    sources: list[Path] = []
    for p in sorted((DATA / "bema-session-1").glob("*.json")):
        sources.append(p)
    for d in [DATA / "bema1", DATA / "bema35"]:
        sources.extend(sorted(d.glob("*.json")) if d.exists() else [])
    if not sources:
        # Unlike the other organize_* functions, this one aggregates several
        # directories and so has no single root to test up front. Without this
        # check it called tree_meta() unconditionally, creating a phantom
        # bema/source-tree.json in an otherwise-empty artifact tree whenever
        # data/ was absent -- the same "data/ is always populated" assumption
        # that let an empty manifest overwrite a populated one.
        return out
    tree_meta("bema", "The BEMA Podcast", "podcast", "data/import-artifacts/bema + data/bema-session-1")
    seen: set[str] = set()
    for src in sources:
        payload = read_json(src)
        base_slug = slugify(payload.get("episode_title") or src.stem)
        item_slug = base_slug
        if item_slug in seen:
            item_slug = slugify(f"{payload.get('episode_title') or src.stem}-{src.parent.name}-{src.stem}")
        seen.add(item_slug)
        out.append(
            organize_payload_json(src=src, tree_slug="bema", tree_type="podcast", provenance="published_transcript", item_slug=item_slug)
        )
    return out


def organize_bema_pages() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page_root = DATA / "import-artifacts" / "bema" / "pages"
    if not page_root.exists():
        return out
    tree_meta("bema", "The BEMA Podcast", "podcast", relative_to_repo(page_root))
    for src in sorted(page_root.glob("*.html"), key=lambda p: (0, int(p.stem)) if p.stem.isdigit() else (1, p.stem)):
        item_slug = f"bema-{int(src.stem):03d}" if src.stem.isdigit() else slugify(src.stem)
        item_dir = ARTIFACT_ROOT / "bema" / "items" / item_slug
        copy_file(src, item_dir / "source-page.html")
        source_json = item_dir / "source.json"
        if not source_json.exists():
            write_json(
                source_json,
                {
                    "schema": "citara.source_item.v1",
                    "source_tree_slug": "bema",
                    "source_tree_type": "podcast",
                    "item_id": item_slug,
                    "item_type": "podcast_episode_page",
                    "title": f"BEMA {src.stem}",
                    "canonical_url": f"https://www.bemadiscipleship.com/{src.stem}",
                    "transcript_provenance": "source_page_snapshot",
                    "original_path": relative_to_repo(src),
                    "artifact_version": 1,
                },
            )
        out.append(
            {
                "source": relative_to_repo(src),
                "target": str(item_dir / "source-page.html"),
                "tree": "bema",
                "item": item_slug,
                "kind": "source_page_html",
                "bytes": src.stat().st_size,
                "sha256": sha256_file(src),
            }
        )
    return out


def organize_legacy_python_bytes() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = DATA / "real-podcast-transcripts"
    if not root.exists():
        return out
    tree_meta("python-bytes", "Python Bytes", "podcast", relative_to_repo(root))
    for src in sorted(root.glob("*.json")):
        out.append(organize_payload_json(src=src, tree_slug="python-bytes", tree_type="podcast", provenance="published_transcript"))
    return out


def organize_generic_podcast_artifacts() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = DATA / "import-artifacts" / "podcasts"
    if not root.exists():
        return out
    for podcast_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        tree_slug = slugify(podcast_dir.name)
        payload_root = podcast_dir / "payloads"
        if not payload_root.exists():
            continue
        tree_title = tree_slug.replace("-", " ").title()
        tree_meta(tree_slug, tree_title, "podcast", relative_to_repo(podcast_dir))
        for src in sorted(payload_root.glob("*.json")):
            record = organize_payload_json(src=src, tree_slug=tree_slug, tree_type="podcast", provenance="published_transcript")
            out.append(record)
            item_dir = ARTIFACT_ROOT / tree_slug / "items" / record["item"]
            vtt = podcast_dir / "transcripts" / f"{src.stem}.vtt"
            if vtt.exists():
                copy_file(vtt, item_dir / "transcript.vtt")
    return out


def legacy_artifact_path_to_uri(value: str) -> str:
    match = re.fullmatch(r"data/import-artifacts/([^/]+)/pdf/(.+)\.pdf", value)
    if match:
        tree_slug = slugify(match.group(1))
        item_slug = slugify(match.group(2))
        return f"source-artifacts://{tree_slug}/items/{item_slug}/transcript.source.pdf"
    match = re.fullmatch(r"data/import-artifacts/([^/]+)/text/(.+)\.txt", value)
    if match:
        tree_slug = slugify(match.group(1))
        item_slug = slugify(match.group(2))
        return f"source-artifacts://{tree_slug}/items/{item_slug}/transcript.source.txt"
    return value


def rewrite_state_paths(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: rewrite_state_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite_state_paths(v) for v in value]
    if isinstance(value, str):
        return legacy_artifact_path_to_uri(value)
    return value


def copy_state(src: Path, dst: Path) -> dict[str, Any]:
    if src.suffix == ".json":
        state = rewrite_state_paths(read_json(src))
        write_json(dst, state)
    else:
        copy_file(src, dst)
    return {
        "source": relative_to_repo(src),
        "target": str(dst),
        "kind": "state_json",
        "bytes": src.stat().st_size,
        "sha256": sha256_file(src),
    }


def organize_states() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    mappings = {
        "bema_pipeline_state.json": "bema_pipeline_state.json",
        "textinus_pipeline_state.json": "textinus_pipeline_state.json",
        "bibleproject_pipeline_state.json": "bibleproject_pipeline_state.json",
    }
    state_dir = DATA / "import-state"
    for src_name, dst_name in mappings.items():
        src = state_dir / src_name
        if src.exists():
            out.append(copy_state(src, STATE_ROOT / dst_name))
    podcasts_state = state_dir / "podcasts"
    if podcasts_state.exists():
        for src in sorted(podcasts_state.glob("*.json")):
            out.append(copy_state(src, STATE_ROOT / src.name))
    return out


def trees_missing_source_tree_json(artifact_root: Path) -> list[str]:
    """Tree slugs under artifact_root that have no source-tree.json.

    Computed fresh from disk (not from the record list) so it reflects reality in
    both the organize and rebuild paths, including trees that carry zero records.
    """
    if not artifact_root.exists():
        return []
    return sorted(entry.name for entry in artifact_root.iterdir() if entry.is_dir() and not (entry / "source-tree.json").exists())


def build_summary(records: list[dict[str, Any]], state_records: list[dict[str, Any]], *, mode: str = "organized") -> dict[str, Any]:
    summary: dict[str, Any] = {
        "schema": "citara.organization_manifest.v1",
        "mode": mode,
        "created_at": datetime.now(UTC).isoformat(),
        "repo": str(REPO),
        "citara_root": str(Citara),
        "source_artifact_root": str(ARTIFACT_ROOT),
        "source_state_root": str(STATE_ROOT),
        "artifact_count": len(records),
        "state_count": len(state_records),
        "artifact_counts_by_tree": {},
        "artifact_counts_by_kind": {},
        "trees_missing_source_tree_json": trees_missing_source_tree_json(ARTIFACT_ROOT),
        "records": records,
        "state_records": state_records,
    }
    for r in records:
        summary["artifact_counts_by_tree"][r["tree"]] = summary["artifact_counts_by_tree"].get(r["tree"], 0) + 1
        summary["artifact_counts_by_kind"][r["kind"]] = summary["artifact_counts_by_kind"].get(r["kind"], 0) + 1
    return summary


class ManifestWriteRefused(RuntimeError):
    """Raised when a write would silently replace a populated manifest with an empty one."""


def guard_manifest_write(new_artifact_count: int, *, manifest_path: Path, force: bool) -> None:
    """Refuse to overwrite a populated manifest with a 0-record one.

    This is the fix for the actual data-loss bug: organize_all() used to write an
    unconditional summary over MANIFEST_PATH, so an emptied staging dir (or, for
    rebuild, an emptied/missing artifact tree) silently wiped a fully populated
    manifest. force=True is the explicit opt-out for when that is truly intended
    (e.g. bootstrapping a brand-new, still-empty tree over a stale manifest).
    """
    if force or new_artifact_count > 0 or not manifest_path.exists():
        return
    try:
        existing = read_json(manifest_path)
    except (OSError, json.JSONDecodeError):
        return
    existing_count = existing.get("artifact_count", 0)
    if existing_count > 0:
        raise ManifestWriteRefused(
            f"Refusing to write {manifest_path}: the new manifest has 0 records but the "
            f"existing one has {existing_count}. This usually means the source data was "
            "emptied or moved rather than actually reorganized -- writing now would "
            "silently destroy the only copy of the index. Re-run with --force if you "
            "really intend to replace it with an empty manifest."
        )


# --- rebuild-from-artifacts mode -------------------------------------------------
#
# organize_all() builds records by scanning data/ (a staging dir) and knows the
# provenance of each file because it just wrote it. Rebuild instead walks the
# already-organized source-artifacts / import-state trees directly, so it can
# recover everything except the one thing that was never stored on disk: the
# original data/ path. That is salvaged on a best-effort basis from each item's
# own source.json ("original_path"), which real data shows is only present on
# ~55% of items; the rest get source: null rather than a fabricated value.

# Remote-transcription artifacts are named "<item-prefix>-<kind>.json", but the
# prefix convention varies by show: bema uses "e<NNN>-", others use forms like
# "q001-buzzsprout-17003095-s4e1-". Anchoring on "e\d+-" only classified bema's
# files and dropped 240 real artifacts into the "other" bucket, so match on the
# kind suffix and let the prefix be anything non-empty.
_OAI_RAW_CHUNKED_RE = re.compile(r"^.+-oai-raw-chunked\.json$")
_OAI_RAW_RE = re.compile(r"^.+-oai-raw\.json$")
_TRANSCRIBE_STATS_RE = re.compile(r"^.+-transcribe-stats\.json$")

# Direct filename -> kind. Reuses the three literals organize_*() already emits
# (payload_json, source_page_html, state_json) wherever a rebuilt file is
# genuinely the same artifact those functions produce, so organized and
# rebuilt manifests stay comparable on artifact_counts_by_kind.
_FILENAME_KIND_MAP: dict[str, str] = {
    "source.json": "source_metadata_json",
    "source-tree.json": "source_tree_json",
    "transcript.txt": "transcript_text",
    "transcript.normalized.json": "transcript_normalized_json",
    # Both are verbatim copies of the original import payload (see
    # organize_payload_json), so both carry the existing "payload_json" kind.
    "import-payload.json": "payload_json",
    "transcript.raw.json": "payload_json",
    "transcript.source.txt": "transcript_source_txt",
    "transcript.source.pdf": "transcript_source_pdf",
    "source-page.html": "source_page_html",
}


def classify_artifact_kind(filename: str) -> str:
    kind = _FILENAME_KIND_MAP.get(filename)
    if kind is not None:
        return kind
    if _OAI_RAW_CHUNKED_RE.match(filename):
        return "oai_raw_chunked_json"
    if _OAI_RAW_RE.match(filename):
        return "oai_raw_json"
    if _TRANSCRIBE_STATS_RE.match(filename):
        return "transcribe_stats_json"
    return "other"


# Skipping every dotfile also skipped live pipeline state (.transcription-watchdog.lock,
# .hourly-completion-reported, ...), which an audit index has no business omitting.
# Only genuine OS cruft is excluded now.
_CRUFT_NAMES = frozenset({".DS_Store", "Thumbs.db", ".localized"})


def is_os_cruft(path: Path) -> bool:
    return path.name in _CRUFT_NAMES or path.name.startswith("._")


def iter_tree_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and not is_os_cruft(p))


def item_slug_from_parts(rel_parts: tuple[str, ...]) -> str | None:
    if len(rel_parts) >= 3 and rel_parts[1] == "items":
        return rel_parts[2]
    return None


def original_path_from_sibling_source_json(path: Path) -> str | None:
    sibling = path.parent / "source.json"
    if not sibling.exists():
        return None
    try:
        data = read_json(sibling)
    except (OSError, json.JSONDecodeError):
        return None
    original_path = data.get("original_path")
    return original_path if isinstance(original_path, str) else None


def rebuild_artifact_record(path: Path, *, hash_files: bool) -> dict[str, Any]:
    rel_parts = path.relative_to(ARTIFACT_ROOT).parts
    record: dict[str, Any] = {
        "source": original_path_from_sibling_source_json(path),
        "target": str(path),
        "tree": rel_parts[0],
        "kind": classify_artifact_kind(path.name),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path) if hash_files else None,
    }
    item = item_slug_from_parts(rel_parts)
    if item is not None:
        record["item"] = item
    return record


def rebuild_state_record(path: Path, *, hash_files: bool) -> dict[str, Any]:
    # There is no salvage mechanism for state file provenance (no sibling metadata
    # carries it), so source is always null here -- same "do not invent it" rule
    # as artifact records, applied honestly rather than guessing.
    return {
        "source": None,
        "target": str(path),
        "kind": "state_json",
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path) if hash_files else None,
    }


def rebuild_from_artifacts(
    *,
    repo: Path = REPO,
    artifact_root: Path = ARTIFACT_ROOT,
    state_root: Path = STATE_ROOT,
    hash_files: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Rebuild organization-manifest.json by walking the existing artifact/state trees.

    Unlike organize_all(), this never writes into artifact_root or state_root --
    it only reads them and (subject to guard_manifest_write) writes MANIFEST_PATH.
    Trees missing source-tree.json are reported via
    summary["trees_missing_source_tree_json"] rather than being synthesized, to
    keep rebuild strictly read-only against a tree that may already be in a
    partially-known state.
    """
    configure_paths(repo=repo, artifact_root=artifact_root, state_root=state_root)
    records = [rebuild_artifact_record(p, hash_files=hash_files) for p in iter_tree_files(ARTIFACT_ROOT)]
    state_records = [rebuild_state_record(p, hash_files=hash_files) for p in iter_tree_files(STATE_ROOT)]
    summary = build_summary(records, state_records, mode="rebuilt")
    guard_manifest_write(summary["artifact_count"], manifest_path=MANIFEST_PATH, force=force)
    write_json(MANIFEST_PATH, summary)
    return summary


def organize_all(
    *, repo: Path = REPO, artifact_root: Path = ARTIFACT_ROOT, state_root: Path = STATE_ROOT, force: bool = False
) -> dict[str, Any]:
    configure_paths(repo=repo, artifact_root=artifact_root, state_root=state_root)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    records.extend(organize_bibleproject())
    records.extend(organize_bema_transcripts())
    records.extend(organize_bema_pages())
    records.extend(organize_legacy_python_bytes())
    records.extend(organize_generic_podcast_artifacts())
    state_records = organize_states()
    summary = build_summary(records, state_records)
    guard_manifest_write(summary["artifact_count"], manifest_path=MANIFEST_PATH, force=force)
    write_json(MANIFEST_PATH, summary)
    return summary


_SUMMARY_PRINT_KEYS = [
    "citara_root",
    "mode",
    "artifact_count",
    "state_count",
    "artifact_counts_by_tree",
    "artifact_counts_by_kind",
    "trees_missing_source_tree_json",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Organize Citara source artifacts, or rebuild the manifest from an existing tree")
    parser.add_argument(
        "--rebuild-from-artifacts",
        action="store_true",
        help="Rebuild organization-manifest.json by walking the existing source-artifacts/import-state "
        "trees instead of organizing files from data/. Use this when data/ has already been emptied.",
    )
    parser.add_argument(
        "--no-hash",
        action="store_true",
        help="Skip sha256 hashing (rebuild mode only). The real tree is ~1.3GB / ~10k files and hashing takes minutes.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow writing a 0-record manifest over an existing manifest that has records.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    repo: Path = REPO,
    artifact_root: Path = ARTIFACT_ROOT,
    state_root: Path = STATE_ROOT,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.rebuild_from_artifacts:
            summary = rebuild_from_artifacts(
                repo=repo,
                artifact_root=artifact_root,
                state_root=state_root,
                hash_files=not args.no_hash,
                force=args.force,
            )
        else:
            summary = organize_all(repo=repo, artifact_root=artifact_root, state_root=state_root, force=args.force)
    except ManifestWriteRefused as exc:
        print(str(exc))
        return 2
    print(json.dumps({k: summary[k] for k in _SUMMARY_PRINT_KEYS}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
