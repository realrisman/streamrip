import os
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from util import arun

import streamrip.db as db
from streamrip.client.downloadable import Downloadable
from streamrip.client.qobuz import QobuzClient
from streamrip.exceptions import IncompleteDownloadError
from streamrip.media.track import MAX_DOWNLOAD_RETRIES, PendingSingle, Track


@pytest.mark.skipif(
    "QOBUZ_EMAIL" not in os.environ, reason="Qobuz credentials not found in env."
)
def test_pending_resolve(qobuz_client: QobuzClient):
    qobuz_client.config.session.downloads.folder = "./tests"
    p = PendingSingle(
        "19512574",
        qobuz_client,
        qobuz_client.config,
        db.Database(db.Dummy(), db.Dummy()),
    )
    t = arun(p.resolve())
    dir = "tests/tests/Fleetwood Mac - Rumours (1977) [FLAC] [24B-96kHz]"
    assert os.path.isdir(dir)
    assert os.path.isfile(os.path.join(dir, "cover.jpg"))
    assert os.path.isfile(t.cover_path)
    assert isinstance(t, Track)
    assert isinstance(t.downloadable, Downloadable)
    assert t.cover_path is not None
    shutil.rmtree(dir)


def _make_track(
    download_path: str,
    download_side_effect=None,
    *,
    downloadable=None,
    client=None,
    quality: int = 3,
    fallback: bool = False,
) -> Track:
    """Build a Track with mocked dependencies for download/retry tests."""
    meta = MagicMock()
    meta.title = "The Long And Winding Road (2021 Mix)"
    meta.tracknumber = 1
    meta.info.id = "12345"

    if downloadable is None:
        downloadable = MagicMock()
        downloadable.source = "tidal"
        downloadable.size = AsyncMock(return_value=100)
        downloadable.download = AsyncMock(side_effect=download_side_effect)
        downloadable.extension = "flac"

    config = MagicMock()
    # Disable progress bars (no-op callback) and the global semaphore (unlimited).
    config.session.cli.progress_bars = False
    config.session.downloads.concurrency = True
    config.session.downloads.max_connections = -1
    config.session.downloads.fallback_to_lower_quality = fallback

    return Track(
        meta=meta,
        downloadable=downloadable,
        config=config,
        folder="",
        cover_path=None,
        db=MagicMock(),
        client=client or MagicMock(),
        quality=quality,
        download_path=download_path,
    )


def test_download_persistent_failure_skips_and_cleans_up(tmp_path):
    """A download that always fails is retried, marked failed, and the partial
    file is removed so postprocess never tags a corrupt file."""
    partial = os.path.join(tmp_path, "track.flac")
    with open(partial, "wb") as f:
        f.write(b"\x00\x00\x00\x00")  # truncated partial file left on disk

    err = IncompleteDownloadError("Expected 100 bytes, only received 4")
    track = _make_track(partial, download_side_effect=err)

    with patch("streamrip.media.track.asyncio.sleep", new=AsyncMock()):
        arun(track.download())

    # One attempt per retry slot.
    assert track.downloadable.download.await_count == MAX_DOWNLOAD_RETRIES
    assert track.failed is True
    track.db.set_failed.assert_called_once_with("tidal", "track", "12345")
    # Partial/truncated file is gone.
    assert not os.path.exists(partial)

    # postprocess must not attempt to tag the (now absent) file.
    with patch("streamrip.media.track.tag_file", new=AsyncMock()) as tag_mock:
        arun(track.postprocess())
    tag_mock.assert_not_called()


def test_download_recovers_after_transient_error(tmp_path):
    """A single transient failure is retried and then succeeds; the track is not
    marked failed and the file survives for tagging."""
    final = os.path.join(tmp_path, "track.flac")

    calls = {"n": 0}

    async def flaky(path, callback):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IncompleteDownloadError("transient")
        # Success: write the complete file.
        with open(path, "wb") as f:
            f.write(b"\x00" * 100)

    track = _make_track(final, download_side_effect=flaky)

    with patch("streamrip.media.track.asyncio.sleep", new=AsyncMock()):
        arun(track.download())

    assert track.downloadable.download.await_count == 2
    assert track.failed is False
    track.db.set_failed.assert_not_called()
    assert os.path.exists(final)


def test_download_falls_back_to_lower_quality(tmp_path):
    """When a download persistently fails at the requested quality and fallback
    is enabled, the track is re-fetched at the next lower quality and downloaded
    successfully."""
    final = os.path.join(tmp_path, "track.flac")

    # Quality 3 (hi-res) always fails — e.g. the CDN won't serve the master.
    hires = MagicMock()
    hires.source = "qobuz"
    hires.size = AsyncMock(return_value=100)
    hires.download = AsyncMock(side_effect=IncompleteDownloadError("hi-res broken"))
    hires.extension = "flac"

    # Quality 2 (CD) succeeds.
    async def good_download(path, callback):
        with open(path, "wb") as f:
            f.write(b"\x00" * 50)

    lowq = MagicMock()
    lowq.source = "qobuz"
    lowq.size = AsyncMock(return_value=50)
    lowq.download = AsyncMock(side_effect=good_download)
    lowq.extension = "flac"

    client = MagicMock()
    client.get_downloadable = AsyncMock(return_value=lowq)

    track = _make_track(
        final, downloadable=hires, client=client, quality=3, fallback=True
    )

    # _set_download_path needs real metadata; keep the tmp path stable instead.
    with patch("streamrip.media.track.asyncio.sleep", new=AsyncMock()), patch.object(
        Track, "_set_download_path", lambda self: None
    ):
        arun(track.download())

    assert hires.download.await_count == MAX_DOWNLOAD_RETRIES
    client.get_downloadable.assert_awaited_once_with("12345", 2)
    assert lowq.download.await_count == 1
    assert track.failed is False
    assert os.path.exists(final)


def test_fast_async_download_raises_on_truncation(tmp_path):
    """fast_async_download detects a connection that closes mid-stream (bytes
    written < Content-Length) and raises IncompleteDownloadError."""
    from streamrip.client import downloadable as dl

    out = os.path.join(tmp_path, "out.bin")

    class FakeResp:
        headers = {"Content-Length": "100"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield b"\x00" * 10  # only 10 of the promised 100 bytes

    with patch.object(dl.requests, "get", return_value=FakeResp()):
        with pytest.raises(IncompleteDownloadError):
            arun(dl.fast_async_download(out, "http://x", {}, lambda _: None))
