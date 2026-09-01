# h2hdb-opds

`h2hdb-opds` lets an OPDS reader browse and download the current H2HDB
publication catalog. It serves the same three top-level collections in both
protocol versions:

- All Publications
- Recently Uploaded
- Recently Downloaded

Use one of these URLs in your reader:

- OPDS 1.2: `https://books.example.net/opds/v1.2/catalog`
- OPDS 2.0: `https://books.example.net/opds/v2`

OPDS 1.2 is the best choice for readers that support OPDS-PSE comic page
streaming. OPDS 2.0 provides JSON feeds, a standalone publication document and
the OPDS Authentication document. Both versions expose search, facets, cover
art, thumbnails, downloads and the same current catalog revision.

## Quick start

This service requires Python 3.14, an H2HDB 0.28 database whose epoch 3/schema
version 2 is already `READY`, the read-only ingest `current` tree, and its
sibling coordination directory. It never creates or migrates the database.

Create `opds.json`:

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
    "trusted_proxy_ips": ["127.0.0.1"]
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

Then rebuild the local environment and start the server:

```bash
./scripts/rebuild-env.sh
.venv/bin/h2hdb-opds --config opds.json
```

This release is a deliberate clean break. It accepts only cursor format v2 and
the core discovery query-hash v2 contract; old saved cursor URLs return 422 and
must be replaced by reopening the catalog. It also requires a database built
from the current H2HDB epoch-3 manifest, including discovery, facet and
presentation authorities. H2HDB has no compatibility migration or dual-read
path for an older/foreign manifest: create a new blank database and republish
the catalog. Storage likewise accepts only `managed-filesystem-v2` descriptors
for the `acquisitions/hash-v2` and `artwork/hash-v2` trees. Legacy
`current/hash-v1` is neither read nor migrated; ingest must build a fresh
`current` tree. No legacy identifier, query-name, cursor or storage shim is
retained.

A JSON string that consists exactly of `${ENV_NAME}` is replaced recursively
from the environment before validation. This works for OPDS and nested database
credentials. Missing variables stop startup without logging their values.

To disable Basic authentication, omit both `auth.username` and `auth.password`.
When authentication is enabled, `public_base_url` must use HTTPS and TLS must be
terminated either by this process or by an address listed in
`server.trusted_proxy_ips`. Untrusted forwarded headers are ignored.

## What is implemented

“Specification” below describes what the protocol can represent. “This server”
states what `h2hdb-opds` actually sends; a blank in one protocol must not be
read as a missing implementation in the other.

| Capability | OPDS 1.2 specification | This server 1.2 | OPDS 2.0 specification | This server 2.0 |
| --- | --- | --- | --- | --- |
| Catalog navigation | Atom navigation feeds | Same three collections | Navigation and group collections | Same three collections, grouped as Browse and Recent Activity |
| All Publications listing | Acquisition feed | Yes, cursor-paged; not marked crawlable/complete | Publications collection | Yes, cursor-paged |
| Search | OpenSearch description and search link | Yes | Templated search link | Yes |
| Facets | OPDS facet links | Language, tag and contributor; fully page-followable | Facet collections | Language, tag and contributor; fully page-followable |
| Standalone publication | Atom entry document | Yes | Publication document | Yes |
| Cover and thumbnail | Image relations | Yes | `images` links | Yes |
| Page count | Extension metadata | PSE `count` | `numberOfPages` | Emitted when greater than zero |
| Comic page streaming | OPDS-PSE extension | Yes | No normative OPDS 2 PSE form | Not emitted |
| Groups | No equivalent groups collection | Not applicable | `groups` collection | Root navigation is split into Browse and Recent Activity groups |
| Acquisition | OPDS acquisition relations | CBZ GET/HEAD/Range | Acquisition Link Objects | CBZ GET/HEAD/Range |
| Shelf/subscriptions discovery | Optional `opds:shelf` and `opds:subscriptions` relations | Not advertised or stored | No general shelf collection | Not stored |
| Reading progress | No persistent reading-progress model | Not stored | No core reading-progress feature | Not stored |
| Buy, borrow, subscribe and preview workflows | Acquisition relations, prices and indirect acquisitions | Not implemented | Acquisition relations, prices and indirect acquisitions | Not implemented |

The server does not claim every optional OPDS extension. In particular, it does
not advertise OPDS 1.2 shelf/subscriptions resources and does not keep per-user
shelves, subscription state, last-read positions or reading progress. The
`subscribe` acquisition relation in either OPDS version describes how a
publication may be acquired through a subscription; it is distinct from the
OPDS 1.2 `opds:subscriptions` discovery relation.

Other deliberately unimplemented breadth includes an OPDS 1.2 complete,
crawlable feed; popular, featured and recommendation relations or collections;
field-specific advanced OpenSearch beyond `searchTerms`; advanced OPDS 2
metadata search; multi-format acquisition alternatives; and the full breadth of
Readium publication metadata. Most of these are optional
discovery or vocabulary features. Their absence is not a conformance failure
for the documents and links this server does emit.

Only one direct CBZ download per publication is implemented. Every published
artifact must have the exact `application/vnd.comicbook+zip` media type at the
OPDS boundary;
unsupported adapter output makes the catalog temporarily unavailable instead
of being advertised as a supported acquisition. The server does not serialize
buy/borrow/subscribe/preview acquisition workflows, prices or nested indirect
acquisitions.

Every schema-governed Atom feed/entry and OPDS 2 feed/publication shape emitted
by the service is represented in an offline conformance corpus. The unmodified
official OPDS 1.2 RELAX NG conversion is compiled separately. Runtime Atom
documents use a strict derived grammar whose only change corrects the pinned
Atom RNC's CR/TAB typo. A shared validator first checks an original OPDS-PSE
link's exact relation, JPEG type, count and single literal `{pageNumber}` token,
then replaces that token only in a validation copy; the grammar itself is not
weakened. OPDS 2 uses the official schemas and the complete Readium Web
Publication Manifest reference closure. OpenSearch and Authentication
documents have dedicated semantic/shape tests because these vendored OPDS
schema sets do not define schemas for them. Raw sources, generated outputs,
the runtime generator and all hashes are in `verification/opds/sources.toml`.

## Browse, search and filter

The All Publications collection is ordered by the catalog's stable publication
order. Pagination means a large result is split into bounded responses instead
of being loaded all at once. This server uses seek pagination, not numbered
pages or database offsets. Follow the feed's `next` link exactly: its opaque
cursor records the last boundary and is bound to the complete query and selected
revision. A malformed cursor returns 422; a cursor for a superseded revision
returns 404.

Recently Uploaded and Recently Downloaded are fixed complete top-128 windows.
They are not paginated and reject `cursor`, `limit` and `offset`. “Downloaded”
means the immutable source download timestamp published by H2HDB, not the time
an OPDS user fetched the CBZ.

Free-text search and facets are available at:

```text
GET /opds/v1.2/search?q=alice
GET /opds/v2/search?query=alice
```

OPDS 2 follows the specification's required search parameter name `query`.
The former `q` spelling is rejected with 422 and is not an alias. OPDS 1.2
continues to use `q`; its OpenSearch description maps `{searchTerms}` to it.
The search endpoint requires a nonblank searchable query. Missing, whitespace-
only, punctuation-only and over-complex queries return 422 instead of becoming
an unfiltered All Publications request.

Search covers only the display title, source title, contributor names and source
tag values. It deliberately does not index the summary, publication identifier
or artifact filename. H2HDB applies its pinned Unicode normalization/case-fold
tokenizer and requires every distinct query lexeme to occur (AND semantics).
Queries contain at most 16 distinct searchable lexemes and 1024 canonical-NFD
UTF-8 bytes. There is no fuzzy matching, stemming, phrase ranking or relevance
order; results remain in the catalog's stable publication order.

Optional filters are:

- `language=VALUE`
- `tag=VALUE&tag_namespace=NAMESPACE`
- `contributor=NAME&role=ROLE`
- `limit=1..128`

`tag` and `tag_namespace` must occur together because a source tag is only
unique within its exact namespace. `contributor` and `role` must also occur
together. Registered roles are `artist`, `author`, `cosplayer`, `group`,
`illustrator` and `uploader`. Filter bytes are not trimmed, Unicode-normalized
or whitespace-collapsed; following the generated facet link is the safest way
to preserve an exact value.

`/publications` also accepts these exact filters without a free-text query, so a
reader can browse and follow facets from All Publications. Each discovery feed
shows a bounded facet window. If a catalog has more values,
the facet contains a followable “More”/`next` link to
`/facets/language`, `/facets/subject` or `/facets/contributor`. No 129th tag or
author is silently discarded and the server never builds an unbounded facet
response.

## Publications, covers and comic pages

A standalone publication endpoint returns one publication rather than a list:

```text
GET /opds/v1.2/publications/{publication_id}?revision=N
GET /opds/v2/publications/{publication_id}?revision=N
```

It is useful when a reader wants current metadata and acquisition links for one
book without parsing a feed. The only accepted identifier form is the canonical
core identity `urn:h2h:gallery:<gid>`, where `gid` is an ASCII decimal integer
from 1 through 2^63-1 with no leading zero. Zero, Unicode digits, arbitrary URIs
and malformed internal identifiers fail closed in both protocol versions. The
decimal suffix must also equal the publication's authoritative H2HDB `gid`; a
canonical-looking but mismatched identity makes the catalog temporarily
unavailable instead of leaking inconsistent links.

Publication metadata includes that identifier, display title, textual summary,
publication/update timestamps, valid BCP 47 language, contributors and source
subjects where available. Acquisition links include exact media type, filename
and byte length. Page-bearing publications additionally include page count,
cover and thumbnail links; unavailable optional facts are omitted rather than
invented.

Ingest prepares presentation descriptors. H2HDB stores the immutable,
revision-scoped presentation authority in normalized database relations: page
count plus each page/cover/thumbnail storage key, logical byte extent, media
type, digest and dimensions. The image and CBZ bytes themselves stay in ingest's
read-only `current` storage tree. Keeping queryable identity/integrity facts in
the database and large immutable bytes in storage avoids database blobs while
still letting OPDS fail closed. OPDS does not parse a ZIP or resize an image
during a request. For a publication with pages:

- page `0` is the cover;
- this server bounds page count to 0..4096 at its OPDS presentation boundary;
- the thumbnail is a separately prepared 320-pixel image;
- OPDS 1.2 advertises standard cover/thumbnail relations and one OPDS-PSE stream
  with a literal `{pageNumber}` token;
- OPDS 2 advertises cover then thumbnail in `images`, plus
  `metadata.numberOfPages`;
- a zero-page publication omits images and `numberOfPages`.

Direct media routes are version-neutral:

```text
GET|HEAD /media/publications/{publication_id}/pages/{page_number}?revision=N
GET|HEAD /media/publications/{publication_id}/thumbnail?revision=N
```

Page numbers are zero-based. Page responses support one byte range whose
coordinates are relative to the logical JPEG page, even when that page is an
extent inside a CBZ. A missing page is 404. OPDS-PSE is intentionally only
advertised by OPDS 1.2; no non-standard OPDS 2 PSE claim is made. The server
does not emit PSE `lastRead` or `maxWidth`, and it does not split double pages.

## Artifact-only catalog and empty results

OPDS is an acquisition catalog. A publication is visible only when the current
revision has a sealed downloadable artifact. Metadata that exists in H2HDB but
has no artifact is not offered as a book the reader could download.

H2HDB revisions use an all-or-none artifact contract. An intentionally empty
catalog occurs when artifact publication is disabled: `artifact_count` is zero
even though metadata publications may exist, so there is nothing downloadable
to expose. Any partial state between zero and the publication count is corrupt,
and every catalog, search, detail, media and acquisition route returns a
retryable 503 instead of exposing a mixed revision. An
empty Atom feed has no entries. An empty OPDS 2 feed omits the schema-invalid
`"publications": []` member and includes a navigation fallback. A valid search
that matches nothing is also a normal empty result, not a database error.

## Downloads and HTTP behavior

Download routes are shared by both representations:

```text
GET|HEAD /opds/v1.2/acquisitions/{artifact_id}?revision=N
GET|HEAD /opds/v2/acquisitions/{artifact_id}?revision=N
```

Anonymous catalogs advertise the precise OPDS open-access relation. Catalogs
that require credentials advertise the generic acquisition relation because a
download is not anonymously open.

Acquisitions expose a strong SHA-256 ETag, `Last-Modified`,
`Accept-Ranges: bytes`, GET, HEAD, one closed/open/suffix byte range and the
standard conditional request headers. Invalid, multiple or unsatisfiable ranges
return 416 with `Content-Range: bytes */size`. The filename comes from catalog
metadata, never from a storage path.

All generated links carry `revision=N`. Omitting it selects the current head.
Supplying a revision only works while it is still the exact current head; the
server returns 404 after activation instead of silently switching an old link
to new data.

## Filesystem and container mounts

`library_root` must mount ingest's entire public `current` tree read-only. OPDS
needs both `current/acquisitions` for CBZ/page extents and `current/artwork` for
prepared thumbnails. `coordination_root` is a separate read-only mount of the
sibling `.h2hdb-coordination` directory:

```yaml
volumes:
  - /volume1/h2hdb/comics/current:/srv/h2hdb/library:ro
  - /volume1/h2hdb/comics/.h2hdb-coordination:/srv/h2hdb/coordination:ro
```

Komga should mount only the acquisition subtree, not the whole current tree,
so it does not scan thumbnails as publications:

```yaml
volumes:
  - /volume1/h2hdb/comics/current/acquisitions:/books/_oneshots:ro
```

Do not expose the parent directory or ingest's private staging, journal,
quarantine and lock directories. OPDS accepts only the exact
`managed-filesystem-v2` storage-key codec, rejects traversal and symlinks,
opens a regular file descriptor and checks its sealed object size and requested
extent before serving bytes.

The strong hash and immutable storage facts were verified when ingest activated
the revision. OPDS intentionally does not hash an entire CBZ on every HEAD,
304, Range or GET. The shared publication lock and current-head check are the
runtime immutability boundary; out-of-band same-size mutation must be found by
an explicit storage integrity audit.

Every catalog read takes a nonblocking shared lock. An active or interrupted
activation returns 503 with `Retry-After` until ingest reconciles it. OPDS never
creates coordination state. POSIX descriptor and `flock` behavior is required,
matching supported Linux containers and macOS deployments.

## Route reference

- `GET /health`
- `GET|HEAD /opds/v1.2/catalog`
- `GET|HEAD /opds/v1.2/publications`
- `GET|HEAD /opds/v1.2/search`
- `GET|HEAD /opds/v1.2/opensearch.xml`
- `GET|HEAD /opds/v1.2/facets/{language|subject|contributor}`
- `GET|HEAD /opds/v1.2/recent/{uploaded|downloaded}`
- `GET|HEAD /opds/v1.2/publications/{publication_id}`
- `GET|HEAD /opds/v1.2/acquisitions/{artifact_id}`
- `GET /opds/v2`
- `GET /opds/v2/publications`
- `GET /opds/v2/search`
- `GET /opds/v2/facets/{language|subject|contributor}`
- `GET /opds/v2/recent/{uploaded|downloaded}`
- `GET /opds/v2/publications/{publication_id}`
- `GET|HEAD /opds/v2/acquisitions/{artifact_id}`
- `GET /opds/v2/authentication`
- `GET|HEAD /media/publications/{publication_id}/pages/{page_number}`
- `GET|HEAD /media/publications/{publication_id}/thumbnail`

## Troubleshooting

- 404 on a previously copied link: its revision is no longer current; reopen
  the catalog root.
- 422 on search: use OPDS 2 `query`, provide tag and namespace together, provide
  contributor and role together, and follow opaque cursor links unchanged.
- 503 with `Retry-After`: ingest holds the activation lock or left an
  `ACTIVATING` marker that it must reconcile.
- 416 on media: only a single satisfiable byte range is supported.
- Catalog opens but has no books: the current revision has no published
  artifacts; metadata-only revisions intentionally produce an empty catalog.
- Basic auth never challenges over HTTP: configure an HTTPS public URL and a
  trusted TLS terminator or local certificate/key pair.

## Development

The service uses only public `h2hdb` catalog facades and forces database access
to read-only. It owns FastAPI integration, protocol serialization, cursor
encoding, filesystem/media adapters, authentication and HTTP responses; core
owns schema, transactions, revisions and catalog authorities.

Run the canonical local gates:

```bash
./scripts/rebuild-env.sh
./scripts/check-fast.sh
./scripts/check-full.sh
```

The full gate verifies immutable schema hashes; compiles both the unmodified and
strict-runtime OPDS 1.2 grammars plus the OPDS 2 closure with no network access;
runs positive schema-governed catalog corpora, PSE semantic negatives and
dedicated OpenSearch/Authentication shape tests; executes all tests; builds
sdist/wheel artifacts; and smoke-tests the installed wheel. `uv.lock` and
`package-lock.json` are not rebuild inputs and must not be committed.

### Scalability benchmark

The repository-local scalability benchmark uses a deterministic, preindexed
synthetic `CatalogReader`. It creates valid publication and one-byte artifact
descriptors in memory but no CBZ files. Run the automatic-sized profile or the
manual 10,000-publication profile with:

```bash
.venv/bin/python -m benchmarks.opds_scalability --profile smoke
.venv/bin/python -m benchmarks.opds_scalability --profile 10k --compact
```

Both commands emit only machine-readable JSON on stdout. `operation_order`
fixes the first discovery page, all three standalone facet families, a nonempty
search, a discovery cursor page and a subject-facet cursor page. Discovery,
search and discovery-cursor operations use the public
`discover_publications_with_facets` bundle; the reader records every requested
limit so tests can prove the route never falls back to an unbounded or legacy
listing call.

The latency pass has `tracemalloc` completely disabled. `first_sample_ns` is
the first invocation of that operation in one fresh timing-pass application;
later operations can still observe process and ASGI infrastructure warmed by
earlier entries in `operation_order`. Each `warm` sample immediately repeats
the same request. Status, exact body bytes, `Content-Type` and `Content-Length`
must remain identical. The fixed smoke manifest and response-body digests make
profile, fixture, config, query, cursor, operation-order and serialization drift
an explicit reviewed change rather than a silently different workload.

Every report also carries a path-independent source provenance manifest. Its
SHA-256 uses deterministic logical names, sizes and content hashes for this
checkout's `pyproject.toml`, every repository-local benchmark tool, every
`src/h2hdb_opds/**/*.py` file, and the `.py` tree of the actually imported
`h2hdb` package when that tree is locatable. A source-checkout import also binds
the corresponding core `pyproject.toml`; each component reports that canonical
project version. Canonical input never contains an absolute host path. The
report lists each logical file receipt so the manifest can be audited. Git
commit and dirty state and installed-distribution metadata are diagnostic only;
they are excluded from canonical digests.

Allocation measurements run separately. One fresh pass measures fixture and
index construction; another creates a new reader and application before
measuring request allocations. Every Python memory field is an explicitly
named baseline delta. `process_lifetime_max_rss_bytes` remains the operating
system's lifetime high-water mark across all three passes and is not attributed
to a single request. No host-dependent timing or memory threshold is enforced;
the smoke profile checks structural contracts while the 10k profile is a manual
measurement.

This remains explicitly a `serialization-only` result. Request timing includes
in-process ASGI routing, coordination locking, `CatalogService`, bounded
synthetic page slicing and OPDS 2 JSON serialization. It excludes SQL, database
startup, network transport, CBZ creation and media reads. Do not compare it to a
SQL-backed run without retaining that mode label.

The separate SQL-backed benchmark consumes the core-owned fixture instead of
adding an OPDS schema or connector seam. First create a new 10,000-publication
fixture from the matching core checkout, then pass both immutable outputs to
OPDS:

```bash
../h2hdb/.venv/bin/python \
  ../h2hdb/benchmarks/sqlite_catalog_scalability.py \
  --profile 10k \
  --database /private/tmp/h2hdb-catalog-10k.sqlite3 \
  --receipt /private/tmp/h2hdb-catalog-10k-core.json

.venv/bin/python -m benchmarks.opds_sqlite_scalability \
  --database /private/tmp/h2hdb-catalog-10k.sqlite3 \
  --fixture-receipt /private/tmp/h2hdb-catalog-10k-core.json \
  --output-receipt /private/tmp/h2hdb-catalog-10k-opds.json \
  --warm-repetitions 5 --compact
```

The OPDS tool verifies the receipt size and duplicate-free JSON, its supported
format/schema version, path-free fixture-contract digest, source/schema manifest
digests, epoch/state, expected cardinalities, and exact SQLite SHA-256/size. A
v1 core receipt may omit `receipt_schema_version`; an explicit value must be
`1`, and unknown versions fail closed. Both the original v1 fixture-contract
shape and the explicit-version v1 contract shape are recognized and reported.
It then creates an empty temporary library and coordination lock and starts the
ordinary application without an injected reader. The lifespan therefore calls
public `h2hdb.open_database` with a read-only database config and performs the
full READY audit.

`timing_pass_startup_and_full_ready_audit_ns` and the independent memory-pass
startup audit are setup measurements; neither is mixed into request samples.
The fixed suite performs full in-process HTTP requests and serialization for
discovery first/cursor pages, nonblank search first/cursor pages, and all three
search-scoped facet endpoints. It checks receipt-oracle GID order and facet
counts, revision-pinned links, exact response bodies and deterministic headers.
Every operation records first/warm latency, body size/SHA-256 and an independent
Python allocation peak. The canonical result digest contains no absolute input
or temporary path. Finally the tool rehashes both inputs and refuses a run in
which the database or core receipt changed. No CBZ, artwork, acquisition, or
media payload path is requested.

## License

GNU General Public License v3.0. See [LICENSE](LICENSE). Vendored schema notices
and upstream terms are recorded under `verification/opds`.
