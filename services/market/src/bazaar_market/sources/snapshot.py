from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


class SnapshotConflict(Exception):
    """A fetch produced different bytes for a file that is already frozen in this version."""


class UnsafePath(ValueError):
    """A file name, possibly taken from remote data, resolves outside the version directory."""


class Snapshot:
    """One frozen version of one source: `<root>/<source>/<version>/` plus a `manifest.json`.

    A file is frozen once the manifest lists it. One process writes a version at a time.
    """

    def __init__(
        self,
        root: Path,
        *,
        source: str,
        version: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.source = source
        self.version = version
        self.dir = Path(root) / source / version
        self._clock = clock

    def path(self, name: str) -> Path:
        target = (self.dir / name).resolve()
        if not target.is_relative_to(self.dir.resolve()):
            raise UnsafePath(f"{name!r} is outside {self.dir}")
        return target

    def _key(self, name: str) -> str:
        """The manifest name for `name`, so two spellings of one file share one entry."""
        return self.path(name).relative_to(self.dir.resolve()).as_posix()

    def entry(self, name: str) -> dict | None:
        """The manifest entry for `name`, or None when the file was never recorded."""
        key = self._key(name)
        return next((e for e in self._manifest()["files"] if e["file"] == key), None)

    def write(self, name: str, content: bytes, *, url: str, rows: int) -> Path:
        target = self.path(name)
        if self.entry(name) is not None:
            if not target.exists():
                raise SnapshotConflict(f"{target} is listed in the manifest but missing on disk.")
            if target.read_bytes() == content:
                return target
            raise SnapshotConflict(
                f"{target} is frozen and the new content differs. Fetch into a new version."
            )
        # Not in the manifest: either new, or left by a run that died before recording it.
        target.parent.mkdir(parents=True, exist_ok=True)
        self._replace(target, content)
        manifest = self._manifest()
        manifest["files"].append(
            {
                "file": self._key(name),
                "url": url,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "rows": rows,
                "fetched_at": self._clock().isoformat(),
            }
        )
        self._replace(self.dir / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
        return target

    def coverage(self, key: str) -> dict | None:
        """The window recorded for `key` as {"start", "end"}, or None when none was recorded."""
        return self._manifest().get("coverage", {}).get(key)

    def cover(self, key: str, *, start: str, end: str, **details: str) -> None:
        """Record the time window `key` was fetched for, and any request details that shape it.

        A different window or different details is a conflict.
        """
        manifest = self._manifest()
        window = {"start": start, "end": end, **details}
        recorded = manifest.setdefault("coverage", {}).get(key)
        if recorded == window:
            return
        if recorded is not None:
            raise SnapshotConflict(
                f"{self.dir} already holds {key} for {recorded['start']} to {recorded['end']}. "
                "Fetch a different window into a new version."
            )
        manifest["coverage"][key] = window
        self.dir.mkdir(parents=True, exist_ok=True)
        self._replace(self.dir / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())

    def _manifest(self) -> dict:
        manifest_path = self.dir / "manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {"source": self.source, "version": self.version, "files": []}

    @staticmethod
    def _replace(target: Path, content: bytes) -> None:
        """Write through a temporary file so a crash never leaves a half-written target."""
        temporary = target.with_name(target.name + ".part")
        temporary.write_bytes(content)
        os.replace(temporary, target)
