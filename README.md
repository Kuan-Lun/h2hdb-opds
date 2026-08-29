# h2hdb-opds

`h2hdb-opds` exposes an H2HDB catalog as an OPDS 2.0 HTTP service. Its boundary
includes FastAPI/ASGI integration, OPDS models and serialization, navigation,
bounded publication pagination, authentication, and acquisition responses with
Range and conditional HTTP support. The search route is reserved but not
advertised until core supplies its bounded index.

Database connectors, schema migrations, durable queues, coordination fencing,
and catalog persistence remain owned by the `h2hdb` core package. This service
uses only the core catalog-reading public interface and opens database access
in read-only mode. Production startup delegates the exact epoch-2 `READY` audit
to core's `open_database()` and never initializes or migrates schema. A
caller-injected `CatalogReader` is treated as already initialized.

This release line targets `h2hdb>=0.25.0,<0.26` and only reads the greenfield
epoch-2 schema.

`h2hdb-opds` does not own or synchronize a second database. It reads the
core-owned H2HDB database: use the same SQLite file in read-only mode, or use a
dedicated read-only MariaDB account against the same database. The latter adds
database-server enforcement on top of the read-only mode forced by this
service.

## Configuration and startup

Create a JSON configuration such as:

```json
{
  "artifact_root": "/srv/h2hdb/artifacts",
  "public_base_url": "https://books.example.net",
  "core": {
    "database": {
      "sql_type": "sqlite",
      "database": "/srv/h2hdb/catalog.sqlite3",
      "access_mode": "read-only"
    }
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8000,
    "tls_certificate": "/run/secrets/opds.crt",
    "tls_private_key": "/run/secrets/opds.key"
  },
  "title": "My H2HDB Catalog",
  "default_page_size": 50,
  "maximum_page_size": 128,
  "auth": {
    "username": "reader",
    "password": "${H2HDB_OPDS_AUTH_PASSWORD}",
    "realm": "My Catalog"
  }
}
```

JSON string values that consist exactly of `${ENV_NAME}` are resolved
recursively before validation. This applies equally to nested core credentials
and OPDS authentication, for example
`"password": "${H2HDB_OPDS_DB_PASSWORD}"` under `core.database`. Inline
interpolation is not performed. Missing or invalid variable names stop startup
without exposing the environment value, and strict unknown-field rejection is
unchanged.

`core.database.access_mode` is always forced to `read-only`, even if a supplied
configuration requests write access. Omit both `auth.username` and
`auth.password` to disable Basic authentication.

`artifact_root` is the only filesystem tree from which acquisitions can be
served. It must be an absolute, real directory rather than a symlink. Published
artifact paths outside this tree, symlinks at any level below it, and non-regular
files are rejected. Before returning even a conditional or HEAD response, the
service verifies both the published size and SHA-256 digest and serves from that
verified snapshot. In Docker Compose, run the OPDS container with a UID/GID that
can read ingest's artifact files, or grant equivalent group permissions. Aligning
the container UID is sufficient for ingest-created `0600` files; no broader file
mode is required.

The secure descriptor-relative open policy relies on POSIX filesystem semantics,
which matches the supported Linux container and macOS deployments.

`public_base_url` is the canonical externally visible URL, including any reverse
proxy path prefix. Generated links never trust the incoming `Host` header.

Basic authentication is accepted only over HTTPS. Choose one explicit TLS
boundary:

- configure `server.tls_certificate` and `server.tls_private_key` together to
  terminate TLS in this process; or
- terminate TLS at a reverse proxy and list only that proxy's addresses or CIDR
  networks in `server.trusted_proxy_ips`.

For example, a loopback reverse proxy configuration uses:

```json
{
  "artifact_root": "/srv/h2hdb/artifacts",
  "public_base_url": "https://books.example.net",
  "auth": {
    "username": "reader",
    "password": "${H2HDB_OPDS_AUTH_PASSWORD}",
    "realm": "My Catalog"
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8000,
    "trusted_proxy_ips": ["127.0.0.1"]
  }
}
```

The proxy must set the forwarded scheme to `https`. Untrusted forwarded headers
are ignored, trusting every proxy address (`"*"`) is rejected, and protected
requests that still arrive with an insecure effective scheme return 426 without
issuing a Basic challenge. The public authentication document remains available
without credentials.

The database must already expose the current epoch-2 `READY` schema; catalog
routes return no feed until at least one revision has been published. Initialize
a truly empty database first with core-owned `migrate` tooling and writer
credentials (normally through the ingest deployment), then start this read-only
service:

```bash
.venv/bin/h2hdb-opds --config opds.json
```

## HTTP API

- `GET /health`
- `GET /opds/v2`
- `GET /opds/v2/publications?offset=0&limit=50&revision=N`
- `GET /opds/v2/search?query=term` (reserved; returns 501 until the bounded
  core search index is available)
- `GET /opds/v2/publications/{publication_id}?revision=N`
- `GET|HEAD /opds/v2/acquisitions/{artifact_id}?revision=N`
- `GET /opds/v2/authentication`

OPDS feeds use `application/opds+json`; standalone publications use
`application/opds-publication+json`. The authentication document is always
public. When Basic authentication is configured, failed protected requests
return that document as `application/opds-authentication+json` together with
`WWW-Authenticate` and an authentication `Link` header.

Acquisitions use artifact metadata from the selected published revision. They
expose a strong SHA-256 ETag, `Last-Modified`, `Accept-Ranges: bytes`, GET and HEAD,
single closed/open/suffix byte ranges, `If-Match`, `If-Unmodified-Since`,
`If-None-Match`, `If-Modified-Since`, and `If-Range` with RFC conditional request
ordering. An `If-Range` HTTP date must exactly match `Last-Modified`.
Invalid, unsatisfiable, or multiple ranges return 416 with
`Content-Range: bytes */size`; unknown range units are ignored as required by
HTTP semantics.

The download filename comes from the neutral artifact name in the published
catalog, never from its storage path. Responses include safe `filename` and
UTF-8 `filename*` parameters in `Content-Disposition`; storage names such as
`gid-sha256.cbz` remain an implementation detail rather than an OPDS 2.0
requirement.

Every generated feed, publication, pagination, and acquisition link carries its
selected catalog revision. An omitted revision selects the current head; an
explicit revision is accepted only when it equals that head. The resolved
`CatalogRevision` is passed into every core read so one response cannot mix
revisions. After a newer head is published, links pinned to the previous
revision return 404 instead of exposing history or silently switching to current
data.

OPDS publication pages request only rows with at least one artifact through
core's pinned-revision `require_artifact` filter. This keeps feed totals,
navigation counts, page links, and OPDS's required acquisition links consistent.
Page sizes are capped at 128 and offsets must align to the selected page size.
Search is deliberately not advertised while the core's bounded search index is
an explicit readiness blocker; the reserved route returns 501 instead of
performing an unbounded scan. Blank optional metadata is omitted, language tags
are normalized and validated as BCP 47, and acquisition Link Objects expose the
standard `size` field.

## Development

Rebuild the repository-local environment and run its canonical gates:

```bash
./scripts/rebuild-env.sh
./scripts/check-fast.sh
./scripts/check-full.sh
```

The rebuild script uses `uv` only to create `.venv` and install through its pip
interface; it never reads `uv.lock` or assumes an adjacent repository checkout.

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
