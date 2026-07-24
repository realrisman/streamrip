import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.media.album import PendingAlbum
from streamrip.metadata.util import get_album_track_ids


@pytest.fixture
def qobuz_album_response():
    with open("tests/qobuz_album_resp.json") as file:
        response = json.load(file)
    response["tracks"] = {
        "items": [{"id": "track-1"}, {"id": "track-2"}, {"id": "track-3"}]
    }
    return response


@pytest.mark.asyncio
async def test_resolve_keeps_downloaded_album_without_folder_or_artwork(
    qobuz_album_response,
):
    client = MagicMock(source="qobuz")
    client.get_metadata = AsyncMock(return_value=qobuz_album_response)
    database = MagicMock()
    database.downloaded.return_value = True
    pending = PendingAlbum("album-id", client, MagicMock(), database)

    with (
        patch("streamrip.media.album.album_folder", return_value="/album"),
        patch("streamrip.media.album.os.makedirs") as makedirs,
        patch(
            "streamrip.media.album.download_artwork", new_callable=AsyncMock
        ) as download_artwork,
    ):
        album = await pending.resolve()

    assert album is not None
    assert album.skip_download
    assert album.folder == "/album"
    expected_track_ids = get_album_track_ids("qobuz", qobuz_album_response)
    assert database.downloaded.call_args_list == [
        ((track_id,), {}) for track_id in expected_track_ids
    ]
    makedirs.assert_not_called()
    download_artwork.assert_not_awaited()

    database.reset_mock()
    await album.rip()
    database.downloaded.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_partially_downloaded_album_still_prepares_download(
    qobuz_album_response,
):
    client = MagicMock(source="qobuz")
    client.get_metadata = AsyncMock(return_value=qobuz_album_response)
    database = MagicMock()
    database.downloaded.side_effect = lambda track_id: track_id != "track-2"
    pending = PendingAlbum("album-id", client, MagicMock(), database)

    with (
        patch("streamrip.media.album.album_folder", return_value="/album"),
        patch("streamrip.media.album.os.makedirs") as makedirs,
        patch(
            "streamrip.media.album.download_artwork",
            new_callable=AsyncMock,
            return_value=("/album/cover.jpg", None),
        ) as download_artwork,
    ):
        album = await pending.resolve()

    assert album is not None
    assert not album.skip_download
    makedirs.assert_called_once_with("/album", exist_ok=True)
    download_artwork.assert_awaited_once()
