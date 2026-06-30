import os
import sqlite3

from streamrip.db import Database, Downloads, Dummy


def test_downloads_roundtrips_path(tmp_path):
    """A fresh downloads DB records and returns a track's on-disk path."""
    db = Downloads(os.path.join(str(tmp_path), "downloads.db"))
    db.add(("track1",))
    db.set_path("track1", "/music/Album/01 - Song.flac")

    assert db.contains(id="track1")
    assert db.get_path("track1") == "/music/Album/01 - Song.flac"
    # No recorded path for an unknown id.
    assert db.get_path("nope") is None


def test_downloads_set_path_upserts(tmp_path):
    """Re-recording a path for the same id overwrites rather than duplicating."""
    db = Downloads(os.path.join(str(tmp_path), "downloads.db"))
    db.set_path("t", "/old/path.flac")
    db.set_path("t", "/new/path.mp3")
    assert db.get_path("t") == "/new/path.mp3"


def test_downloads_migrates_preexisting_db(tmp_path):
    """A downloads DB created before path-persistence (no download_paths table)
    gains the companion table on open and can record paths; pre-existing
    download rows simply have no recorded path until re-recorded."""
    path = os.path.join(str(tmp_path), "downloads.db")
    # Simulate a legacy DB: only the `downloads` table, one downloaded id.
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE downloads (id TEXT UNIQUE NOT NULL)")
        conn.execute("INSERT INTO downloads (id) VALUES ('legacy')")

    db = Downloads(path)  # must not raise; creates download_paths idempotently
    assert db.contains(id="legacy")
    assert db.get_path("legacy") is None  # no path recorded by the old version

    db.set_path("legacy", "/music/Legacy/01.flac")
    assert db.get_path("legacy") == "/music/Legacy/01.flac"
    # The original downloads table and its row shape are untouched.
    assert db.all() == [("legacy",)]


def test_database_wrapper_set_downloaded_with_path(tmp_path):
    downloads = Downloads(os.path.join(str(tmp_path), "downloads.db"))
    failed = Downloads(os.path.join(str(tmp_path), "failed.db"))
    database = Database(downloads, failed)

    database.set_downloaded("abc", "/music/A/01.flac")
    assert database.downloaded("abc")
    assert database.path_for("abc") == "/music/A/01.flac"

    # Backward-compatible: path is optional; only the id is recorded.
    database.set_downloaded("noPath")
    assert database.downloaded("noPath")
    assert database.path_for("noPath") is None


def test_dummy_path_is_noop():
    dummy = Dummy()
    dummy.set_path("x", "/y")  # no-op, must not raise
    assert dummy.get_path("x") is None
