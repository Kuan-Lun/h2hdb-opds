# Agent Instructions

## Project

`h2hdb-opds` is the OPDS 2.0 HTTP service for H2HDB catalogs. Python must be
run through `uv run --no-sync`, using the repository-local virtual environment.
The supported Python version is defined by `requires-python` in
`pyproject.toml`.

## Ownership Boundary

This repository owns:

- FastAPI and ASGI integration;
- OPDS 2.0 models and serialization;
- navigation, publications, search, and pagination;
- authentication;
- acquisition, Range, and conditional HTTP responses.

The `h2hdb` core package exclusively owns connectors, transactions, database
schema and migrations, durable queues, coordination fencing, and catalog
repositories. Depend only on core public interfaces. Do not import connector or
repository internals and do not create or migrate schema here. Database access
is always forced to read-only, and the service exposes only published catalog
revisions. Production startup uses `open_database()` for its compatibility
check. Injected readers are checked through their public `check_compatibility()`
method. No startup path may call `migrate()`.

## Module Layout and HTTP Invariants

- `config.py` owns the frozen OPDS, server, authentication, and nested core
  configuration models.
- `app.py` wires the FastAPI lifespan and routes to a compatible public
  `CatalogReader`.
- `serialization.py` owns OPDS feed, publication, navigation, search template,
  and pagination serialization.
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

## Environment and Commands

This is a standalone repository, not a uv workspace. `uv.lock` is ignored and
must not become a build, test, or runtime input.

```bash
uv venv --python 3.14
uv pip install -e ".[dev]"
uv run --no-sync ruff check .
uv run --no-sync black --check .
uv run --no-sync mypy src tests
uv run --no-sync pytest
uv run --no-sync python -m build
```

If the environment is damaged, run `scripts/rebuild-env.sh`.

## Shared Finalization

The repository keeps provider-neutral hooks in `scripts/hooks/`.

- After changing Python, run `bash scripts/hooks/finalize-python.sh`.
- After changing Markdown, run `bash scripts/hooks/finalize-markdown.sh`.

Do not create agent-specific copies of these implementations.

## Design and Compatibility

The project is pre-1.0. Prefer clear responsibility boundaries and the cleanest
end state over compatibility shims or deprecated aliases. Follow SOLID
principles. Keep authentication, file I/O, and response streaming separate from
catalog query construction and never perform schema mutation at service startup.

`CLAUDE.md` documents the same repository rules. Keep both files synchronized
when changing workflow, ownership, testing, or tooling conventions.
