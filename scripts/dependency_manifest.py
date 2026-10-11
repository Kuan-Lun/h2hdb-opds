"""Canonical dependency input shared by audit production and verification."""

from __future__ import annotations

import json
from typing import Any


def dependency_manifest(document: dict[str, Any], package: dict[str, Any]) -> bytes:
    """Return the canonical bytes bound by dependency-audit v1 receipts."""
    project = document.get("project", {})
    manifest = {
        "build": document.get("build-system", {}).get("requires", []),
        "dependency-groups": document.get("dependency-groups", {}),
        "optional": project.get("optional-dependencies", {}),
        "runtime": project.get("dependencies", []),
        "node-dev": package.get("devDependencies", {}),
    }
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
