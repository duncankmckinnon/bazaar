import json
from datetime import UTC, datetime

import pytest
from bazaar_market.sources.snapshot import Snapshot, SnapshotConflict, UnsafePath

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


@pytest.mark.parametrize("name", ["../escape.json", "a/../../escape.json", "/abs/escape.json"])
def test_a_name_that_leaves_the_version_directory_is_refused(tmp_path, name):
    snap = Snapshot(tmp_path / "raw", source="edgar", version="v1", clock=lambda: FIXED_NOW)

    with pytest.raises(UnsafePath):
        snap.write(name, b"x", url="u", rows=1)

    assert not list(tmp_path.rglob("escape.json"))


def test_a_file_left_without_a_manifest_entry_is_recorded_on_the_next_write(tmp_path):
    snap = make(tmp_path)
    snap.path("b.json").parent.mkdir(parents=True)
    snap.path("b.json").write_bytes(b"hello\n")  # a run that died before recording it

    snap.write("b.json", b"hello\n", url="https://example.test/b", rows=1)

    assert snap.entry("b.json")["url"] == "https://example.test/b"


def test_an_unrecorded_partial_file_is_replaced_not_treated_as_frozen(tmp_path):
    snap = make(tmp_path)
    snap.path("b.json").parent.mkdir(parents=True)
    snap.path("b.json").write_bytes(b"hel")

    snap.write("b.json", b"hello\n", url="u", rows=1)

    assert snap.path("b.json").read_bytes() == b"hello\n"


def test_a_write_leaves_no_temporary_files_behind(tmp_path):
    snap = make(tmp_path)
    snap.write("a/b.json", b"hello\n", url="u", rows=1)

    assert sorted(p.name for p in snap.dir.rglob("*") if p.is_file()) == ["b.json", "manifest.json"]


def test_entry_is_none_for_a_file_that_was_never_recorded(tmp_path):
    assert make(tmp_path).entry("missing.json") is None
