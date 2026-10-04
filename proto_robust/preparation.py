"""Verifiable shared-preparation snapshots for paired replay.

A paired group freezes its shared on-disk preparation once into a snapshot
directory.  Before every side of every pair, the snapshot is restored into
the runtime shared directory and then *verified by content hash*: every
restored file is re-read from disk and its SHA-256 compared against the
frozen manifest.  A directory merely existing is never treated as proof
that the preparation was restored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shutil

MANIFEST_NAME = "manifest.json"


def hash_tree(root: str | Path) -> dict[str, str]:
    """Return {relative POSIX path: sha256 hex} for every file under root."""
    base = Path(root)
    hashes: dict[str, str] = {}
    if not base.is_dir():
        return hashes
    for path in sorted(base.rglob("*")):
        if path.is_file():
            hashes[path.relative_to(base).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def tree_digest(hashes: dict[str, str]) -> str:
    """One digest summarizing the whole manifest, for compact evidence."""
    digest = hashlib.sha256()
    for name in sorted(hashes):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashes[name].encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def freeze_preparation(source_dir: str | Path, snapshot_dir: str | Path) -> dict:
    """Freeze the shared preparation of one paired group into a snapshot."""
    source = Path(source_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"preparation template missing: {source}")
    snapshot = Path(snapshot_dir)
    if snapshot.exists():
        shutil.rmtree(snapshot)
    files_dir = snapshot / "files"
    shutil.copytree(source, files_dir)
    hashes = hash_tree(files_dir)
    manifest = {
        "source": str(source),
        "files": hashes,
        "tree_sha256": tree_digest(hashes),
    }
    (snapshot / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def load_manifest(snapshot_dir: str | Path) -> dict:
    return json.loads((Path(snapshot_dir) / MANIFEST_NAME).read_text(encoding="utf-8"))


@dataclass
class RestoreReport:
    """Verifiable evidence of one preparation restore attempt."""

    verified: bool = False
    checked_files: int = 0
    tree_sha256: str = ""
    mismatches: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    failure_location: str = ""

    def to_dict(self) -> dict:
        return {
            "verified": self.verified,
            "checked_files": self.checked_files,
            "tree_sha256": self.tree_sha256,
            "mismatches": list(self.mismatches),
            "missing": list(self.missing),
            "extra": list(self.extra),
            "failure_location": self.failure_location,
        }


def verify_preparation(manifest: dict, target_dir: str | Path) -> RestoreReport:
    """Re-hash every file under target_dir and compare with the manifest."""
    expected = dict(manifest.get("files", {}))
    actual = hash_tree(target_dir)
    report = RestoreReport(
        checked_files=len(actual),
        tree_sha256=tree_digest(actual),
    )
    for name, digest in sorted(expected.items()):
        if name not in actual:
            report.missing.append(name)
        elif actual[name] != digest:
            report.mismatches.append(
                {"path": name, "expected": digest, "actual": actual[name]}
            )
    for name in sorted(actual):
        if name not in expected:
            report.extra.append(name)
    report.verified = not (report.mismatches or report.missing or report.extra)
    if not report.verified:
        first = (
            report.mismatches[0]["path"]
            if report.mismatches
            else (report.missing[0] if report.missing else report.extra[0])
        )
        report.failure_location = first
    return report


def restore_preparation(snapshot_dir: str | Path, target_dir: str | Path) -> RestoreReport:
    """Restore a frozen snapshot into target_dir and verify by content hash.

    The target directory is fully rebuilt from the snapshot, then every
    restored file is re-read and hashed.  The returned report is the
    verifiable evidence that the preparation was (or was not) restored.
    """
    snapshot = Path(snapshot_dir)
    manifest = load_manifest(snapshot)
    target = Path(target_dir)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(snapshot / "files", target)
    return verify_preparation(manifest, target)
