"""Portable bundle tests use synthetic databases only; never production data."""

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from citara import __version__


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "original"
    root.mkdir()
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from citara.core.models import Base, Chunk, Source, Tenant, User

    engine = create_engine(f"sqlite:///{root / 'citara.db'}")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(Tenant(id="local", name="Local"))
            session.flush()
            session.add(User(id="owner", tenant_id="local", display_name="Owner"))
            session.flush()
            session.add(Source(id="source-1", tenant_id="local", user_id="owner", source_type="text", title="Synthetic"))
            session.flush()
            session.add(Chunk(id="chunk-1", tenant_id="local", source_id="source-1", chunk_index=0, text="Synthetic text"))
            session.commit()
        with sqlite3.connect(root / "citara.db") as db:
            db.executescript("""
                CREATE TABLE alembic_version(version_num TEXT NOT NULL);
                INSERT INTO alembic_version VALUES ('20260816_0005');
                INSERT INTO chunk_fts(chunk_id, tokens) VALUES ('chunk-1', 'synthetic text');
            """)
    finally:
        engine.dispose()
    return root


def export(root, output, **kwargs):
    from citara.core.bundles import export_bundle

    return export_bundle(
        data_root=root, output=output, embedding_provider="local", embedding_model="deterministic-hash-v1", embedding_dimensions=8, **kwargs
    )


def test_export_snapshot_manifest(corpus, tmp_path):
    output = tmp_path / "corpus.zip"
    manifest = export(corpus, output)
    with zipfile.ZipFile(output) as z:
        assert set(z.namelist()) == {"manifest.json", "citara.db"}
        assert json.loads(z.read("manifest.json")) == manifest
        record = manifest["payloads"][0]
        assert record == {"path": "citara.db", "size": len(z.read("citara.db")), "sha256": hashlib.sha256(z.read("citara.db")).hexdigest()}
    assert manifest["format"] == "citara-corpus"
    assert manifest["format_version"] == 1
    assert manifest["citara_version"] == __version__
    assert manifest["alembic_revision"] == "20260816_0005"
    assert manifest["counts"] == {"sources": 1, "chunks": 1}
    assert manifest["embedding"] == {"provider": "local", "model": "deterministic-hash-v1", "dimensions": 8}
    assert manifest["tenant_id"] == "local"
    assert manifest["user_id"] == "owner"


def test_export_artifact_allowlist_wal_and_safety(corpus, tmp_path):
    from citara.core.bundles import BundleError

    db = sqlite3.connect(corpus / "citara.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "INSERT INTO sources SELECT 'wal-only', tenant_id, user_id, collection_id, source_type, title, author, canonical_url, provider, external_id, original_asset_id, status, language, metadata_json, created_at, updated_at FROM sources WHERE id='source-1'"
    )
    db.commit()
    for name in (
        ".env",
        "import-state/state",
        "backups/old.db",
        "logs/log",
        "cache/item",
        "source-artifacts/nested/transcript.txt",
        "object-store/item",
    ):
        path = corpus / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private" if name == ".env" else "artifact")
    try:
        plain = export(corpus, tmp_path / "plain.zip")
        assert plain["counts"]["sources"] == 2
        output = tmp_path / "artifacts.zip"
        manifest = export(corpus, output, include_artifacts=True, tenant_id="team", user_id="reader")
        assert {r["path"] for r in manifest["payloads"]} == {"citara.db", "source-artifacts/nested/transcript.txt", "object-store/item"}
        with pytest.raises(BundleError, match="exist"):
            export(corpus, output)
        with pytest.raises(BundleError, match="inside"):
            export(corpus, corpus / "bad.zip")
        (corpus / "source-artifacts/link").symlink_to(tmp_path)
        with pytest.raises(BundleError, match="[Ss]ymlink"):
            export(corpus, tmp_path / "link.zip", include_artifacts=True)
        assert not (tmp_path / "link.zip").exists()
        with pytest.raises(BundleError, match="absolute"):
            export(Path("relative"), tmp_path / "relative.zip")
    finally:
        db.close()


@pytest.mark.parametrize(
    "name", ["corpus?query", "corpus\x01control", "corpus\tcontrol", "corpus\ncontrol", "corpus\x7fcontrol", "corpus\x9fcontrol"]
)
@pytest.mark.parametrize("operation", ["import", "settings"])
def test_unsafe_data_root_rejected_before_restore_or_settings(corpus, tmp_path, name, operation, monkeypatch):
    from citara.core import bundles

    archive = tmp_path / "good.zip"
    manifest = export(corpus, archive)
    target = tmp_path / name
    before = set(tmp_path.iterdir())

    def unexpected_restore(*args, **kwargs):
        pytest.fail("Unsafe data root must be rejected before restoring the archive")

    monkeypatch.setattr(bundles, "_restore", unexpected_restore)
    with pytest.raises(bundles.BundleError, match="data root.*[?].*control"):
        if operation == "import":
            bundles.import_bundle(archive, data_root=target)
        else:
            bundles._environment(target, manifest)
    assert set(tmp_path.iterdir()) == before
    assert not target.exists()
    assert not (target / "citara.env").exists()


def test_import_unicode_space_quote_root_selects_exact_sqlite_database(corpus, tmp_path):
    import subprocess
    import sys

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from citara.core.bundles import import_bundle

    archive = tmp_path / "regular.zip"
    export(corpus, archive)
    target = tmp_path / "corpus å日本 'quoted'"
    import_bundle(archive, data_root=target)
    launcher = Path(__file__).resolve().parents[1] / "scripts/run_citara.py"
    result = subprocess.run(
        [sys.executable, str(launcher), "--env-file", str(target / "citara.env"), "check"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    url = make_url(settings["DATABASE_URL"])
    assert url.database == (target / "citara.db").as_posix()
    assert not url.query
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT title FROM sources")).scalar_one() == "Synthetic"
    finally:
        engine.dispose()


def test_roundtrip_inspect_and_portable_environment(corpus, tmp_path):
    from citara.core.bundles import BundleError, import_bundle, inspect_bundle

    artifact = corpus / "source-artifacts/episode.txt"
    artifact.parent.mkdir()
    artifact.write_text("transcript")
    archive = tmp_path / "roundtrip.zip"
    manifest = export(corpus, archive, include_artifacts=True, tenant_id="team", user_id="reader")
    assert inspect_bundle(archive) == manifest
    target = tmp_path / "restored"
    assert import_bundle(archive, data_root=target) == manifest
    assert json.loads((target / "corpus.json").read_text()) == manifest
    assert (target / "source-artifacts/episode.txt").read_text() == "transcript"
    env = (target / "citara.env").read_text()
    for setting in (
        f"CITARA_DATA_ROOT={target}",
        f"DATABASE_URL=sqlite:///{target}/citara.db",
        "EMBEDDING_PROVIDER=local",
        "EMBEDDING_MODEL=deterministic-hash-v1",
        "EMBEDDING_DIMENSIONS=8",
        "DEFAULT_TENANT_ID=team",
        "DEFAULT_USER_ID=reader",
        "CITARA_SKIP_DOTENV=1",
        "TRANSLATION_PROVIDER=noop",
        f"SOURCE_STATE_ROOT={target}/import-state",
    ):
        assert setting in env
    assert "KEY" not in env and "ENDPOINT" not in env
    with sqlite3.connect(target / "citara.db") as db:
        assert db.execute("SELECT count(*) FROM chunks").fetchone() == (1,)
    with pytest.raises(BundleError, match="exist"):
        import_bundle(archive, data_root=target)


def rewrite_bundle(original, output, mutate=None, extra=()):
    with zipfile.ZipFile(original) as z:
        members = {name: z.read(name) for name in z.namelist()}
    manifest = json.loads(members["manifest.json"])
    if mutate:
        mutate(manifest, members)
    members["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
        for name, data in extra:
            z.writestr(name, data)
    return output


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "C:/drive",
        "C:drive",
        "a\\b",
        "a//b",
        "source-artifacts/../escape",
        "source-artifacts/./x",
        "undeclared",
        "source-artifacts/folder/",
    ],
)
def test_reject_unsafe_or_undeclared_zip_paths(corpus, tmp_path, name):
    from citara.core.bundles import BundleError, import_bundle

    original = tmp_path / "good.zip"
    export(corpus, original)
    bad = rewrite_bundle(original, tmp_path / "bad.zip", extra=[(name, b"bad")])
    target = tmp_path / "target"
    with pytest.raises(BundleError):
        import_bundle(bad, data_root=target)
    assert not target.exists()
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    "segment",
    ["CON", "con.txt", "PrN", "AUX.json", "nul", "CON .txt", "name.", "name ", "COM¹", "lpt².txt", "COM³.txt"]
    + [f"{prefix}{digit}.txt" for prefix in ("COM", "LPT") for digit in range(1, 10)],
)
@pytest.mark.parametrize("operation", ["path", "import"])
def test_reject_windows_device_and_aliased_path_segments(corpus, tmp_path, segment, operation):
    from citara.core.bundles import BundleError, _payload_name, import_bundle

    name = f"source-artifacts/nested/{segment}/transcript.txt"
    target = tmp_path / "target"
    if operation == "path":
        with pytest.raises(BundleError, match="[Pp]ath"):
            _payload_name(name)
    else:
        original = tmp_path / "good.zip"
        export(corpus, original)

        def add_payload(manifest, members):
            members[name] = b"transcript"
            manifest["payloads"].append({"path": name, "size": 10, "sha256": hashlib.sha256(b"transcript").hexdigest()})

        archive = rewrite_bundle(original, tmp_path / "bad.zip", add_payload)
        with pytest.raises(BundleError, match="[Pp]ath"):
            import_bundle(archive, data_root=target)
        assert not target.exists()


def test_valid_unicode_artifact_names_roundtrip(corpus, tmp_path):
    from citara.core.bundles import import_bundle

    name = "source-artifacts/日本語/åäö-эпизод.txt"
    artifact = corpus / name
    artifact.parent.mkdir(parents=True)
    artifact.write_text("Unicode content")
    archive = tmp_path / "unicode.zip"
    manifest = export(corpus, archive, include_artifacts=True)
    target = tmp_path / "target"
    assert import_bundle(archive, data_root=target) == manifest
    assert (target / name).read_text() == "Unicode content"


@pytest.mark.parametrize("kind", ["duplicate", "symlink", "encrypted", "too-large", "ratio", "members"])
def test_reject_zip_hazards_and_limits(corpus, tmp_path, kind):
    import stat

    from citara.core.bundles import BundleError, BundleLimits, inspect_bundle

    original = tmp_path / "good.zip"
    export(corpus, original)
    bad = tmp_path / "bad.zip"
    limits = {}
    if kind == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate"):
            rewrite_bundle(original, bad, extra=[("citara.db", b"duplicate")])
    elif kind == "symlink":
        info = zipfile.ZipInfo("source-artifacts/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        rewrite_bundle(original, bad, extra=[(info, b"/elsewhere")])
    elif kind == "encrypted":
        data = bytearray(original.read_bytes())
        # Encryption bit in each local/central header, without requiring a password tool.
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            start = 0
            while (start := data.find(signature, start)) >= 0:
                data[start + offset] |= 1
                start += 4
        bad.write_bytes(data)
    else:
        bad.write_bytes(original.read_bytes())
        limits = (
            {"max_file_bytes": 10} if kind == "too-large" else ({"max_compression_ratio": 1} if kind == "ratio" else {"max_members": 1})
        )
    with pytest.raises(BundleError):
        inspect_bundle(bad, limits=BundleLimits(**limits))


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", "foreign"),
        ("format_version", 2),
        ("format_version", True),
        ("citara_version", "99.0.0"),
        ("alembic_revision", "old"),
        ("counts", {"sources": 100, "chunks": 1}),
        ("counts", {"sources": True, "chunks": 1}),
        ("embedding", {}),
        ("embedding", {"provider": "local", "model": "hash", "dimensions": 0}),
        ("tenant_id", "bad\nKEY=secret"),
        ("user_id", "$(touch /tmp/no)"),
        ("payloads", []),
        ("payloads", [{"path": "citara.db", "size": -1, "sha256": "bad"}]),
    ],
)
def test_manifest_validation(corpus, tmp_path, field, value):
    from citara.core.bundles import BundleError, import_bundle

    original = tmp_path / "good.zip"
    export(corpus, original)
    bad = rewrite_bundle(original, tmp_path / "bad.zip", lambda m, _: m.update({field: value}))
    with pytest.raises(BundleError):
        import_bundle(bad, data_root=tmp_path / "target")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("kind", ["hash", "size", "integrity", "schema", "table"])
def test_payload_and_database_validation(corpus, tmp_path, kind):
    from citara.core.bundles import BundleError, inspect_bundle

    original = tmp_path / "good.zip"
    export(corpus, original)

    def mutate(manifest, members):
        if kind == "hash":
            members["citara.db"] = members["citara.db"] + b"tamper"
        elif kind == "size":
            manifest["payloads"][0]["size"] += 1
        else:
            if kind == "integrity":
                members["citara.db"] = b"not sqlite"
            else:
                staging = tmp_path / "changed.db"
                staging.write_bytes(members["citara.db"])
                with sqlite3.connect(staging) as db:
                    db.execute("UPDATE alembic_version SET version_num='old'" if kind == "schema" else "DROP TABLE chunks")
                members["citara.db"] = staging.read_bytes()
            manifest["payloads"][0].update(size=len(members["citara.db"]), sha256=hashlib.sha256(members["citara.db"]).hexdigest())

    bad = rewrite_bundle(original, tmp_path / "bad.zip", mutate)
    with pytest.raises(BundleError):
        inspect_bundle(bad)


@pytest.mark.parametrize("operation", ["export", "import"])
@pytest.mark.parametrize("schema", ["minimal", "missing-table", "missing-column", "missing-fts", "fake-fts"])
def test_reject_spoofed_current_schema(corpus, tmp_path, operation, schema):
    from citara.core.bundles import BundleError, import_bundle

    original = tmp_path / "good.zip"
    export(corpus, original)
    database = corpus / "citara.db"
    with sqlite3.connect(database) as db:
        if schema == "minimal":
            db.executescript("""
                DROP TABLE chunks;
                DROP TABLE sources;
                CREATE TABLE sources(id TEXT PRIMARY KEY);
                CREATE TABLE chunks(id TEXT PRIMARY KEY, source_id TEXT);
                INSERT INTO sources VALUES ('source-1');
                INSERT INTO chunks VALUES ('chunk-1', 'source-1');
            """)
        elif schema == "missing-table":
            db.execute("DROP TABLE ingestion_jobs")
        elif schema == "missing-column":
            db.execute("ALTER TABLE sources DROP COLUMN author")
        else:
            db.execute("DROP TABLE chunk_fts")
            if schema == "fake-fts":
                db.execute("CREATE TABLE chunk_fts(chunk_id TEXT, tokens TEXT)")
    before = database.read_bytes()
    target = tmp_path / "rejected"
    with pytest.raises(BundleError, match="schema"):
        if operation == "export":
            export(corpus, tmp_path / "bad-export.zip")
        else:

            def replace_database(manifest, members):
                members["citara.db"] = before
                manifest["payloads"][0].update(size=len(before), sha256=hashlib.sha256(before).hexdigest())

            archive = rewrite_bundle(original, tmp_path / "bad-import.zip", replace_database)
            import_bundle(archive, data_root=target)
    assert database.read_bytes() == before
    assert not target.exists()
    assert not (tmp_path / "bad-export.zip").exists()


def test_schema_contract_matches_runtime_models(corpus):
    from citara.core.bundles import REQUIRED_SCHEMA, SCHEMA_REVISION, _database_info
    from citara.core.models import CHUNK_FTS_TABLE, Base

    expected = {name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()}
    expected.update(alembic_version={"version_num"})
    expected[CHUNK_FTS_TABLE] = {"chunk_id", "tokens"}
    assert REQUIRED_SCHEMA == expected
    database = corpus / "citara.db"
    before = database.read_bytes()
    assert _database_info(database) == {"alembic_revision": SCHEMA_REVISION, "counts": {"sources": 1, "chunks": 1}}
    assert database.read_bytes() == before


def test_database_validation_stays_stdlib_only_and_environment_independent(corpus, tmp_path):
    import os
    import subprocess
    import sys

    database = corpus / "citara.db"
    before = database.read_bytes()
    forbidden_root = tmp_path / "must-not-be-created"
    env = {**os.environ, "DATABASE_URL": "invalid://must-not-connect", "CITARA_DATA_ROOT": str(forbidden_root)}
    result = subprocess.run(
        [
            sys.executable,
            "-S",  # No site-packages: validation must not need SQLAlchemy/settings.
            "-c",
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "from citara.core.bundles import _database_info; "
            "assert _database_info(Path(sys.argv[2]))['counts'] == {'sources': 1, 'chunks': 1}; "
            "assert 'citara.core.config' not in sys.modules; assert 'citara.core.db' not in sys.modules",
            str(Path(__file__).resolve().parents[1] / "src"),
            str(database),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert database.read_bytes() == before
    assert not forbidden_root.exists()


@pytest.mark.parametrize("operation", ["export", "import"])
def test_reject_unknown_embedding_provider(corpus, tmp_path, operation):
    from citara.core.bundles import BundleError, export_bundle, import_bundle

    archive = tmp_path / "provider.zip"
    target = tmp_path / "target"
    if operation == "import":
        original = tmp_path / "good.zip"
        export(corpus, original)
        rewrite_bundle(original, archive, lambda m, _: m["embedding"].update(provider="unsupported"))
    with pytest.raises(BundleError, match="[Pp]rovider"):
        if operation == "export":
            export_bundle(
                data_root=corpus, output=archive, embedding_provider="unsupported", embedding_model="hash", embedding_dimensions=8
            )
        else:
            import_bundle(archive, data_root=target)
    assert not target.exists()
    if operation == "export":
        assert not archive.exists()


@pytest.mark.parametrize("provider", ["local", "openai", "azure_foundry"])
def test_supported_embedding_provider_roundtrip_without_credentials(corpus, tmp_path, provider, monkeypatch):
    from citara.core.bundles import export_bundle, import_bundle

    for name in ("OPENAI_API_KEY", "AZURE_FOUNDRY_API_KEY", "AZURE_FOUNDRY_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    archive = tmp_path / "provider.zip"
    manifest = export_bundle(data_root=corpus, output=archive, embedding_provider=provider, embedding_model="hash", embedding_dimensions=8)
    target = tmp_path / "target"
    assert import_bundle(archive, data_root=target) == manifest
    assert f"EMBEDDING_PROVIDER={provider}" in (target / "citara.env").read_text()
    assert "KEY=" not in (target / "citara.env").read_text()


def test_sanitized_manifest_drops_unrecognized_secrets(corpus, tmp_path):
    from citara.core.bundles import import_bundle

    original = tmp_path / "good.zip"
    export(corpus, original)

    def mutate(manifest, _):
        manifest["api_key"] = "SUPER_SECRET"
        manifest["embedding"]["endpoint"] = "https://private.example"

    bad = rewrite_bundle(original, tmp_path / "extras.zip", mutate)
    target = tmp_path / "target"
    result = import_bundle(bad, data_root=target)
    assert "api_key" not in result
    assert "endpoint" not in result["embedding"]
    assert "SUPER_SECRET" not in (target / "corpus.json").read_text()


def test_cli_explicit_settings_and_roundtrip(corpus, tmp_path):
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts/citara_bundle.py"

    def run(*args):
        return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True)

    archive = tmp_path / "cli.zip"
    missing = run("export", "--data-root", corpus, "--output", archive)
    assert missing.returncode == 2
    assert "--embedding-provider" in missing.stderr
    result = run(
        "export",
        "--data-root",
        corpus,
        "--output",
        archive,
        "--embedding-provider",
        "local",
        "--embedding-model",
        "deterministic-hash-v1",
        "--embedding-dimensions",
        "8",
        "--tenant-id",
        "team",
        "--user-id",
        "reader",
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["tenant_id"] == "team" and manifest["user_id"] == "reader"
    inspected = run("inspect", archive)
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout) == manifest
    limited = run("inspect", archive, "--max-total-bytes", "10")
    assert limited.returncode == 2 and "limit" in limited.stderr.lower()
    target = tmp_path / "space root"
    imported = run("import", archive, "--data-root", target)
    assert imported.returncode == 0, imported.stderr
    env_result = subprocess.run(
        ["/bin/sh", "-c", '. "$1"; printf "%s" "$DATABASE_URL"', "sh", str(target / "citara.env")], capture_output=True, text=True
    )
    assert env_result.stdout == f"sqlite:///{target}/citara.db"
    overwritten = run("import", archive, "--data-root", target)
    assert overwritten.returncode == 2 and "Traceback" not in overwritten.stderr


def test_atomic_publish_does_not_replace_racing_empty_directory(tmp_path):
    from citara.core.bundles import BundleError, _publish_directory

    staged, target = tmp_path / "staged", tmp_path / "target"
    staged.mkdir()
    (staged / "sentinel").write_text("corpus")
    target.mkdir()
    with pytest.raises(BundleError, match="exist"):
        _publish_directory(staged, target)
    assert target.is_dir() and list(target.iterdir()) == []
    assert (staged / "sentinel").read_text() == "corpus"


def test_export_rejects_unsafe_artifact_names_and_sqlite_sidecar_symlink(corpus, tmp_path):
    from citara.core.bundles import BundleError

    (corpus / "source-artifacts").mkdir()
    bad = corpus / "source-artifacts/C:unsafe"
    bad.write_text("not portable")
    with pytest.raises(BundleError, match="path"):
        export(corpus, tmp_path / "unsafe.zip", include_artifacts=True)
    assert not (tmp_path / "unsafe.zip").exists()
    bad.unlink()
    (corpus / "citara.db-wal").symlink_to(tmp_path / "other")
    with pytest.raises(BundleError, match="[Ss]ymlink"):
        export(corpus, tmp_path / "wal-link.zip")


@pytest.mark.parametrize("name", [".env", "credentials.json", "nested/.env.local", "logs/debug.log", "__pycache__/x.pyc"])
def test_optional_artifacts_never_include_secret_or_transient_files(corpus, tmp_path, name):
    from citara.core.bundles import BundleError

    path = corpus / "source-artifacts" / name
    path.parent.mkdir(parents=True)
    path.write_text("private")
    with pytest.raises(BundleError, match="[Ss]ecret|[Tt]ransient"):
        export(corpus, tmp_path / "secrets.zip", include_artifacts=True)
    assert not (tmp_path / "secrets.zip").exists()


@pytest.mark.parametrize("kind", ["view", "columns", "foreign-key"])
def test_export_checks_schema_tables_columns_and_relations(corpus, tmp_path, kind):
    from citara.core.bundles import BundleError

    with sqlite3.connect(corpus / "citara.db") as db:
        if kind == "view":
            db.executescript("DROP TABLE chunks; CREATE VIEW chunks AS SELECT id, id AS source_id FROM sources;")
        elif kind == "columns":
            db.executescript("DROP TABLE chunks; CREATE TABLE chunks(wrong TEXT);")
        else:
            db.execute("UPDATE chunks SET source_id='absent'")
    with pytest.raises(BundleError, match="schema|[Ff]oreign"):
        export(corpus, tmp_path / "invalid.zip")


def test_export_failure_leaves_no_partial_archive(corpus, tmp_path, monkeypatch):
    original = zipfile.ZipFile.write

    def interrupted(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError("simulated disk failure")

    monkeypatch.setattr(zipfile.ZipFile, "write", interrupted)
    output = tmp_path / "failed.zip"
    with pytest.raises(OSError, match="disk"):
        export(corpus, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".citara-export-*"))


@pytest.mark.parametrize("body", [b"no JSON", b"[]", b"{}", b"null"])
def test_malformed_manifest_is_clear_error(tmp_path, body):
    from citara.core.bundles import BundleError, inspect_bundle

    archive = tmp_path / "malformed.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", body)
    with pytest.raises(BundleError):
        inspect_bundle(archive)


@pytest.mark.parametrize(
    "setting,value",
    [("max_members", True), ("max_file_bytes", 1.5), ("max_compression_ratio", float("nan")), ("max_compression_ratio", float("inf"))],
)
def test_invalid_resource_limits_fail_closed(setting, value):
    from citara.core.bundles import BundleError, BundleLimits

    with pytest.raises(BundleError):
        BundleLimits(**{setting: value})


def test_optional_artifact_root_must_be_directory(corpus, tmp_path):
    from citara.core.bundles import BundleError

    (corpus / "source-artifacts").write_text("not a directory")
    with pytest.raises(BundleError, match="directory"):
        export(corpus, tmp_path / "bad.zip", include_artifacts=True)


def test_archive_filenames_can_be_relative_but_data_root_cannot(corpus, tmp_path, monkeypatch):
    from citara.core.bundles import import_bundle, inspect_bundle

    monkeypatch.chdir(tmp_path)
    manifest = export(corpus, Path("relative.zip"))
    assert inspect_bundle(Path("relative.zip")) == manifest
    assert import_bundle(Path("relative.zip"), data_root=tmp_path / "target") == manifest


def test_windows_database_url_uses_forward_slashes():
    from pathlib import PureWindowsPath

    from citara.core.bundles import _environment

    manifest = {"embedding": {"provider": "local", "model": "hash", "dimensions": 8}, "tenant_id": "local", "user_id": "owner"}
    env = _environment(PureWindowsPath("C:/Corpus Space"), manifest)
    assert "DATABASE_URL='sqlite:///C:/Corpus Space/citara.db'" in env


def test_windows_publication_does_not_require_posix_libc(tmp_path, monkeypatch):
    import citara.core.bundles as bundles

    staged, target = tmp_path / "staged", tmp_path / "target"
    staged.mkdir()
    monkeypatch.setattr(bundles.sys, "platform", "win32")

    def no_libc(*args, **kwargs):
        raise AssertionError("POSIX libc unavailable on Windows")

    monkeypatch.setattr(bundles.ctypes, "CDLL", no_libc)
    bundles._publish_directory(staged, target)
    assert target.is_dir() and not staged.exists()


def test_duplicate_json_keys_are_rejected(corpus, tmp_path):
    from citara.core.bundles import BundleError, inspect_bundle

    archive = tmp_path / "duplicate-json.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", b'{"format": "foreign", "format": "citara-corpus"}')
    with pytest.raises(BundleError, match="[Dd]uplicate"):
        inspect_bundle(archive)
