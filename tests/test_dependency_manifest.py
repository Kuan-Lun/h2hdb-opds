from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import runpy
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
dependency_manifest = runpy.run_path(
    str(_ROOT / "scripts/_shared/h2h_dev_tools/dependency_manifest.py")
)["dependency_manifest"]

_DOCUMENT: dict[str, Any] = {
    "project": {
        "dependencies": ["runtime>=1"],
        "optional-dependencies": {"images": ["image>=2"], "dev": ["test>=3"]},
        "version": "0.1.0",
    },
    "build-system": {"requires": ["builder>=4"]},
    "dependency-groups": {"型別": ["types>=5"]},
}
_PACKAGE = {"devDependencies": {"formatter": ">=6"}}
# Captured from the original audit writer before extracting the shared contract.
_HISTORICAL_SHA256 = "350511ceafdec4bb8aa60ad3e86a4b6be741dc9209597bbd3f97a03dd8b1a136"


def test_manifest_preserves_historical_receipt_bytes() -> None:
    payload = dependency_manifest(_DOCUMENT, _PACKAGE)
    assert hashlib.sha256(payload).hexdigest() == _HISTORICAL_SHA256
    reordered = dict(reversed(list(_DOCUMENT.items())))
    assert dependency_manifest(reordered, _PACKAGE) == payload
    unrelated = copy.deepcopy(_DOCUMENT)
    unrelated["project"].update(version="0.2.0", description="unrelated metadata")
    assert dependency_manifest(unrelated, _PACKAGE) == payload


@pytest.mark.parametrize(
    "changed_section",
    [None, "build", "runtime", "optional", "development", "groups", "node"],
)
def test_audit_verifier_binds_every_existing_dependency_section(
    monkeypatch: pytest.MonkeyPatch, changed_section: str | None
) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "check-version.py"
    monkeypatch.syspath_prepend(str(script.parent))
    specification = importlib.util.spec_from_file_location("manifest_version", script)
    assert specification is not None and specification.loader is not None
    policy = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(policy)
    document = copy.deepcopy(_DOCUMENT)
    package = copy.deepcopy(_PACKAGE)
    match changed_section:
        case "build":
            document["build-system"]["requires"].append("other>=1")
        case "runtime":
            document["project"]["dependencies"].append("other>=1")
        case "optional":
            document["project"]["optional-dependencies"]["images"].append("other>=1")
        case "development":
            document["project"]["optional-dependencies"]["dev"].append("other>=1")
        case "groups":
            document["dependency-groups"]["型別"].append("other>=1")
        case "node":
            package["devDependencies"]["formatter"] = ">=7"
    receipt = {
        "schema": "h2h.dependency-audit.v1",
        "project_version": "0.1.0",
        "manifest_sha256": _HISTORICAL_SHA256,
        "review": {"status": "reviewed", "note": "historical fixture"},
    }

    def tree_file(tree: str, path: str) -> bytes:
        assert tree == "candidate"
        value = {
            ".release/dependency-audit.json": receipt,
            "package.json": package,
        }[path]
        return json.dumps(value).encode()

    monkeypatch.setattr(policy, "_tree_file", tree_file)
    if changed_section is None:
        policy._validate_audit("candidate", document, "0.1.0")
    else:
        with pytest.raises(ValueError, match="does not match dependencies"):
            policy._validate_audit("candidate", document, "0.1.0")
