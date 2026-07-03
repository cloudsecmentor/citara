from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
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
            "created_at": datetime.now(timezone.utc).isoformat(),
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
    tree_meta("bema", "The BEMA Podcast", "podcast", "data/import-artifacts/bema + data/bema-session-1")
    sources: list[Path] = []
    for p in sorted((DATA / "bema-session-1").glob("*.json")):
        sources.append(p)
    for d in [DATA / "bema1", DATA / "bema35"]:
        sources.extend(sorted(d.glob("*.json")) if d.exists() else [])
    seen: set[str] = set()
    for src in sources:
        payload = read_json(src)
        base_slug = slugify(payload.get("episode_title") or src.stem)
        item_slug = base_slug
        if item_slug in seen:
            item_slug = slugify(f"{payload.get('episode_title') or src.stem}-{src.parent.name}-{src.stem}")
        seen.add(item_slug)
        out.append(organize_payload_json(src=src, tree_slug="bema", tree_type="podcast", provenance="published_transcript", item_slug=item_slug))
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
        out.append({"source": relative_to_repo(src), "target": str(item_dir / "source-page.html"), "tree": "bema", "item": item_slug, "kind": "source_page_html", "bytes": src.stat().st_size, "sha256": sha256_file(src)})
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
    return {"source": relative_to_repo(src), "target": str(dst), "kind": "state_json", "bytes": src.stat().st_size, "sha256": sha256_file(src)}


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


def build_summary(records: list[dict[str, Any]], state_records: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "schema": "citara.organization_manifest.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(REPO),
        "citara_root": str(Citara),
        "source_artifact_root": str(ARTIFACT_ROOT),
        "source_state_root": str(STATE_ROOT),
        "artifact_count": len(records),
        "state_count": len(state_records),
        "artifact_counts_by_tree": {},
        "artifact_counts_by_kind": {},
        "records": records,
        "state_records": state_records,
    }
    for r in records:
        summary["artifact_counts_by_tree"][r["tree"]] = summary["artifact_counts_by_tree"].get(r["tree"], 0) + 1
        summary["artifact_counts_by_kind"][r["kind"]] = summary["artifact_counts_by_kind"].get(r["kind"], 0) + 1
    return summary


def organize_all(*, repo: Path = REPO, artifact_root: Path = ARTIFACT_ROOT, state_root: Path = STATE_ROOT) -> dict[str, Any]:
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
    write_json(MANIFEST_PATH, summary)
    return summary


def main() -> None:
    summary = organize_all(repo=REPO, artifact_root=ARTIFACT_ROOT, state_root=STATE_ROOT)
    print(json.dumps({k: summary[k] for k in ["citara_root", "artifact_count", "state_count", "artifact_counts_by_tree", "artifact_counts_by_kind"]}, indent=2))


if __name__ == "__main__":
    main()
