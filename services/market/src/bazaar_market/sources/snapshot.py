from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


class SnapshotConflict(Exception):
    """A fetch produced different bytes for a file that is already frozen in this version."""


class Snapshot:
    """One frozen version of one source: `<root>/<source>/<version>/` plus a `manifest.json`."""

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
        return self.dir / name

    def write(self, name: str, content: bytes, *, url: str, rows: int) -> Path:
        target = self.path(name)
        if target.exists():
            if target.read_bytes() == content:
                return target
            raise SnapshotConflict(
                f"{target} is frozen and the new content differs. Fetch into a new version."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self._record(
            {
                "file": name,
                "url": url,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "rows": rows,
                "fetched_at": self._clock().isoformat(),
            }
        )
        return target

    def _record(self, entry: dict[str, object]) -> None:
        manifest_path = self.dir / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
        else:
            manifest = {"source": self.source, "version": self.version, "files": []}
        manifest["files"].append(entry)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
