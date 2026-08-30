# h2hdb-opds

`h2hdb-opds` exposes an H2HDB catalog as an OPDS 2.0 HTTP service. Its boundary
includes FastAPI/ASGI integration, OPDS models and serialization, navigation,
bounded publication pagination, authentication, and acquisition responses with
Range and conditional HTTP support. The search route is reserved but not
advertised until core supplies its bounded index.

Database connectors, schema migrations, durable queues, coordination fencing,
and catalog persistence remain owned by the `h2hdb` core package. This service
uses only the core catalog-reading public interface and opens database access
in read-only mode. Production startup delegates the exact epoch-3 `READY` audit
to core's `open_database()` and never initializes or migrates schema. A
caller-injected `CatalogReader` is treated as already initialized.

This release line targets `h2hdb>=0.26.0,<0.27` and only reads the greenfield
epoch-3 schema.

`h2hdb-opds` does not own or synchronize a second database. It reads the
core-owned H2HDB database: use the same SQLite file in read-only mode, or use a
dedicated read-only MariaDB account against the same database. The latter adds
database-server enforcement on top of the read-only mode forced by this
service.

## Configuration and startup

Create a JSON configuration such as:

```json
{
  "library_root": "/srv/h2hdb/library",
  "coordination_root": "/srv/h2hdb/coordination",
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

The former `artifact_root` setting has been removed and is rejected rather than
treated as an alias. There is no dual-root or content-addressed acquisition
compatibility path: configure both roots from the single-library deployment.

`library_root` is the only public current tree from which acquisitions can be
served. It must be an absolute, real directory rather than a symlink. The
service resolves only core-validated, stable `ArtifactStorageKey` segments;
symlinks at any level and non-regular files are rejected. Ingest is the only
writer and atomically replaces a completely written, fsynced CBZ before making
its catalog revision current. OPDS verifies the opened file's sealed size and
streams that descriptor directly. It does not persist a second CBZ, copy the
payload to a request spool, or rehash the whole file for HEAD, 304, Range, or
ordinary GET responses. The catalog SHA-256 remains the strong ETag because it
was verified at ingest activation.

The read-only mounts and ingest-only writer rule are therefore part of the
validator's trust boundary. A size mismatch fails 409 immediately; same-size
out-of-band mutation or storage bit rot is not rediscovered by every HTTP
request and must be detected by an explicit integrity audit. Transactional
recovery handles orderly stop, process loss, and power interruption; damaged
storage still requires a verified source or backup for reconstruction.

`coordination_root` is a separate read-only mount of the canonical library
parent's `.h2hdb-coordination` sibling. It contains only the permanent regular
file `publication.lock` and an optional `ACTIVATING` marker. The two configured
roots must be distinct and non-overlapping. Every
catalog response takes a nonblocking shared `flock`; contention or any marker
entry returns 503 with `Retry-After`. Acquisition holds that lock while it pins
the current revision, opens and validates the CBZ descriptor, and revalidates
the head. Streaming can then continue from the opened immutable inode while a
later activation atomically replaces the pathname. A marker left by power loss
or writer termination intentionally keeps OPDS fail-closed until ingest
reconciles the activation.

For Docker Compose, mount only the public `current` and sibling coordination
subtrees read-only:

```yaml
volumes:
  - /volume1/h2hdb/comics/current:/srv/h2hdb/library:ro
  - /volume1/h2hdb/comics/.h2hdb-coordination:/srv/h2hdb/coordination:ro
```

Do not mount the `comics` parent into OPDS. In particular, do not expose
ingest's private `.h2hdb-state` staging, journal, quarantine, or lock
directories. The OPDS container UID/GID must be able to read the CBZ files and
`publication.lock`. Komga can mount the same host `comics/current` directory at
its configured `_oneshots` path; this does not create another filesystem copy.

Synology's Compose stop action is a supported maintenance path. A graceful
SIGTERM lets active responses finish; when the process exits, the operating
system closes every CBZ and lock descriptor. A forced termination also releases
reader locks automatically. OPDS never creates durable coordination state, so
it cannot leave an `ACTIVATING` marker. If ingest was terminated during its
exclusive activation instead, its durable marker remains and the next ingest
startup must reconcile it before readers resume.

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
  "library_root": "/srv/h2hdb/library",
  "coordination_root": "/srv/h2hdb/coordination",
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

The database must already expose the current epoch-3 `READY` schema; catalog
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
- `GET /opds/v2/publications?cursor=TOKEN&limit=50&revision=N`
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

Acquisitions use artifact metadata from the selected published revision and
the same single CBZ tree read by Komga. They
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
`hash-v1/13/8/h2h-1234567.cbz` remain an implementation detail rather than an
OPDS 2.0 requirement.

Every generated feed, publication, pagination, and acquisition link carries its
selected catalog revision. An omitted revision selects the current head; an
explicit revision is accepted only when it equals that head. The resolved
`CatalogRevision` is passed into every core read so one response cannot mix
revisions. After a newer head is published, links pinned to the previous
revision return 404 instead of exposing history or silently switching to current
data.

OPDS publication pages use core's revision-bound artifact-only seek cursor and
immutable `artifact_count`. This keeps feed totals, navigation links, and
OPDS's required acquisition links consistent without a per-page `COUNT(*)` or
deep `OFFSET` scan. Page sizes are capped at 128. Cursors are opaque,
canonically encoded, and bound to the exact revision, order position, and
publication identity; tampering returns 422 and links for a superseded current
revision return 404. Cursor feeds expose `self`, `first`, and forward `next`
links rather than fabricating an expensive random-access `last` page.
The cursor is stateless HTTP input, not mutable server progress: interruption
does not leave a cursor table to repair, and a client can retry the same pinned
link idempotently.
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
