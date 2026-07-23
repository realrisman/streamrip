import asyncio
import glob
import html
import logging
import os
import random
import re
from collections import deque
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
from ..filepath_utils import clean_filename
from ..metadata import (
    AlbumMetadata,
    PlaylistMetadata,
    SearchResults,
    TrackMetadata,
)
from ..metadata.util import get_album_id_from_track
from ..utils.ssl_utils import get_aiohttp_connector_kwargs
from .album import Album, PendingAlbum
from .media import Media, Pending
from .track import (
    PendingSingle,
    Track,
    album_folder,
    disc_subfolder,
    format_track_filename,
    singles_folder,
)

logger = logging.getLogger("streamrip")

# Number of playlist tracks whose metadata is resolved concurrently.
TRACK_RESOLVE_CHUNK = 20
# Number of albums/singles downloaded concurrently per batch.
DOWNLOAD_CHUNK = 5
# Prefix of the m3u comment line that records a track's stable streamrip
# identity (`<source>:<track_id>`). It is a comment (starts with '#'), so media
# players ignore it, but it lets a re-run match an existing entry to its track
# exactly instead of guessing from the `artist - title` label.
STREAMRIP_ID_PREFIX = "#STREAMRIP:"


@dataclass(slots=True)
class PlaylistTrackInfo:
    """Resolved metadata for one playlist entry.

    Records which album backs the track and the metadata needed to locate the
    track's file on disk once the album has been downloaded.
    """

    position: int
    track_id: str
    client: Client
    # None when the source has no album for this track (e.g. SoundCloud); such
    # tracks are downloaded as singles instead of as part of an album.
    album_id: str | None
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
            # NOTE: not album.info.id — for some sources (Qobuz, Tidal) that is
            # not the id the album endpoint accepts. Pull the fetchable album id
            # straight from the track response instead.
            album_id=get_album_id_from_track(self.client.source, resp),
            album_meta=album,
            track_meta=meta,
        )


@dataclass(slots=True)
class Playlist(Media):
    name: str
    config: Config
    client: Client
    tracks: list[PendingPlaylistTrack]
    db: Database

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

        # Phase B: download each distinct album once (full album), then locate
        # each playlist entry's file inside its album folder. Any track that
        # could not be located there (album-less, album failed, or the track
        # failed inside an otherwise-successful album) falls back to a single
        # download so a streamable track is never lost.
        resolved_albums = await self._download_albums(infos)
        album_files = self._locate_album_files(infos, resolved_albums)
        single_needed = [info for info in infos if info.position not in album_files]
        resolved_singles = await self._download_singles(single_needed)

        # Phase C: write an m3u that references the tracks inside their album
        # folders (which live outside the `playlist` folder).
        self._write_m3u(infos, album_files, resolved_singles)

    async def _resolve_track_infos(self) -> list[PlaylistTrackInfo]:
        infos: list[PlaylistTrackInfo] = []
        for batch in self.batch(self.tracks, TRACK_RESOLVE_CHUNK):
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
    ) -> dict[tuple[str, str], Album]:
        # Dedupe albums by (source, album_id). The source must be part of the
        # key: a last.fm playlist can mix sources, and a bare numeric id can
        # collide across them. Tracks without an album (album_id is None) are
        # skipped here and handled as singles instead.
        return await self._dedupe_resolve_download(
            infos, lambda info: info.album_id, PendingAlbum
        )

    async def _download_singles(
        self,
        infos: list[PlaylistTrackInfo],
    ) -> dict[tuple[str, str], Track]:
        """Download the given playlist tracks as singles.

        `infos` are the entries that could not be located inside a downloaded
        album: album-less tracks (e.g. SoundCloud), tracks whose backing album
        failed to download, and tracks that failed inside an otherwise-
        successful album. Downloading them as singles recovers these within-run
        cases so a freshly-streamable track is never lost. (A track already in
        the database whose file was removed out-of-band between runs is *not*
        re-downloaded — `PendingSingle.resolve` honors the global downloaded
        ledger, same as elsewhere in streamrip; clear the DB or use `--no-db`
        to force a re-fetch.) Returns the resolved `Track` keyed by
        (source, track_id) so the m3u can reference the downloaded file.
        """
        return await self._dedupe_resolve_download(
            infos, lambda info: info.track_id, PendingSingle
        )

    async def _dedupe_resolve_download(
        self,
        infos: list[PlaylistTrackInfo],
        key_id,
        pending_cls,
    ) -> dict[tuple[str, str], Media]:
        """Resolve and download each distinct media item once, concurrently.

        `key_id(info)` selects the id to download (album id or track id); entries
        whose id is None are skipped. Items are deduped by (source, id) — the
        source is part of the key because a last.fm playlist can mix sources
        whose bare ids may collide. Returns the downloaded objects keyed by
        (source, id).
        """
        unique: dict[tuple[str, str], Client] = {}
        for info in infos:
            id = key_id(info)
            if id is None:
                continue
            unique.setdefault((info.client.source, id), info.client)

        resolved: dict[tuple[str, str], Media] = {}
        for batch in self.batch(list(unique.items()), DOWNLOAD_CHUNK):
            results = await asyncio.gather(
                *[
                    self._resolve_and_download(pending_cls, key[1], client)
                    for key, client in batch
                ],
                return_exceptions=True,
            )
            for (key, _), result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.error(f"Error downloading {key}: {result}")
                elif result is not None:
                    resolved[key] = result
        return resolved

    async def _resolve_and_download(
        self,
        pending_cls,
        id: str,
        client: Client,
    ) -> Media | None:
        obj = await pending_cls(id, client, self.config, self.db).resolve()
        if obj is None:
            return None
        await obj.rip()
        return obj

    def _write_m3u(
        self,
        infos: list[PlaylistTrackInfo],
        album_files: dict[int, str],
        resolved_singles: dict[tuple[str, str], Track],
    ):
        downloads_config = self.config.session.downloads
        playlist_folder = os.path.join(downloads_config.folder, "playlist")
        os.makedirs(playlist_folder, exist_ok=True)
        m3u_path = os.path.join(playlist_folder, clean_filename(self.name) + ".m3u")

        # Index an existing m3u's entries for cross-run carry-over. Prefer the
        # stable streamrip id (`source:track_id`) when present: it identifies the
        # exact track, so two tracks sharing one `artist - title` label don't
        # collide and a track that moved on disk replaces its prior entry in
        # place. Id-less entries (legacy m3us, or files written by another tool)
        # fall back to label matching, consumed in order so distinct same-label
        # tracks still keep their own paths.
        existing = self._existing_m3u_entries(m3u_path)
        existing_by_srid: dict[str, str] = {}
        legacy_by_label: dict[str, deque[str]] = {}
        for extinf, srid, rel in existing:
            if srid is not None:
                existing_by_srid.setdefault(srid, rel)
            else:
                legacy_by_label.setdefault(extinf, deque()).append(rel)

        # Build the entries strictly in playlist order. For each track prefer the
        # file located this run; otherwise carry over its prior entry so a
        # degraded re-run keeps the track (in its playlist position) rather than
        # dropping it or hoisting the few re-located tracks to the front.
        # `located_any` distinguishes "located nothing this run" (leave any
        # existing file untouched) from "located some" (safe to rewrite).
        entries: list[tuple[str, str | None, str]] = []
        seen_srids: set[str] = set()
        located_any = False
        for info in infos:
            srid = f"{info.client.source}:{info.track_id}"
            extinf = f"#EXTINF:-1,{info.track_meta.artist} - {info.track_meta.title}"
            track_file = album_files.get(info.position)
            if track_file is None:
                track_file = self._locate_single_file(info, resolved_singles)
            if track_file is not None:
                located_any = True
                rel_path = self._relpath(track_file, playlist_folder)
            elif srid in existing_by_srid:
                rel_path = existing_by_srid[srid]
            elif legacy_by_label.get(extinf):
                rel_path = legacy_by_label[extinf].popleft()
            else:
                logger.warning(
                    f"Could not locate downloaded file for '{info.track_meta.title}'; "
                    "omitting from playlist file",
                )
                continue
            entries.append((extinf, srid, rel_path))
            seen_srids.add(srid)

        # Don't clobber a previously-good playlist (with a header-only stub, or a
        # mere reordering) when nothing could be located this run, e.g. a
        # transient all-fail re-run; leave any existing file untouched.
        if not located_any:
            logger.warning(
                f"No tracks could be located for playlist '{self.name}'; "
                "leaving any existing playlist file untouched",
            )
            return

        # Reconcile the remaining existing entries, appended after the playlist
        # tracks so the playlist order above is undisturbed:
        #   - A streamrip entry (has an id) whose id is no longer in the playlist
        #     was removed from the source playlist -> drop it.
        #   - An id-less entry (legacy, or written by another tool) is kept,
        #     deduped by label and path, preserving the "never lose a
        #     previously-referenced track" guarantee for genuinely foreign
        #     content. (A legacy streamrip entry matched to a current track was
        #     already consumed above and rewritten with its id.)
        seen_labels = {extinf for extinf, _, _ in entries}
        seen_paths = {rel for _, _, rel in entries}
        for extinf, srid, rel in existing:
            if srid is not None:
                continue
            if extinf not in seen_labels and rel not in seen_paths:
                seen_labels.add(extinf)
                seen_paths.add(rel)
                entries.append((extinf, None, rel))

        lines = ["#EXTM3U"]
        for extinf, srid, rel in entries:
            lines.append(extinf)
            if srid is not None:
                lines.append(STREAMRIP_ID_PREFIX + srid)
            lines.append(rel)

        # surrogateescape: entries carried over from an existing m3u written by
        # another tool may hold non-UTF-8 bytes (round-tripped as surrogates by
        # `_existing_m3u_entries`); re-emit them byte-for-byte instead of raising.
        with open(m3u_path, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("\n".join(lines) + "\n")
        logger.info(f"Wrote playlist file to {m3u_path}")
        console.print(f"[green]Wrote playlist file to[/green] {m3u_path}")

    @staticmethod
    def _relpath(track_file: str, playlist_folder: str) -> str:
        """Return `track_file` relative to `playlist_folder`.

        Falls back to the absolute path (with a warning) when the two live on
        different drives (Windows), where `os.path.relpath` raises `ValueError`:
        the track stays referenced and playable instead of the `ValueError`
        aborting the whole m3u write after every album has already downloaded.
        """
        try:
            return os.path.relpath(track_file, start=playlist_folder)
        except ValueError:
            logger.warning(
                f"Cannot relativize '{track_file}' against the playlist folder "
                "(different drive?); referencing it by absolute path",
            )
            return track_file

    def _locate_album_files(
        self,
        infos: list[PlaylistTrackInfo],
        resolved_albums: dict[tuple[str, str], Album],
    ) -> dict[int, str]:
        """Locate each playlist entry's file inside its album.

        Returns a map of `info.position` -> on-disk path for the entries found.
        Entries omitted from the result need a single download instead.
        """
        located: dict[int, str] = {}
        for info in infos:
            if info.album_id is None:
                continue
            path = self._locate_album_file(info, resolved_albums)
            if path is not None:
                located[info.position] = path
        return located

    def _db_located(self, info: PlaylistTrackInfo) -> str | None:
        """Return the on-disk path a previous run recorded for this entry.

        Authoritative for any track downloaded after path-persistence shipped:
        it sidesteps folder reconstruction entirely (correct regardless of
        source quirks like Deezer's incomplete track-endpoint album metadata,
        and regardless of which folder — album or singles — the file lives in).
        Returns None for legacy entries with no recorded path, or if the
        recorded file no longer exists, so the caller falls back to globbing.
        """
        path = self.db.path_for(info.track_id)
        if path is not None and os.path.exists(path):
            return path
        return None

    def _locate_album_file(
        self,
        info: PlaylistTrackInfo,
        resolved_albums: dict[tuple[str, str], Album],
    ) -> str | None:
        """Locate one album-backed playlist entry's file on disk."""
        album = resolved_albums.get((info.client.source, info.album_id))
        if album is not None:
            # The album download recorded the exact path it wrote, which is
            # authoritative regardless of how `track_format`/`folder_format`
            # are configured (and so is correct even when the playlist's
            # track-embedded album metadata differs from the album endpoint's).
            path = album.track_paths.get(str(info.track_id))
            if path is not None:
                return path

            # The track was skipped this run (already in the database); prefer
            # the path a previous run recorded over reconstructing the folder.
            db_path = self._db_located(info)
            if db_path is not None:
                return db_path

            # No recorded path (downloaded before path-persistence) but the
            # album still re-resolved, so glob the album's own folder by filename
            # stem. Use the album object's folder and metadata — the album-
            # endpoint truth the writer actually used — rather than reconstructing
            # from the track-embedded album metadata, which can understate
            # `disctotal` (e.g. Deezer hardcodes it to 1 for a track response)
            # and send the glob to the album root instead of the right Disc N
            # subfolder. Globbing survives format conversion (changed extension).
            #
            # Known limitation: under a custom album-derived `track_format`, the
            # stem built here from the track-embedded metadata may differ from
            # what the album endpoint produced; the track is then re-downloaded
            # as a single rather than mis-referenced.
            folder = disc_subfolder(
                album.folder, self.config, album.meta, info.track_meta
            )
            return self._glob_stem(
                folder, format_track_filename(info.track_meta, self.config)
            )

        # The album failed to (re)resolve this run, but the file may exist from a
        # prior run. A recorded path is exact; prefer it over reconstruction.
        db_path = self._db_located(info)
        if db_path is not None:
            return db_path

        # No recorded path (legacy entry). Best-effort reconstruct the folder
        # from the track-embedded album metadata (which may understate
        # `disctotal` / custom formats); on a miss the track falls back to a
        # single download rather than a wrong reference.
        folder = album_folder(self.config, info.client.source, info.album_meta)
        folder = disc_subfolder(folder, self.config, info.album_meta, info.track_meta)
        return self._glob_stem(folder, format_track_filename(info.track_meta, self.config))

    def _locate_single_file(
        self,
        info: PlaylistTrackInfo,
        resolved_singles: dict[tuple[str, str], Track],
    ) -> str | None:
        """Return the path of a single-downloaded track for a playlist entry."""
        track = resolved_singles.get((info.client.source, info.track_id))
        if track is not None and not track.failed:
            # A failed download deletes its partial file and sets `failed`
            # (see Track.download); referencing track.download_path would point
            # the m3u at a nonexistent file. Mirror the album path's guard
            # (Album records only non-failed tracks) and fall through to the
            # glob below, which won't match the deleted file.
            return track.download_path

        # The track was skipped this run (already in the database). A recorded
        # path is exact and — unlike the singles-folder glob below — also finds
        # a track that physically lives in an album folder (e.g. an album-backed
        # entry whose album locator missed and fell through to here).
        db_path = self._db_located(info)
        if db_path is not None:
            return db_path

        # No recorded path (legacy entry). Glob the singles destination folder so
        # the m3u still references the existing file.
        folder = singles_folder(self.config, info.client.source, info.album_meta)
        return self._glob_stem(folder, format_track_filename(info.track_meta, self.config))

    @staticmethod
    def _existing_m3u_entries(m3u_path: str) -> list[tuple[str, str | None, str]]:
        """Parse `(#EXTINF, streamrip-id, path)` triples from an existing m3u
        (empty if absent).

        Each path line is paired with the most recent preceding `#EXTINF` line
        (a synthesized one if a path appears without it) and the streamrip id
        from an immediately-preceding `#STREAMRIP:<source>:<id>` comment, if any.
        The id (`source:track_id`, a stable per-track identity) lets a re-run
        match an entry to its track exactly — distinguishing two tracks that
        share an `artist - title` label, and a removed streamrip track from a
        genuinely foreign entry. `None` for legacy entries written before the id
        line existed (and for foreign entries written by another tool). Used to
        merge previously-referenced tracks across a degraded re-run.
        """
        if not os.path.exists(m3u_path):
            return []
        try:
            # surrogateescape: an existing m3u written by another tool may not be
            # UTF-8; a decode error here must not abort the whole write. Foreign
            # bytes round-trip as surrogates and are re-emitted byte-for-byte by
            # the writer, so carried-over entries keep their original labels.
            with open(m3u_path, encoding="utf-8", errors="surrogateescape") as f:
                raw = [line.rstrip("\n") for line in f]
        except OSError as e:
            logger.warning(f"Could not read existing playlist file {m3u_path}: {e}")
            return []

        entries: list[tuple[str, str | None, str]] = []
        pending_extinf: str | None = None
        pending_srid: str | None = None
        for line in raw:
            if line.startswith("#EXTINF"):
                pending_extinf = line
                pending_srid = None
            elif line.startswith(STREAMRIP_ID_PREFIX):
                pending_srid = line[len(STREAMRIP_ID_PREFIX) :]
            elif line and not line.startswith("#"):
                entries.append((pending_extinf or "#EXTINF:-1,", pending_srid, line))
                pending_extinf = None
                pending_srid = None
        return entries

    @staticmethod
    def _glob_stem(folder: str, stem: str) -> str | None:
        """Return the first file in `folder` whose name (sans extension) is `stem`.

        Matches across a changed extension (format conversion). Sorted for a
        deterministic choice if two files share a stem.
        """
        pattern = os.path.join(glob.escape(folder), glob.escape(stem) + ".*")
        matches = sorted(glob.glob(pattern))
        return matches[0] if matches else None

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
        return Playlist(name, self.config, self.client, tracks, self.db)


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

        return Playlist(playlist_title, self.config, self.client, pending_tracks, self.db)

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
