---
name: citara-backup-cleanup
description: Retain only the newest Citara backup generation.
version: 0.1.0
author: Seb, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [citara, backups, retention, maintenance]
    related_skills: []
---

# Citara Backup Cleanup Skill

Safely prune timestamped Citara backup generations while preserving complete restore sets. Files sharing a UTC timestamp such as `20260912T211217Z` are one generation and are retained or removed together.

## When to Use

- Schedule routine retention cleanup for Citara backups.
- Run a dry-run before changing backup retention.
- Verify that only timestamped backup files are eligible.

Do not use this skill to delete the live Citara database, source artifacts, unrecognized files, directories, or backups without a UTC timestamp in their filename.

## Prerequisites

- Python 3.11 or newer.
- A writable Citara backup directory.
- `CITARA_DATA_ROOT` set when data is not under `~/code/citara-data`.

## How to Run

Dry-run through `terminal`:

```text
python skills/maintenance/citara-backup-cleanup/scripts/cleanup_backups.py --dry-run
```

Apply retention through `terminal`:

```text
python skills/maintenance/citara-backup-cleanup/scripts/cleanup_backups.py
```

The default keeps only the newest timestamp generation, matching `citara.core.backups`, which already prunes older backups every time a new one is written. Use this skill to tidy up after manual copies. Override the directory or retention count only when explicitly requested:

```text
python skills/maintenance/citara-backup-cleanup/scripts/cleanup_backups.py --backup-dir /path/to/backups --keep 1
```

## Procedure

1. Run with `--dry-run`; confirm `recognized_generations`, `kept_generations`, and `delete_files` match the intended scope.
2. Run without `--dry-run`; require exit code zero and inspect the JSON summary.
3. Re-run with `--dry-run`; completion requires `delete_files` to be empty.
4. For automation, schedule the script directly as a script-only job so cleanup never depends on an LLM provider.

## Pitfalls

- Retention is by **generation**, not individual file. A `.db` and `.tar.gz` carrying the same UTC timestamp remain together.
- Only regular, non-symlink files with a `YYYYMMDDTHHMMSSZ` timestamp (optionally including fractional seconds before `Z`) and a recognized backup suffix are eligible.
- Cleanup and the shared backup helper coordinate through a database-scoped interprocess lock beside the database (`.<database filename>.backup.lock`). Never delete or replace it while an operation is active. For a database not named `citara.db`, pass `--database /absolute/path/to/custom.sqlite` so cleanup uses the same lock as its writer.
- Unknown files and incomplete names are ignored rather than deleted.
- `--keep` must be at least 1.

## Verification

A successful run exits zero and emits JSON where:

- `kept_generations` contains at most the requested number of newest generations;
- `deleted_files` lists only recognized files from older generations;
- a follow-up dry-run reports an empty `delete_files` list.
