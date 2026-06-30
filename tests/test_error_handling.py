import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.media.album import Album
from streamrip.media.playlist import Playlist


class TestErrorHandling:
    """Test error handling in playlist and album downloads."""

    @pytest.mark.asyncio
    async def test_playlist_handles_failed_track(self):
        """A failure resolving one playlist track must not abort the playlist.

        The remaining (successfully resolved) tracks should still be passed on
        to the album-download and m3u-writing phases.
        """
        mock_config = MagicMock()
        mock_client = MagicMock()

        good_info = MagicMock()
        mock_track_success = MagicMock()
        mock_track_success.resolve = AsyncMock(return_value=good_info)

        mock_track_failure = MagicMock()
        mock_track_failure.resolve = AsyncMock(
            side_effect=json.JSONDecodeError("Expecting value", "", 0)
        )

        playlist = Playlist(
            name="Test Playlist",
            config=mock_config,
            client=mock_client,
            tracks=[mock_track_success, mock_track_failure],
            db=MagicMock(),
        )

        with patch.object(
            Playlist, "_download_albums", AsyncMock(return_value={})
        ) as mock_download_albums, patch.object(
            Playlist, "_locate_album_files", return_value={}
        ) as mock_locate, patch.object(
            Playlist, "_download_singles", AsyncMock(return_value={})
        ) as mock_download_singles, patch.object(
            Playlist, "_write_m3u"
        ) as mock_write_m3u:
            await playlist.download()

        mock_track_success.resolve.assert_called_once()
        mock_track_failure.resolve.assert_called_once()
        # The surviving track's info is forwarded despite the other failing.
        mock_download_albums.assert_called_once_with([good_info])
        mock_locate.assert_called_once_with([good_info], {})
        # Nothing located in an album -> the survivor falls back to a single.
        mock_download_singles.assert_called_once_with([good_info])
        mock_write_m3u.assert_called_once()

    @pytest.mark.asyncio
    async def test_album_handles_failed_track(self):
        """Test that an album download continues even if one track fails."""
        mock_config = MagicMock()
        mock_db = MagicMock()
        mock_meta = MagicMock()

        # Create a list of mock tracks - one will succeed, one will fail
        mock_track_success = MagicMock()
        mock_track_success.resolve = AsyncMock(return_value=MagicMock())
        mock_track_success.resolve.return_value.rip = AsyncMock()

        # This track will raise a JSONDecodeError when resolved
        mock_track_failure = MagicMock()
        mock_track_failure.resolve = AsyncMock(
            side_effect=json.JSONDecodeError("Expecting value", "", 0)
        )

        album = Album(
            meta=mock_meta,
            config=mock_config,
            tracks=[mock_track_success, mock_track_failure],
            folder="/test/folder",
            db=mock_db,
        )

        await album.download()

        mock_track_success.resolve.assert_called_once()
        mock_track_success.resolve.return_value.rip.assert_called_once()
        mock_track_failure.resolve.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_rip_handles_failed_media(self):
        """Test that the Main.rip method handles failed media items."""
        from streamrip.rip.main import Main

        mock_config = MagicMock()

        mock_config.session.downloads.requests_per_minute = 0
        mock_config.session.database.downloads_enabled = False
        mock_config.session.database.failed_downloads_enabled = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(mock_config)

            mock_media_success = MagicMock()
            mock_media_success.rip = AsyncMock()

            mock_media_failure = MagicMock()
            mock_media_failure.rip = AsyncMock(
                side_effect=Exception("Media download failed")
            )

            main.media = [mock_media_success, mock_media_failure]

            await main.rip()

            mock_media_success.rip.assert_called_once()
            mock_media_failure.rip.assert_called_once()
