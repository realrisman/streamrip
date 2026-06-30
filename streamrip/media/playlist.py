import asyncio
import glob
import html
import logging
import os
import random
import re
from contextlib import ExitStack
from dataclasses import dataclass

import aiohttp
from rich.text import Text

from .. import progress
from ..client import Client
from ..config import Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..filepath_utils import clean_filepath
from ..metadata import (
    AlbumMetadata,
    PlaylistMetadata,
    SearchResults,
    TrackMetadata,
)
from ..utils.ssl_utils import get_aiohttp_connector_kwargs
from .album import Album, PendingAlbum
from .media import Media, Pending
from .track import format_track_filename

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class PlaylistTrackInfo:
    """Resolved metadata for one playlist entry.

    Records which album backs the track and the metadata needed to locate the
    track's file on disk once the album has been downloaded.
    """

    position: int
    track_id: str
    client: Client
    album_id: str
    album_meta: AlbumMetadata
    track_meta: TrackMetadata


@dataclass(slots=True)
class PendingPlaylistTrack(Pending):
    id: str
    client: Client
    config: Config
    playlist_name: str
    position: int
    db: Database

    async def resolve(self) -> PlaylistTrackInfo | None:
        # NOTE: the database is intentionally *not* consulted here. We still
        # want a playlist (m3u) entry for tracks that were downloaded on a
        # previous run; the per-track skip happens later when the album is
        # downloaded.
        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Could not stream track {self.id}: {e}")
            return None

        album = AlbumMetadata.from_track_resp(resp, self.client.source)
        if album is None:
            logger.error(
                f"Track ({self.id}) not available for stream on {self.client.source}",
            )
            self.db.set_failed(self.client.source, "track", self.id)
            return None
        meta = TrackMetadata.from_resp(album, self.client.source, resp)
        if meta is None:
            logger.error(
                f"Track ({self.id}) not available for stream on {self.client.source}",
            )
            self.db.set_failed(self.client.source, "track", self.id)
            return None

        return PlaylistTrackInfo(
            position=self.position,
            track_id=self.id,
            client=self.client,
            album_id=album.info.id,
            album_meta=album,
            track_meta=meta,
        )


@dataclass(slots=True)
class Playlist(Media):
    name: str
    config: Config
    client: Client
    tracks: list[PendingPlaylistTrack]

    async def preprocess(self):
        progress.add_title(self.name)

    async def postprocess(self):
        progress.remove_title(self.name)

    async def download(self):
        # Phase A: resolve each playlist entry's album + track metadata.
        infos = await self._resolve_track_infos()
        if not infos:
            logger.error(f"No tracks could be resolved for playlist '{self.name}'")
            return

        # Phase B: download each distinct album once (full album).
        resolved_albums = await self._download_albums(infos)

        # Phase C: write an m3u that references the tracks inside their album
        # folders (which live outside the `playlist` folder).
        self._write_m3u(infos, resolved_albums)

    async def _resolve_track_infos(self) -> list[PlaylistTrackInfo]:
        chunk_size = 20
        infos: list[PlaylistTrackInfo] = []
        for batch in self.batch(self.tracks, chunk_size):
            results = await asyncio.gather(
                *[item.resolve() for item in batch],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Error resolving playlist track: {result}")
                elif result is not None:
                    infos.append(result)
        infos.sort(key=lambda i: i.position)
        return infos

    async def _download_albums(
        self,
        infos: list[PlaylistTrackInfo],
    ) -> dict[str, Album]:
        # Dedupe album ids, keeping the first client seen for each album.
        unique: dict[str, Client] = {}
        for info in infos:
            unique.setdefault(info.album_id, info.client)

        resolved: dict[str, Album] = {}
        album_chunk_size = 5
        for batch in self.batch(list(unique.items()), album_chunk_size):
            results = await asyncio.gather(
                *[
                    self._resolve_and_download_album(album_id, client)
                    for album_id, client in batch
                ],
                return_exceptions=True,
            )
            for (album_id, _), result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.error(f"Error downloading album {album_id}: {result}")
                elif result is not None:
                    resolved[album_id] = result
        return resolved

    async def _resolve_and_download_album(
        self,
        album_id: str,
        client: Client,
    ) -> Album | None:
        pending = PendingAlbum(album_id, client, self.config, self.db)
        album = await pending.resolve()
        if album is None:
            return None
        await album.rip()
        return album

    def _write_m3u(
        self,
        infos: list[PlaylistTrackInfo],
        resolved_albums: dict[str, Album],
    ):
        downloads_config = self.config.session.downloads
        playlist_folder = os.path.join(downloads_config.folder, "playlist")
        os.makedirs(playlist_folder, exist_ok=True)
        m3u_path = os.path.join(playlist_folder, clean_filepath(self.name) + ".m3u")

        lines = ["#EXTM3U"]
        for info in infos:
            album = resolved_albums.get(info.album_id)
            if album is None:
                logger.warning(
                    f"Album for track '{info.track_meta.title}' was not downloaded; "
                    "omitting from playlist file",
                )
                continue

            expected_dir = album.folder
            if downloads_config.disc_subdirectories and info.album_meta.disctotal > 1:
                expected_dir = os.path.join(
                    expected_dir,
                    f"Disc {info.track_meta.discnumber}",
                )

            stem = format_track_filename(info.track_meta, self.config)
            pattern = os.path.join(glob.escape(expected_dir), glob.escape(stem) + ".*")
            matches = glob.glob(pattern)
            if not matches:
                logger.warning(
                    f"Could not locate downloaded file for '{info.track_meta.title}'; "
                    "omitting from playlist file",
                )
                continue

            rel_path = os.path.relpath(matches[0], start=playlist_folder)
            lines.append(
                f"#EXTINF:-1,{info.track_meta.artist} - {info.track_meta.title}",
            )
            lines.append(rel_path)

        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info(f"Wrote playlist file to {m3u_path}")
        console.print(f"[green]Wrote playlist file to[/green] {m3u_path}")

    @staticmethod
    def batch(iterable, n=1):
        total = len(iterable)
        for ndx in range(0, total, n):
            yield iterable[ndx : min(ndx + n, total)]


@dataclass(slots=True)
class PendingPlaylist(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Playlist | None:
        try:
            resp = await self.client.get_metadata(self.id, "playlist")
        except NonStreamableError as e:
            logger.error(
                f"Playlist {self.id} not available to stream on {self.client.source} ({e})",
            )
            return None

        try:
            meta = PlaylistMetadata.from_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error creating playlist: {e}")
            return None
        name = meta.name
        tracks = [
            PendingPlaylistTrack(
                id,
                self.client,
                self.config,
                name,
                position + 1,
                self.db,
            )
            for position, id in enumerate(meta.ids())
        ]
        return Playlist(name, self.config, self.client, tracks)


@dataclass(slots=True)
class PendingLastfmPlaylist(Pending):
    lastfm_url: str
    client: Client
    fallback_client: Client | None
    config: Config
    db: Database

    @dataclass(slots=True)
    class Status:
        found: int
        failed: int
        total: int

        def text(self) -> Text:
            return Text.assemble(
                "Searching for last.fm tracks (",
                (f"{self.found} found", "bold green"),
                ", ",
                (f"{self.failed} failed", "bold red"),
                ", ",
                (f"{self.total} total", "bold"),
                ")",
            )

    async def resolve(self) -> Playlist | None:
        try:
            playlist_title, titles_artists = await self._parse_lastfm_playlist(
                self.lastfm_url,
            )
        except Exception as e:
            logger.error("Error occured while parsing last.fm page: %s", e)
            return None

        requests = []

        s = self.Status(0, 0, len(titles_artists))
        if self.config.session.cli.progress_bars:
            with console.status(s.text(), spinner="moon") as status:

                def callback():
                    status.update(s.text())

                for title, artist in titles_artists:
                    requests.append(self._make_query(f"{title} {artist}", s, callback))
                results: list[tuple[str | None, bool]] = await asyncio.gather(*requests)
        else:

            def callback():
                pass

            for title, artist in titles_artists:
                requests.append(self._make_query(f"{title} {artist}", s, callback))
            results: list[tuple[str | None, bool]] = await asyncio.gather(*requests)

        pending_tracks = []
        for pos, (id, from_fallback) in enumerate(results, start=1):
            if id is None:
                logger.warning(f"No results found for {titles_artists[pos-1]}")
                continue

            if from_fallback:
                assert self.fallback_client is not None
                client = self.fallback_client
            else:
                client = self.client

            pending_tracks.append(
                PendingPlaylistTrack(
                    id,
                    client,
                    self.config,
                    playlist_title,
                    pos,
                    self.db,
                ),
            )

        return Playlist(playlist_title, self.config, self.client, pending_tracks)

    async def _make_query(
        self,
        query: str,
        search_status: Status,
        callback,
    ) -> tuple[str | None, bool]:
        """Search for a track with the main source, and use fallback source
        if that fails.

        Args:
        ----
            query (str): Query to search
            s (Status):
            callback: function to call after each query completes

        Returns: A 2-tuple, where the first element contains the ID if it was found,
        and the second element is True if the fallback source was used.
        """
        with ExitStack() as stack:
            # ensure `callback` is always called
            stack.callback(callback)
            pages = await self.client.search("track", query, limit=1)
            if len(pages) > 0:
                logger.debug(f"Found result for {query} on {self.client.source}")
                search_status.found += 1
                return (
                    SearchResults.from_pages(self.client.source, "track", pages)
                    .results[0]
                    .id
                ), False

            if self.fallback_client is None:
                logger.debug(f"No result found for {query} on {self.client.source}")
                search_status.failed += 1
                return None, False

            pages = await self.fallback_client.search("track", query, limit=1)
            if len(pages) > 0:
                logger.debug(f"Found result for {query} on {self.client.source}")
                search_status.found += 1
                return (
                    SearchResults.from_pages(
                        self.fallback_client.source,
                        "track",
                        pages,
                    )
                    .results[0]
                    .id
                ), True

            logger.debug(f"No result found for {query} on {self.client.source}")
            search_status.failed += 1
        return None, True

    async def _parse_lastfm_playlist(
        self,
        playlist_url: str,
    ) -> tuple[str, list[tuple[str, str]]]:
        """From a last.fm url, return the playlist title, and a list of
        track titles and artist names.

        Each page contains 50 results, so `num_tracks // 50 + 1` requests
        are sent per playlist.

        :param url:
        :type url: str
        :rtype: tuple[str, list[tuple[str, str]]]
        """
        logger.debug("Fetching lastfm playlist")

        title_tags = re.compile(r'<a\s+href="[^"]+"\s+title="([^"]+)"')
        re_total_tracks = re.compile(r'data-playlisting-entry-count="(\d+)"')
        re_playlist_title_match = re.compile(
            r'<h1 class="playlisting-playlist-header-title">([^<]+)</h1>',
        )

        def find_title_artist_pairs(page_text):
            info: list[tuple[str, str]] = []
            titles = title_tags.findall(page_text)  # [2:]
            for i in range(0, len(titles) - 1, 2):
                info.append((html.unescape(titles[i]), html.unescape(titles[i + 1])))
            return info

        async def fetch(session: aiohttp.ClientSession, url, **kwargs):
            async with session.get(url, **kwargs) as resp:
                return await resp.text("utf-8")

        # Create new session so we're not bound by rate limit
        verify_ssl = getattr(self.config.session.downloads, "verify_ssl", True)
        connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
        connector = aiohttp.TCPConnector(**connector_kwargs)

        async with aiohttp.ClientSession(connector=connector) as session:
            page = await fetch(session, playlist_url)
            playlist_title_match = re_playlist_title_match.search(page)
            if playlist_title_match is None:
                raise Exception("Error finding title from response")

            playlist_title: str = html.unescape(playlist_title_match.group(1))

            title_artist_pairs: list[tuple[str, str]] = find_title_artist_pairs(page)

            total_tracks_match = re_total_tracks.search(page)
            if total_tracks_match is None:
                raise Exception("Error parsing lastfm page: %s", page)
            total_tracks = int(total_tracks_match.group(1))

            remaining_tracks = total_tracks - 50  # already got 50 from 1st page
            if remaining_tracks <= 0:
                return playlist_title, title_artist_pairs

            last_page = (
                1 + int(remaining_tracks // 50) + int(remaining_tracks % 50 != 0)
            )
            requests = []
            for page in range(2, last_page + 1):
                requests.append(fetch(session, playlist_url, params={"page": page}))
            results = await asyncio.gather(*requests)

        for page in results:
            title_artist_pairs.extend(find_title_artist_pairs(page))

        return playlist_title, title_artist_pairs

    async def _make_query_mock(
        self,
        _: str,
        s: Status,
        callback,
    ) -> tuple[str | None, bool]:
        await asyncio.sleep(random.uniform(1, 20))
        if random.randint(0, 4) >= 1:
            s.found += 1
        else:
            s.failed += 1
        callback()
        return None, False
