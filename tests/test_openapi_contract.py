import json
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from h2hdb_opds import OPDSConfig, create_app

_HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)
_OPDS12_OPERATIONS = (
    ("/catalog", "catalog", "opds_v1_2_catalog"),
    ("/publications", "publications", "opds_v1_2_publications"),
    ("/search", "search", "opds_v1_2_search"),
    ("/facets/{facet}", "facet_values", "opds_v1_2_facets__facet_"),
    ("/opensearch.xml", "opensearch", "opds_v1_2_opensearch_xml"),
    ("/recent/uploaded", "recent_uploaded", "opds_v1_2_recent_uploaded"),
    ("/recent/downloaded", "recent_downloaded", "opds_v1_2_recent_downloaded"),
    (
        "/publications/{publication_id}",
        "publication",
        "opds_v1_2_publications__publication_id_",
    ),
)
_OPENAPI_OPERATION_IDS_SCRIPT = """
import json
import sys
import warnings
from pathlib import Path

from h2hdb_opds import OPDSConfig, create_app

root = Path(sys.argv[1])
app = create_app(OPDSConfig(
    library_root=root / "current",
    coordination_root=root / "coordination",
    public_base_url="http://catalog.example",
))
with warnings.catch_warnings():
    warnings.filterwarnings("error", message="Duplicate Operation ID", category=UserWarning)
    schema = app.openapi()
print(json.dumps({
    f"{method.upper()} {path}": operation["operationId"]
    for path, path_item in schema["paths"].items()
    for method, operation in path_item.items()
    if method in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
}, sort_keys=True))
"""


def test_openapi_operation_ids_are_nonempty_and_globally_unique(
    opds_config: OPDSConfig,
) -> None:
    app = create_app(opds_config)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "error", message="Duplicate Operation ID", category=UserWarning
        )
        schema = app.openapi()
    operation_ids = [
        operation["operationId"]
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method in _HTTP_METHODS
    ]
    assert operation_ids
    assert all(isinstance(value, str) and value.strip() for value in operation_ids)
    assert len(set(operation_ids)) == len(operation_ids)


@pytest.mark.parametrize(("path", "name", "path_id"), _OPDS12_OPERATIONS)
def test_opds12_get_and_head_have_distinct_stable_operation_ids(
    opds_config: OPDSConfig,
    path: str,
    name: str,
    path_id: str,
) -> None:
    app = create_app(opds_config)
    schema = app.openapi()
    operations = schema["paths"][f"/opds/v1.2{path}"]
    assert set(operations) == {"get", "head"}
    assert operations["get"]["operationId"] == f"opds12_{name}_{path_id}_get"
    assert operations["head"]["operationId"] == f"opds12_head_{name}_{path_id}_head"


def test_openapi_operation_ids_are_independent_of_python_hash_seed(
    tmp_path: Path,
) -> None:
    schemas = []
    # These seeds exposed opposite GET/HEAD set orders before routes were split.
    for seed in ("1", "3"):
        result = subprocess.run(
            [sys.executable, "-c", _OPENAPI_OPERATION_IDS_SCRIPT, str(tmp_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        schemas.append(json.loads(result.stdout))
    assert schemas[0]
    assert schemas[0] == schemas[1]
