# CLAUDE.md

This file provides repository guidance to Claude Code and other coding agents.

## Communication

- Claude 必須以繁體中文回答所有對話內容；程式碼、指令、檔名與專有名詞維持原文。

## Project

`h2hdb-opds` exposes an H2HDB catalog through an OPDS 2.0 HTTP service. It owns
FastAPI and ASGI integration, OPDS models and serialization, navigation,
bounded publication pagination, an explicit 501 search stub until core has its
bounded index, authentication, and acquisition responses including Range and
conditional HTTP behavior.

The `h2hdb` core package exclusively owns database connectors, transactions,
schema migrations, durable queues, coordination fencing, and catalog
repositories. Use only core public interfaces. Never import connector or
repository internals, create or migrate schema, or expose unpublished catalog
state. Database access is always forced to read-only. Production startup calls
the public `open_database()` v1-v7 migration-ledger compatibility path. An
injected `CatalogReader` is a caller-supplied, already-initialized boundary and
is used directly; it does not need a `SchemaCompatibility` or
`check_compatibility()` hook. No startup path may call `migrate()`.

## Module Layout and HTTP Invariants

- `config.py` owns the frozen OPDS, server, authentication, and nested core
  configuration models.
- `app.py` wires the FastAPI lifespan and routes to a public
  `CatalogReader`.
- `serialization.py` owns OPDS feed, publication, navigation, and pagination
  serialization. It does not advertise unavailable search.
- `auth.py` owns Basic authentication and the public OPDS authentication
  document. Credential comparisons must use `secrets.compare_digest`.
- `acquisition.py` owns artifact file responses and HTTP validators/ranges.

Feed responses use `application/opds+json`; standalone publications use
`application/opds-publication+json`. Acquisition ETags are strong SHA-256
validators from the verified artifact. Apply the RFC precondition order from
`If-Match` through `If-Modified-Since`. Support only one byte range; invalid,
multiple, and unsatisfiable byte ranges return 416 with `Content-Range`, unknown
range units are ignored, and an `If-Range` mismatch returns the complete 200
representation. An `If-Range` date comparison is exact.

All acquisition paths must remain under the configured real artifact root and
must be opened without following symlinks. Verify size and SHA-256 before sending
a strong validator. Absolute links come only from the canonical public base URL,
never the request Host header. Basic authentication requires an effective HTTPS
request and either local TLS or an explicitly trusted TLS-terminating proxy.
Catalog listing and counts use core's pinned `require_artifact` filter so every
serialized OPDS Publication has an acquisition link.

All feed, publication, pagination, and acquisition links carry their selected
revision. Resolve an omitted revision to the current snapshot and an explicit
revision through core's public revision-history lookup. Map a missing revision
to an explicit 404, and pass the resolved `CatalogRevision` into every list,
publication, and artifact read. Never fall back from a missing requested
revision to current data or mix snapshots across pagination.

## Development

This is an independent repository and not part of a uv workspace. `uv.lock` is
ignored and must not be used by build, test, or runtime workflows. Always run
Python through `uv run --no-sync` after installing the editable environment.

```bash
uv venv --python 3.14
uv pip install -e ".[dev]"
uv run --no-sync ruff check .
uv run --no-sync black --check .
uv run --no-sync mypy src tests
uv run --no-sync pytest
uv run --no-sync python -m build
```

Use `scripts/rebuild-env.sh` to recreate a damaged environment.

## Shared Finalization

Provider-neutral hooks live in `scripts/hooks/` and are shared by humans and
all coding agents.

- After Python changes, run `bash scripts/hooks/finalize-python.sh`.
- After Markdown changes, run `bash scripts/hooks/finalize-markdown.sh`.

Do not duplicate their implementation under an agent-specific directory.

## Design

This project is pre-1.0. Prefer the cleanest architecture over compatibility
shims and deprecated aliases. Follow SOLID principles. Keep authentication,
file I/O, and response streaming separate from catalog query construction, and
never perform schema mutation during service startup.

Keep this document synchronized with `AGENTS.md` whenever ownership, workflow,
testing, or tooling conventions change.
