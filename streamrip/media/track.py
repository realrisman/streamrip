import asyncio
import logging
import os
from dataclasses import dataclass

from .. import converter
from ..client import Client, Downloadable
from ..config import Config
from ..db import Database
from ..exceptions import NonStreamableError
from ..filepath_utils import clean_filename, clean_filepath
from ..metadata import AlbumMetadata, Covers, TrackMetadata, tag_file
from ..progress import add_title, get_progress_callback, remove_title
from .artwork import download_artwork
from .media import Media, Pending
from .semaphore import global_download_semaphore

logger = logging.getLogger("streamrip")

# Total download attempts per track (1 initial + retries) before giving up.
MAX_DOWNLOAD_RETRIES = 3


def format_track_filename(meta: TrackMetadata, config: Config) -> str:
    """Return the cleaned, truncated track filename (without extension).

    This is the single source of truth for a track's on-disk filename stem so
    that callers which need to locate a downloaded file (e.g. playlist m3u
    generation) stay in sync with what ``Track`` actually writes.
    """
    c = config.session.filepaths
    name = clean_filename(
        meta.format_track_path(c.track_format),
        restrict=c.restrict_characters,
    )
    if c.truncate_to > 0 and len(name) > c.truncate_to:
        name = name[: c.truncate_to]
    return name


def singles_folder(config: Config, source: str, album_meta: AlbumMetadata) -> str:
    """Return the folder a single track is downloaded into.

    Single source of truth for ``PendingSingle``'s destination so that callers
    which need to locate a single's file (e.g. playlist m3u generation) stay in
    sync with what ``PendingSingle`` actually writes.
    """
    c = config.session
    parent = c.downloads.folder
    if not c.filepaths.add_singles_to_folder:
        return parent
    if c.downloads.source_subdirectories:
        parent = os.path.join(parent, source.capitalize())
    return os.path.join(parent, album_meta.format_folder_path(c.filepaths.folder_format))


def album_folder(config: Config, source: str, album_meta: AlbumMetadata) -> str:
    """Return the folder an album's tracks are downloaded into.

    Single source of truth for ``PendingAlbum``'s destination so that callers
    which need to locate an album track's file (e.g. playlist m3u generation)
    can reconstruct the exact folder the album writer used.
    """
    c = config.session
    parent = c.downloads.folder
    if c.downloads.source_subdirectories:
        parent = os.path.join(parent, source.capitalize())
    folder = clean_filepath(
        album_meta.format_folder_path(c.filepaths.folder_format),
        c.filepaths.restrict_characters,
    )
    return os.path.join(parent, folder)


@dataclass(slots=True)
class Track(Media):
    meta: TrackMetadata
    downloadable: Downloadable
    config: Config
    folder: str
    # Is None if a cover doesn't exist for the track
    cover_path: str | None
    db: Database
    # Client + quality are kept so a failed download can be retried at a lower
    # quality (see `download`).
    client: Client
    quality: int
    # change?
    download_path: str = ""
    is_single: bool = False
    # Set when all download attempts fail; skips tagging/conversion in postprocess.
    failed: bool = False

    async def preprocess(self):
        self._set_download_path()
        os.makedirs(self.folder, exist_ok=True)
        if self.is_single:
            add_title(self.meta.title)

    async def download(self):
        # TODO: progress bar description
        async with global_download_semaphore(self.config.session.downloads):
            quality = self.quality
            while True:
                if await self._download_with_retries():
                    return

                # Remove the partial/truncated file from the failed attempts so
                # it isn't tagged or left behind.
                if os.path.exists(self.download_path):
                    os.remove(self.download_path)

                # The file may be unavailable at this quality (e.g. a hi-res
                # master the CDN won't serve). Fall back to the next lower
                # quality and try again, if enabled.
                if not (
                    self.config.session.downloads.fallback_to_lower_quality
                    and quality > 0
                ):
                    break

                quality -= 1
                logger.warning(
                    f"Could not download track '{self.meta.title}' at quality "
                    f"{quality + 1}, falling back to quality {quality}"
                )
                try:
                    self.downloadable = await self.client.get_downloadable(
                        self.meta.info.id, quality
                    )
                except Exception as e:
                    logger.error(
                        f"No lower quality available for track '{self.meta.title}': {e}"
                    )
                    break
                # The extension can change between qualities (e.g. flac -> mp3).
                self._set_download_path()

            logger.error(
                f"Persistent error downloading track '{self.meta.title}', skipping"
            )
            self.db.set_failed(self.downloadable.source, "track", self.meta.info.id)
            self.failed = True

    async def _download_with_retries(self) -> bool:
        """Download at the current quality, retrying transient failures with
        exponential backoff. Returns True on success, False if every attempt
        fails."""
        for attempt in range(MAX_DOWNLOAD_RETRIES):
            label = f"Track {self.meta.tracknumber}"
            if attempt > 0:
                label += " (retry)"
            with get_progress_callback(
                self.config.session.cli.progress_bars,
                await self.downloadable.size(),
                label,
            ) as callback:
                try:
                    await self.downloadable.download(self.download_path, callback)
                    return True
                except Exception as e:
                    logger.error(
                        f"Error downloading track '{self.meta.title}', retrying: {e}"
                    )
                    if attempt < MAX_DOWNLOAD_RETRIES - 1:
                        # Exponential backoff between attempts.
                        await asyncio.sleep(2**attempt)
        return False

    async def postprocess(self):
        if self.is_single:
            remove_title(self.meta.title)

        # Download failed: nothing valid on disk to tag/convert/record.
        if self.failed:
            return

        await tag_file(self.download_path, self.meta, self.cover_path)
        if self.config.session.conversion.enabled:
            await self._convert()

        self.db.set_downloaded(self.meta.info.id)

    async def _convert(self):
        c = self.config.session.conversion
        engine_class = converter.get(c.codec)
        engine = engine_class(
            filename=self.download_path,
            sampling_rate=c.sampling_rate,
            bit_depth=c.bit_depth,
            remove_source=True,  # always going to delete the old file
        )
        await engine.convert()
        self.download_path = engine.final_fn  # because the extension changed

    def _set_download_path(self):
        track_path = format_track_filename(self.meta, self.config)
        self.download_path = os.path.join(
            self.folder,
            f"{track_path}.{self.downloadable.extension}",
        )


@dataclass(slots=True)
class PendingTrack(Pending):
    id: str
    album: AlbumMetadata
    client: Client
    config: Config
    folder: str
    db: Database
    # cover_path is None <==> Artwork for this track doesn't exist in API
    cover_path: str | None

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            return None

        source = self.client.source
        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Track {self.id} not available for stream on {source}: {e}")
            return None

        try:
            meta = TrackMetadata.from_resp(self.album, source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for {self.id}: {e}")
            return None

        if meta is None:
            logger.error(f"Track {self.id} not available for stream on {source}")
            self.db.set_failed(source, "track", self.id)
            return None

        quality = self.config.session.get_source(source).quality
        try:
            downloadable = await self.client.get_downloadable(self.id, quality)
        except NonStreamableError as e:
            logger.error(
                f"Error getting downloadable data for track {meta.tracknumber} [{self.id}]: {e}"
            )
            return None

        downloads_config = self.config.session.downloads
        if downloads_config.disc_subdirectories and self.album.disctotal > 1:
            folder = os.path.join(self.folder, f"Disc {meta.discnumber}")
        else:
            folder = self.folder

        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            self.cover_path,
            self.db,
            self.client,
            quality,
        )


@dataclass(slots=True)
class PendingSingle(Pending):
    """Whereas PendingTrack is used in the context of an album, where the album metadata
    and cover have been resolved, PendingSingle is used when a single track is downloaded.

    This resolves the Album metadata and downloads the cover to pass to the Track class.
    """

    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            return None

        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Error fetching track {self.id}: {e}")
            return None
        # Patch for soundcloud
        try:
            album = AlbumMetadata.from_track_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error building album metadata for track {id=}: {e}")
            return None

        if album is None:
            self.db.set_failed(self.client.source, "track", self.id)
            logger.error(
                f"Cannot stream track (am) ({self.id}) on {self.client.source}",
            )
            return None

        try:
            meta = TrackMetadata.from_resp(album, self.client.source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for track {id=}: {e}")
            return None

        if meta is None:
            self.db.set_failed(self.client.source, "track", self.id)
            logger.error(
                f"Cannot stream track (tm) ({self.id}) on {self.client.source}",
            )
            return None

        config = self.config.session
        quality = getattr(config, self.client.source).quality
        assert isinstance(quality, int)
        folder = singles_folder(self.config, self.client.source, album)

        os.makedirs(folder, exist_ok=True)

        embedded_cover_path, downloadable = await asyncio.gather(
            self._download_cover(album.covers, folder),
            self.client.get_downloadable(self.id, quality),
        )
        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            embedded_cover_path,
            self.db,
            self.client,
            quality,
            is_single=True,
        )

    async def _download_cover(self, covers: Covers, folder: str) -> str | None:
        embed_path, _ = await download_artwork(
            self.client.session,
            folder,
            covers,
            self.config.session.artwork,
            for_playlist=False,
        )
        return embed_path
