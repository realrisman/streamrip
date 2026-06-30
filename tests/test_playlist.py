import os
from unittest.mock import MagicMock

from streamrip.media.playlist import Playlist
from streamrip.media.track import format_track_filename
from streamrip.metadata.util import get_album_id_from_track


def _make_config(tmp_path, *, disc_subdirectories=False):
    config = MagicMock()
    fp = config.session.filepaths
    fp.track_format = "{tracknumber} - {title}"
    fp.restrict_characters = False
    fp.truncate_to = 0
    dl = config.session.downloads
    dl.folder = str(tmp_path)
    dl.disc_subdirectories = disc_subdirectories
    return config


def _make_info(album_id, stem, *, artist, title, disctotal=1, discnumber=1, track_id="t"):
    info = MagicMock()
    info.album_id = album_id
    info.track_id = track_id
    info.album_meta.disctotal = disctotal
    info.track_meta.format_track_path.return_value = stem
    info.track_meta.artist = artist
    info.track_meta.title = title
    info.track_meta.discnumber = discnumber
    return info


def test_format_track_filename_truncates():
    meta = MagicMock()
    meta.format_track_path.return_value = "A very long track title indeed"
    config = MagicMock()
    config.session.filepaths.track_format = "{title}"
    config.session.filepaths.restrict_characters = False
    config.session.filepaths.truncate_to = 10

    assert format_track_filename(meta, config) == "A very lon"


def test_write_m3u_uses_relative_paths_into_album_folders(tmp_path):
    config = _make_config(tmp_path)

    # Two albums downloaded directly under the downloads folder.
    album1 = MagicMock(folder=os.path.join(str(tmp_path), "Album One"))
    album2 = MagicMock(folder=os.path.join(str(tmp_path), "Album Two"))
    os.makedirs(album1.folder)
    os.makedirs(album2.folder)
    # The actual track files (extension differs to prove glob-by-stem works).
    open(os.path.join(album1.folder, "01 - First.flac"), "w").close()
    open(os.path.join(album2.folder, "02 - Second.mp3"), "w").close()

    infos = [
        _make_info("A1", "01 - First", artist="Artist A", title="First"),
        _make_info("A2", "02 - Second", artist="Artist B", title="Second"),
    ]
    resolved = {"A1": album1, "A2": album2}

    playlist = Playlist("My Mix", config, MagicMock(), [], MagicMock())
    playlist._write_m3u(infos, resolved, {})

    m3u_path = os.path.join(str(tmp_path), "playlist", "My Mix.m3u")
    assert os.path.exists(m3u_path)
    with open(m3u_path, encoding="utf-8") as f:
        content = f.read()

    assert content.splitlines() == [
        "#EXTM3U",
        "#EXTINF:-1,Artist A - First",
        os.path.join("..", "Album One", "01 - First.flac"),
        "#EXTINF:-1,Artist B - Second",
        os.path.join("..", "Album Two", "02 - Second.mp3"),
    ]


def test_write_m3u_omits_missing_album_and_missing_file(tmp_path):
    config = _make_config(tmp_path)

    album1 = MagicMock(folder=os.path.join(str(tmp_path), "Album One"))
    os.makedirs(album1.folder)
    open(os.path.join(album1.folder, "01 - First.flac"), "w").close()

    infos = [
        _make_info("A1", "01 - First", artist="Artist A", title="First"),
        # Album was never downloaded (not in resolved map) -> omitted.
        _make_info("A2", "02 - Second", artist="Artist B", title="Second"),
        # Album downloaded but the file is absent on disk -> omitted.
        _make_info("A3", "03 - Third", artist="Artist C", title="Third"),
    ]
    album3 = MagicMock(folder=os.path.join(str(tmp_path), "Album Three"))
    os.makedirs(album3.folder)
    resolved = {"A1": album1, "A3": album3}

    playlist = Playlist("Mix", config, MagicMock(), [], MagicMock())
    playlist._write_m3u(infos, resolved, {})

    with open(os.path.join(str(tmp_path), "playlist", "Mix.m3u"), encoding="utf-8") as f:
        content = f.read()

    # Only the one track whose file exists is referenced.
    assert content.splitlines() == [
        "#EXTM3U",
        "#EXTINF:-1,Artist A - First",
        os.path.join("..", "Album One", "01 - First.flac"),
    ]


def test_write_m3u_honors_disc_subdirectories(tmp_path):
    config = _make_config(tmp_path, disc_subdirectories=True)

    album = MagicMock(folder=os.path.join(str(tmp_path), "Multi Disc"))
    disc2 = os.path.join(album.folder, "Disc 2")
    os.makedirs(disc2)
    open(os.path.join(disc2, "05 - Deep Cut.flac"), "w").close()

    info = _make_info(
        "A1", "05 - Deep Cut", artist="Artist", title="Deep Cut",
        disctotal=2, discnumber=2,
    )

    playlist = Playlist("Discs", config, MagicMock(), [], MagicMock())
    playlist._write_m3u([info], {"A1": album}, {})

    with open(os.path.join(str(tmp_path), "playlist", "Discs.m3u"), encoding="utf-8") as f:
        content = f.read()

    assert os.path.join("..", "Multi Disc", "Disc 2", "05 - Deep Cut.flac") in content


def test_write_m3u_references_singles_for_album_less_tracks(tmp_path):
    """Tracks with no album (album_id is None) are referenced via the resolved
    single Track's download_path."""
    config = _make_config(tmp_path)

    # A single downloaded somewhere under the downloads folder.
    single_dir = os.path.join(str(tmp_path), "Some Artist")
    os.makedirs(single_dir)
    single_file = os.path.join(single_dir, "Loose Track.flac")
    open(single_file, "w").close()
    track = MagicMock(download_path=single_file)

    info = _make_info(
        None, "Loose Track", artist="Some Artist", title="Loose Track",
        track_id="sc123",
    )

    playlist = Playlist("Mix", config, MagicMock(), [], MagicMock())
    playlist._write_m3u([info], {}, {"sc123": track})

    with open(os.path.join(str(tmp_path), "playlist", "Mix.m3u"), encoding="utf-8") as f:
        content = f.read()

    assert content.splitlines() == [
        "#EXTM3U",
        "#EXTINF:-1,Some Artist - Loose Track",
        os.path.join("..", "Some Artist", "Loose Track.flac"),
    ]


def test_get_album_id_from_track():
    # qobuz/tidal/deezer expose the fetchable album id at resp["album"]["id"].
    for source in ("qobuz", "tidal", "deezer"):
        resp = {"album": {"id": "0060254767005", "qobuz_id": 30369460}}
        assert get_album_id_from_track(source, resp) == "0060254767005"

    # soundcloud has no album concept.
    assert get_album_id_from_track("soundcloud", {"album": {"id": "x"}}) is None
    # missing/empty album object -> None.
    assert get_album_id_from_track("qobuz", {}) is None
    assert get_album_id_from_track("qobuz", {"album": {}}) is None
