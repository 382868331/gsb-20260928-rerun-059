"""Verifiable frozen preparation snapshots for paired replay groups.

A paired replay group shares one frozen preparation snapshot: the exact
shared-file state every pair in the group must start from.  Restoring a
snapshot is only accepted when the restored tree is *verified* against the
manifest -- every file's sha256 and size must match, and no unexpected extra
files may remain.  A directory merely existing is never treated as proof
that preparation was restored.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from proto_robust.session_campaign import _atomic_json

MANIFEST_NAME = "snapshot.manifest.json"
UNREMOVABLE_MARKER = "gsb-unremovable"


class PreparationRestoreError(Exception):
    """The frozen preparation state could not be restored or verified."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_files(directory: Path) -> list[Path]:
    return sorted(
        (path for path in directory.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(directory).as_posix(),
    )


def freeze(source_dir: str | Path, snapshot_dir: str | Path) -> dict:
    """Freeze the current state of ``source_dir`` into ``snapshot_dir``.

    Returns the manifest: one entry per file with its relative path, size
    and sha256, plus a manifest digest so a whole frozen group snapshot can
    be re-checked later.  Freezing an existing snapshot dir is refused: a
    frozen group snapshot is immutable evidence.
    """
    source = Path(source_dir)
    snapshot = Path(snapshot_dir)
    if snapshot.exists():
        raise PreparationRestoreError(f"snapshot already frozen: {snapshot}")
    snapshot.mkdir(parents=True)
    entries = []
    for path in _relative_files(source):
        relative = path.relative_to(source).as_posix()
        data = path.read_bytes()
        target = snapshot / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries.append({
            "path": relative,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    manifest = {
        "source": str(source),
        "files": entries,
        "manifest_sha256": _manifest_digest(entries),
    }
    _atomic_json(snapshot / MANIFEST_NAME, manifest)
    return manifest


def load_manifest(snapshot_dir: str | Path) -> dict:
    path = Path(snapshot_dir) / MANIFEST_NAME
    if not path.exists():
        raise PreparationRestoreError(f"no frozen snapshot manifest at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify(snapshot_dir: str | Path, target_dir: str | Path) -> dict:
    """Verify ``target_dir`` against the frozen manifest, content by content.

    The returned evidence lists every checked file with its expected and
    observed sha256; ``ok`` is true only when every manifest file matches
    byte-for-byte and no extra files are present.
    """
    snapshot = Path(snapshot_dir)
    target = Path(target_dir)
    manifest = load_manifest(snapshot)
    checked = []
    mismatched = []
    missing = []
    for entry in manifest["files"]:
        candidate = target / entry["path"]
        record = {"path": entry["path"], "expected_sha256": entry["sha256"]}
        if not candidate.is_file():
            record["observed_sha256"] = None
            missing.append(entry["path"])
        else:
            observed = _sha256(candidate)
            record["observed_sha256"] = observed
            if observed != entry["sha256"] or candidate.stat().st_size != entry["size"]:
                mismatched.append(entry["path"])
        checked.append(record)
    expected = {entry["path"] for entry in manifest["files"]}
    extra = []
    if target.is_dir():
        extra = [
            path.relative_to(target).as_posix()
            for path in _relative_files(target)
            if path.relative_to(target).as_posix() not in expected
        ]
    ok = not mismatched and not missing and not extra
    return {
        "ok": ok,
        "manifest_sha256": manifest["manifest_sha256"],
        "checked": checked,
        "mismatched": mismatched,
        "missing": missing,
        "extra": extra,
    }


def restore(snapshot_dir: str | Path, target_dir: str | Path) -> dict:
    """Restore the frozen snapshot into ``target_dir`` and verify the result.

    Raises :class:`PreparationRestoreError` when an artifact refuses removal
    (simulated external lock) or when post-restore verification fails, so a
    pair never silently starts from an unverified state.
    """
    snapshot = Path(snapshot_dir)
    target = Path(target_dir)
    manifest = load_manifest(snapshot)
    if target.exists():
        for path in _relative_files(target):
            with path.open("rb") as handle:
                head = handle.read(len(UNREMOVABLE_MARKER))
            if head.decode("utf-8", "replace") == UNREMOVABLE_MARKER:
                raise PreparationRestoreError(
                    f"unremovable artifact blocks restore: {path.name}"
                )
        for path in reversed(_relative_files(target)):
            path.unlink()
    for entry in manifest["files"]:
        source = snapshot / "files" / entry["path"]
        destination = target / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    evidence = verify(snapshot, target)
    if not evidence["ok"]:
        raise PreparationRestoreError(
            "post-restore verification failed: "
            f"mismatched={evidence['mismatched']} missing={evidence['missing']} "
            f"extra={evidence['extra']}"
        )
    return evidence


def _manifest_digest(entries: list[dict]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()
