# Security Policy

## Supported versions

This project is currently an early personal-use alpha. Security fixes are handled on the `main` branch until versioned releases exist.

## Reporting vulnerabilities

Please report security issues privately to the maintainer instead of opening a public issue. Include:

- affected version or commit;
- reproduction steps;
- impact;
- suggested fix, if known.

## Secrets and local data

Do not commit:

- `.env` files;
- `.azure/` files;
- API keys or provider credentials;
- database dumps;
- ingested third-party transcripts, audio, PDFs, or other copyrighted content;
- local Docker volumes or generated caches.

The repository includes `.gitignore` rules for common local artifacts, but users remain responsible for reviewing changes before committing.

## Third-party content

Hermes Knowledge Vault can ingest external content such as podcast transcripts. Users are responsible for ensuring they have the right to ingest, store, process, and redistribute any external content they use with the software.
