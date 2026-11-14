# Portable setup procedure

## 1. Discover and install the skill

Resolve the active agent/profile's skill directory. In Hermes use `$HERMES_HOME/skills` (fallback `~/.hermes/skills` only when unset); in other agents use their documented skill mechanism. Do not modify another profile. Prefer the agent's normal skill installer/security review. For Hermes, through `terminal`:

```bash
hermes skills install cloudsecmentor/citara/skills/citara
```

Do not bypass a blocked security scan. If installing from a local checkout, copy the complete `skills/citara/` directory to a new `citara/` entry in that profile's skills directory, after checking for an existing entry. Other skill-capable agents can copy the same directory. If an agent has no skill support, use these instructions for this session and say persistence is not installed.

## 2. Install the runtime separately

Check Git, Python 3.11+, and `uv` availability with `terminal`. If missing, use official platform instructions and obtain approval for system-level installations. Native Windows uses PowerShell syntax; the shell examples below assume POSIX or WSL. The Python runtime and bundle helper are cross-platform.

Choose a NEW checkout and a NEW local data root outside it (for example, a `citara` checkout under the user's code directory and a sibling `citara-data`). Resolve absolute paths; never reuse an unexplained existing directory. Data-root paths must not contain `?` or control characters: `?` would be interpreted as a SQLite URL query separator rather than part of the filename. Paths with spaces and ordinary Unicode names are supported. Through `terminal`, using the actual selected paths:

```bash
git clone https://github.com/cloudsecmentor/citara.git "$REPO"
uv --directory "$REPO" sync --locked
```

Here `$REPO` is a placeholder variable you set to the resolved absolute checkout. Do not run `git pull`, checkout a different revision, or modify an existing dirty checkout without approval. Record the installed commit. A package-only install is not the default: Alembic migrations and bundle scripts currently require the checkout.

## 3. Choose fresh data or a supplied ZIP

### Supplied ZIP

Treat the attachment as untrusted. Never run included scripts or source a file from the ZIP. Use only the trusted helper in the installed checkout:

```bash
uv --directory "$REPO" run python scripts/citara_bundle.py inspect "$ZIP"
uv --directory "$REPO" run python scripts/citara_bundle.py import "$ZIP" --data-root "$DATA"
```

`$ZIP` is the resolved attachment path; `$DATA` must not exist. Import verifies payload hashes, DB integrity/counts, compatibility, and safe paths before publishing a new directory. A plain ZIP of arbitrary data is not a Citara bundle: ask the sender to use `export` rather than guessing its layout. Compare the manifest embedding configuration to the sender's known settings; configuration is declarative, not proof every row was embedded correctly.

If the helper reports incompatible Citara version/schema, stop and explain. Obtain a compatible trusted runtime or plan an explicit backed-up migration of a separate copy; never migrate the original ZIP or production data automatically. A recorded Git commit is provenance, not authorization to execute arbitrary code from it.

Import generates `$DATA/corpus.json` and `$DATA/citara.env` locally. Read both with `read_file`. The env file contains destination-specific absolute paths and non-secret runtime settings, not sender credentials. Never copy a sender's `.env`. For external embeddings, start with `mode="keyword"`; only enable hybrid/vector retrieval after the recipient supplies their own compatible credentials through the client's secret mechanism. Azure also needs a recipient-controlled endpoint/deployment. Never ask for keys in chat or put them in the share ZIP.

### Fresh empty corpus

Create the selected data directory and a local `citara.env` using `write_file` or the agent's equivalent. Use absolute paths, a fresh nonexistent DB, and these explicit values (replace placeholders with resolved paths, quote values using dotenv syntax):

```dotenv
CITARA_SKIP_DOTENV=1
CITARA_DATA_ROOT=/absolute/local/citara-data
DATABASE_URL=sqlite:////absolute/local/citara-data/citara.db
SOURCE_ARTIFACT_ROOT=/absolute/local/citara-data/source-artifacts
SOURCE_STATE_ROOT=/absolute/local/citara-data/import-state
OBJECT_STORE_PATH=/absolute/local/citara-data/object-store
EMBEDDING_PROVIDER=local
EMBEDDING_MODEL=deterministic-hash-v1
EMBEDDING_DIMENSIONS=8
TRANSLATION_PROVIDER=noop
DEFAULT_TENANT_ID=local
DEFAULT_USER_ID=owner
```

For Windows the SQLite URL uses `sqlite:///C:/.../citara.db`, not POSIX's four-slash absolute form. Do not inherit an unrelated `DATABASE_URL` or provider from the shell. The trusted launcher below overrides all listed settings before importing Citara, without changing the user's persistent environment. Use forward slashes in Windows SQLite URLs and quote paths with spaces in the settings file.

Create the schema only for this fresh corpus:

```bash
uv --directory "$REPO" run python scripts/run_citara.py --env-file "$DATA/citara.env" migrate
```

Do not run migrations as a generic connectivity check against an existing/imported database.

## 4. Connect MCP with explicit paths

Use the same clean subprocess environment and env file for schema creation and every runtime launch. Set the client's MCP command to the absolute `uv` executable and these arguments (substitute resolved paths; `--directory` sets cwd, unlike `--project`):

```json
{
  "command": "/absolute/path/to/uv",
  "args": ["--directory", "/absolute/path/to/citara", "run", "python", "scripts/run_citara.py", "--env-file", "/absolute/path/to/citara-data/citara.env", "mcp"]
}
```

The trusted `scripts/run_citara.py` launcher parses only allowlisted non-secret settings, overrides ambient database/provider configuration before importing Citara, and refuses missing databases. It never executes the env file as shell code. It also handles paths with spaces without relying on uv's own `--env-file` parsing. Use its `check` action to print the resolved non-secret settings; its `migrate` action initializes only a fresh nonexistent database. Use the client's documented config mechanism, preserve other servers, and ask before replacing a server name. Prefer a new name such as `citara-shared` when `citara` already exists.

For Hermes, inspect `hermes mcp list` and `hermes mcp add --help`, then use `hermes mcp add` rather than hand-editing YAML. For example, through `terminal` (the `--args` option must be last):

```bash
hermes mcp add citara-shared --command "$UV" --args --directory "$REPO" run python scripts/run_citara.py --env-file "$DATA/citara.env" mcp
hermes mcp list
hermes mcp test citara-shared
```

The same launcher works for fresh and imported corpora: it reads their actual provider/model/dimensions/tenant/user from `citara.env`, not hardcoded defaults. Do not configure non-local embeddings as `local` to avoid supplying credentials: keyword queries do not require provider credentials. Never hand-edit Hermes `config.yaml` or use a skill installer `--force` to bypass a blocked scan.

For Claude/Cursor/other agents, use their documented MCP registration UI/CLI and the equivalent command/args/env. Citara uses stdio, not an unauthenticated public HTTP endpoint. Read back the exact configuration target after changing it. A restart/fresh conversation may be needed before this agent can see tools.

## 5. Verify the actual corpus

Call `ping`, then `list_sources` and a real `search_knowledge(query=..., mode="keyword")` or `retrieve_context_pack(query=..., mode="keyword")` through the client's connected MCP tools. For imported data, compare the manifest counts and search a distinctive phrase from an existing source. Validate the returned source ID, exact source text, and citation; use timestamp links for transcripts when available. Never declare success from `ping`/tool discovery alone.

For an empty fresh setup, add one labeled synthetic smoke note using `add_text_source` and retrieve its exact phrase. Record its ID, explain that it is a test note, and do not silently delete it. If verification is performed with a standalone MCP SDK client because the current session cannot reload tools, label that limitation and ask for the client restart before claiming live-agent connectivity.

## Sharing a corpus

Only export content the user has permission to redistribute. Verify the actual embedding and tenant settings first; do not assume defaults. For a corpus using the documented local defaults:

```bash
uv --directory "$REPO" run python scripts/citara_bundle.py export --data-root "$DATA" --output "$OUT" --embedding-provider local --embedding-model deterministic-hash-v1 --embedding-dimensions 8
```

`$OUT` must be a new ZIP path outside `$DATA`. Set the provider/model/dimensions to the corpus's real values; specify nondefault tenant/user with `--tenant-id` and `--user-id`. Add `--include-artifacts` only if needed and after reviewing those files for sensitive/redistribution-restricted content. Pause artifact writers during such an export; only the SQLite snapshot is transactionally consistent. Do not distribute `.env`, credentials, import-state, logs, or existing backups. The database itself contains source text and metadata; the export is the whole database, not a per-user redaction or selected-source export. Hashes detect corruption, not sender authenticity. ZIPs are not encrypted; use a trusted private transfer mechanism for sensitive data.
