import json
from datetime import UTC, datetime

import pytest
from bazaar_market.sources.snapshot import Snapshot, SnapshotConflict

FIXED_NOW = datetime(2026, 10, 6, 18, 0, 0, tzinfo=UTC)


def make(tmp_path):
    return Snapshot(tmp_path, source="edgar", version="2026-10-06", clock=lambda: FIXED_NOW)


def test_write_stores_bytes_under_source_and_version(tmp_path):
    make(tmp_path).write("a/b.json", b"hello\n", url="https://example.test/b", rows=1)

    assert (tmp_path / "edgar" / "2026-10-06" / "a" / "b.json").read_bytes() == b"hello\n"


def test_manifest_records_provenance_for_each_file(tmp_path):
    make(tmp_path).write("b.json", b"hello\n", url="https://example.test/b", rows=7)

    manifest = json.loads((tmp_path / "edgar" / "2026-10-06" / "manifest.json").read_text())
    assert manifest["source"] == "edgar"
    assert manifest["version"] == "2026-10-06"
    assert manifest["files"] == [
        {
            "file": "b.json",
            "url": "https://example.test/b",
            "sha256": "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
            "bytes": 6,
            "rows": 7,
            "fetched_at": "2026-10-06T18:00:00+00:00",
        }
    ]


def test_rewriting_identical_content_is_a_no_op(tmp_path):
    snap = make(tmp_path)
    snap.write("b.json", b"hello\n", url="u", rows=1)
    snap.write("b.json", b"hello\n", url="u", rows=1)

    manifest = json.loads((tmp_path / "edgar" / "2026-10-06" / "manifest.json").read_text())
    assert len(manifest["files"]) == 1


def test_changed_content_cannot_replace_a_frozen_file(tmp_path):
    snap = make(tmp_path)
    snap.write("b.json", b"hello\n", url="u", rows=1)

    with pytest.raises(SnapshotConflict):
        snap.write("b.json", b"revised\n", url="u", rows=1)

    assert (tmp_path / "edgar" / "2026-10-06" / "b.json").read_bytes() == b"hello\n"


def test_a_second_writer_extends_the_existing_manifest(tmp_path):
    make(tmp_path).write("one.json", b"1", url="u1", rows=1)
    make(tmp_path).write("two.json", b"2", url="u2", rows=1)

    manifest = json.loads((tmp_path / "edgar" / "2026-10-06" / "manifest.json").read_text())
    assert [f["file"] for f in manifest["files"]] == ["one.json", "two.json"]
