import asyncio
import logging
import os
from dataclasses import dataclass, field

from .. import progress
from ..client import Client
from ..config import Config
from ..db import Database
from ..exceptions import NonStreamableError
from ..metadata import AlbumMetadata
from ..metadata.util import get_album_track_ids
from .artwork import download_artwork
from .media import Media, Pending
from .track import PendingTrack, album_folder

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Album(Media):
    meta: AlbumMetadata
    tracks: list[PendingTrack]
    config: Config
    # folder where the tracks will be downloaded
    folder: str
    db: Database
    # Maps track id -> final on-disk path for each track written during this
    # run. Lets callers (e.g. playlist m3u generation) reference the exact file
    # the download produced instead of re-discovering it by globbing. Tracks
    # skipped (already in the database) are absent and located by other means.
    track_paths: dict[str, str] = field(default_factory=dict)
    # Keep fully-downloaded albums resolved so callers can still use their
    # metadata and folder, while making rip() free of filesystem/network work.
    skip_download: bool = False

    async def preprocess(self):
        if self.skip_download:
            return
        progress.add_title(self.meta.album)

    async def download(self):
        if self.skip_download:
            return

        async def _resolve_and_download(pending: Pending):
            try:
                track = await pending.resolve()
                if track is None:
                    return
                await track.rip()
                if not track.failed:
                    self.track_paths[str(track.meta.info.id)] = track.download_path
            except Exception as e:
                logger.error(f"Error downloading track: {e}")

        results = await asyncio.gather(
            *[_resolve_and_download(p) for p in self.tracks], return_exceptions=True
        )

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Album track processing error: {result}")

    async def postprocess(self):
        if self.skip_download:
            return
        progress.remove_title(self.meta.album)


@dataclass(slots=True)
class PendingAlbum(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Album | None:
        try:
            resp = await self.client.get_metadata(self.id, "album")
        except NonStreamableError as e:
            logger.error(
                f"Album {self.id} not available to stream on {self.client.source} ({e})",
            )
            return None

        try:
            meta = AlbumMetadata.from_album_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error building album metadata for {id=}: {e}")
            return None

        if meta is None:
            logger.error(
                f"Album {self.id} not available to stream on {self.client.source}",
            )
            return None

        tracklist = get_album_track_ids(self.client.source, resp)
        skip_download = bool(tracklist) and all(
            self.db.downloaded(track_id) for track_id in tracklist
        )
        if skip_download:
            logger.info(
                f"Skipping album {self.id}. All tracks are marked as downloaded "
                "in the database.",
            )

        album_dir = album_folder(self.config, self.client.source, meta)
        embed_cover = None
        if not skip_download:
            os.makedirs(album_dir, exist_ok=True)
            embed_cover, _ = await download_artwork(
                self.client.session,
                album_dir,
                meta.covers,
                self.config.session.artwork,
                for_playlist=False,
            )
        pending_tracks = [
            PendingTrack(
                id,
                album=meta,
                client=self.client,
                config=self.config,
                folder=album_dir,
                db=self.db,
                cover_path=embed_cover,
            )
            for id in tracklist
        ]
        logger.debug("Pending tracks: %s", pending_tracks)
        return Album(
            meta,
            pending_tracks,
            self.config,
            album_dir,
            self.db,
            skip_download=skip_download,
        )
