import functools
from typing import Optional, Type, TypeVar


def get_album_track_ids(source: str, resp) -> list[str]:
    tracklist = resp["tracks"]
    if source == "qobuz":
        tracklist = tracklist["items"]
    return [track["id"] for track in tracklist]


def get_album_id_from_track(source: str, track_resp) -> str | None:
    """Return the fetchable album id for the album a track belongs to.

    The album id embedded in a track's metadata is the value that the album
    endpoint accepts (e.g. for Qobuz this is the UPC-style ``album.id``, not the
    internal ``qobuz_id`` that ``AlbumMetadata`` exposes). Returns None when the
    source has no separate album (SoundCloud) or the track carries no album
    object, in which case the track is downloaded as a single instead.
    """
    if source == "soundcloud":
        return None
    album = track_resp.get("album") if isinstance(track_resp, dict) else None
    if not album:
        return None
    album_id = album.get("id")
    if not album_id:  # None, "", or 0 -> no fetchable album
        return None
    return str(album_id)


def safe_get(dictionary, *keys, default=None):
    return functools.reduce(
        lambda d, key: d.get(key, default) if isinstance(d, dict) else default,
        keys,
        dictionary,
    )


T = TypeVar("T")


def typed(thing, expected_type: Type[T]) -> T:
    assert isinstance(thing, expected_type)
    return thing


def get_quality_id(
    bit_depth: Optional[int],
    sampling_rate: Optional[int | float],
) -> int:
    """Get the universal quality id from bit depth and sampling rate.

    :param bit_depth:
    :type bit_depth: Optional[int]
    :param sampling_rate: In kHz
    :type sampling_rate: Optional[int]
    """
    # XXX: Should `0` quality be supported?
    if bit_depth is None or sampling_rate is None:  # is lossy
        return 1

    if bit_depth == 16:
        return 2

    if bit_depth == 24:
        if sampling_rate <= 96:
            return 3

        return 4

    raise Exception(f"Invalid {bit_depth = }")
