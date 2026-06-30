import os
from unittest.mock import MagicMock

from streamrip.media.playlist import Playlist
from streamrip.media.track import format_track_filename, singles_folder
from streamrip.metadata.util import get_album_id_from_track


def _make_config(
    tmp_path,
    *,
    disc_subdirectories=False,
    add_singles_to_folder=False,
    source_subdirectories=False,
):
    config = MagicMock()
    fp = config.session.filepaths
    fp.track_format = "{tracknumber} - {title}"
    fp.restrict_characters = False
    fp.truncate_to = 0
    fp.add_singles_to_folder = add_singles_to_folder
    fp.folder_format = "{albumartist} - {album}"
    dl = config.session.downloads
    dl.folder = str(tmp_path)
    dl.disc_subdirectories = disc_subdirectories
    dl.source_subdirectories = source_subdirectories
    return config


def _make_info(
    album_id,
    stem,
    *,
    artist,
    title,
    position=1,
    disctotal=1,
    discnumber=1,
    track_id="t",
    source="qobuz",
    album_dir="Album",
):
    info = MagicMock()
    info.position = position
    info.client.source = source
    info.album_id = album_id
    info.track_id = track_id
    info.album_meta.disctotal = disctotal
    # The locator reconstructs the album folder from metadata; this is the
    # folder name the album writer would have produced.
    info.album_meta.format_folder_path.return_value = album_dir
    info.track_meta.format_track_path.return_value = stem
    info.track_meta.artist = artist
    info.track_meta.title = title
    info.track_meta.discnumber = discnumber
    return info


def _make_album(folder, *, disctotal=1, track_paths=None):
    """Build a resolved-Album stand-in.

    The locator reads `album.folder` and `album.meta.disctotal` (the album-
    endpoint truth) when a track was skipped this run, so both must be concrete.
    """
    album = MagicMock()
    album.folder = folder
    album.meta.disctotal = disctotal
    album.track_paths = {} if track_paths is None else track_paths
    return album


def _playlist(name, config):
    return Playlist(name, config, MagicMock(), [], MagicMock())


def test_format_track_filename_truncates():
    meta = MagicMock()
    meta.format_track_path.return_value = "A very long track title indeed"
    config = MagicMock()
    config.session.filepaths.track_format = "{title}"
    config.session.filepaths.restrict_characters = False
    config.session.filepaths.truncate_to = 10

    assert format_track_filename(meta, config) == "A very lon"


# --- _locate_album_files -----------------------------------------------------


def test_locate_album_files_prefers_exact_recorded_path(tmp_path):
    """A track downloaded this run is located via the album's recorded
    track_paths (the exact file written), independent of track_format."""
    config = _make_config(tmp_path)

    album = MagicMock(
        track_paths={"42": os.path.join(str(tmp_path), "Album One", "anything.flac")},
    )
    info = _make_info(
        "A1", "ignored stem", artist="Artist", title="First",
        track_id="42", position=1,
    )

    located = _playlist("Mix", config)._locate_album_files(
        [info], {("qobuz", "A1"): album}
    )

    assert located == {1: album.track_paths["42"]}


def test_locate_album_files_fallback_globs_reconstructed_folder(tmp_path):
    """A track skipped this run (not in track_paths) is found by reconstructing
    the album folder from metadata and globbing the stem, across a changed ext."""
    config = _make_config(tmp_path)

    album_dir = os.path.join(str(tmp_path), "Album One")
    os.makedirs(album_dir)
    # Extension differs from any assumption to prove stem-based matching.
    open(os.path.join(album_dir, "01 - First.mp3"), "w").close()

    album = _make_album(album_dir)
    info = _make_info(
        "A1", "01 - First", artist="Artist", title="First",
        position=1, album_dir="Album One",
    )

    located = _playlist("Mix", config)._locate_album_files(
        [info], {("qobuz", "A1"): album}
    )

    assert located == {1: os.path.join(album_dir, "01 - First.mp3")}


def test_locate_album_files_honors_disc_subdirectories(tmp_path):
    config = _make_config(tmp_path, disc_subdirectories=True)

    disc2 = os.path.join(str(tmp_path), "Multi Disc", "Disc 2")
    os.makedirs(disc2)
    open(os.path.join(disc2, "05 - Deep Cut.flac"), "w").close()

    album = _make_album(os.path.join(str(tmp_path), "Multi Disc"), disctotal=2)
    info = _make_info(
        "A1", "05 - Deep Cut", artist="Artist", title="Deep Cut",
        disctotal=2, discnumber=2, position=1, album_dir="Multi Disc",
    )

    located = _playlist("Discs", config)._locate_album_files(
        [info], {("qobuz", "A1"): album}
    )

    assert located == {1: os.path.join(disc2, "05 - Deep Cut.flac")}


def test_locate_album_files_disc_scoping_avoids_cross_disc_collision(tmp_path):
    """Regression (finding 1): two discs containing a file with the same stem
    must each resolve to their own disc's file, not collide onto disc 1."""
    config = _make_config(tmp_path, disc_subdirectories=True)

    base = os.path.join(str(tmp_path), "Multi Disc")
    disc1 = os.path.join(base, "Disc 1")
    disc2 = os.path.join(base, "Disc 2")
    os.makedirs(disc1)
    os.makedirs(disc2)
    open(os.path.join(disc1, "01 - Intro.flac"), "w").close()
    open(os.path.join(disc2, "01 - Intro.flac"), "w").close()

    album = _make_album(base, disctotal=2)
    infos = [
        _make_info("A1", "01 - Intro", artist="Artist", title="Intro",
                   disctotal=2, discnumber=1, position=1, album_dir="Multi Disc"),
        _make_info("A1", "01 - Intro", artist="Artist", title="Intro",
                   disctotal=2, discnumber=2, position=2, album_dir="Multi Disc"),
    ]

    located = _playlist("Discs", config)._locate_album_files(
        infos, {("qobuz", "A1"): album}
    )

    assert located == {
        1: os.path.join(disc1, "01 - Intro.flac"),
        2: os.path.join(disc2, "01 - Intro.flac"),
    }


def test_locate_album_files_uses_album_meta_disctotal_over_track_meta(tmp_path):
    """Regression (finding 2): for a track skipped this run, the disc subfolder
    is chosen from the resolved album's metadata (album-endpoint truth), not the
    track-embedded album metadata which can understate disctotal (e.g. Deezer
    hardcodes it to 1 for a track response)."""
    config = _make_config(tmp_path, disc_subdirectories=True)

    base = os.path.join(str(tmp_path), "Multi Disc")
    disc2 = os.path.join(base, "Disc 2")
    os.makedirs(disc2)
    open(os.path.join(disc2, "05 - Deep Cut.flac"), "w").close()

    # The album re-resolved this run and knows it is a 2-disc set.
    album = _make_album(base, disctotal=2)
    # The track-embedded album metadata understates disctotal (=1) -- the bug
    # input. Were it used, the glob would look in the album root and miss.
    info = _make_info(
        "A1", "05 - Deep Cut", artist="Artist", title="Deep Cut",
        disctotal=1, discnumber=2, position=1, album_dir="Multi Disc",
    )

    located = _playlist("Discs", config)._locate_album_files(
        [info], {("qobuz", "A1"): album}
    )

    assert located == {1: os.path.join(disc2, "05 - Deep Cut.flac")}


def test_locate_album_files_finds_file_when_album_absent(tmp_path):
    """Regression (finding 2): an album-backed track whose album failed to
    resolve this run but exists on disk from a prior run is still located in its
    (reconstructed) album folder rather than dropped to a (wrong) singles path."""
    config = _make_config(tmp_path)

    album_dir = os.path.join(str(tmp_path), "Prior Album")
    os.makedirs(album_dir)
    open(os.path.join(album_dir, "01 - Old.flac"), "w").close()

    info = _make_info(
        "A1", "01 - Old", artist="Artist", title="Old",
        position=1, album_dir="Prior Album",
    )

    # resolved_albums is empty: the album did not (re)resolve this run.
    located = _playlist("Mix", config)._locate_album_files([info], {})

    assert located == {1: os.path.join(album_dir, "01 - Old.flac")}


def test_locate_album_files_omits_when_file_absent(tmp_path):
    """Tracks whose file is absent on disk (album never downloaded, or a failed
    track inside a downloaded album) are omitted so they fall back to singles."""
    config = _make_config(tmp_path)

    album_one = os.path.join(str(tmp_path), "Album One")
    os.makedirs(album_one)
    open(os.path.join(album_one, "01 - First.flac"), "w").close()
    os.makedirs(os.path.join(str(tmp_path), "Album Three"))  # dir exists, file absent

    album1 = _make_album(album_one)
    album3 = _make_album(os.path.join(str(tmp_path), "Album Three"))
    infos = [
        _make_info("A1", "01 - First", artist="A", title="First",
                   position=1, album_dir="Album One"),
        # Album never downloaded (absent from resolved) and no file on disk.
        _make_info("A2", "02 - Second", artist="B", title="Second",
                   position=2, album_dir="Album Two"),
        # Album downloaded but the file is missing on disk (failed track).
        _make_info("A3", "03 - Third", artist="C", title="Third",
                   position=3, album_dir="Album Three"),
    ]
    resolved = {("qobuz", "A1"): album1, ("qobuz", "A3"): album3}

    located = _playlist("Mix", config)._locate_album_files(infos, resolved)

    assert located == {1: os.path.join(album_one, "01 - First.flac")}


def test_locate_album_files_namespaces_album_id_by_source(tmp_path):
    """Two different albums sharing a bare id on different sources don't
    collide: each entry resolves to its own source's album."""
    config = _make_config(tmp_path)

    album_q = MagicMock(
        track_paths={"tq": os.path.join(str(tmp_path), "Q", "q.flac")},
    )
    album_t = MagicMock(
        track_paths={"tt": os.path.join(str(tmp_path), "T", "t.flac")},
    )
    infos = [
        _make_info("100", "q", artist="A", title="Q", track_id="tq",
                   source="qobuz", position=1),
        _make_info("100", "t", artist="B", title="T", track_id="tt",
                   source="tidal", position=2),
    ]
    resolved = {("qobuz", "100"): album_q, ("tidal", "100"): album_t}

    located = _playlist("Mix", config)._locate_album_files(infos, resolved)

    assert located == {1: album_q.track_paths["tq"], 2: album_t.track_paths["tt"]}


# --- _write_m3u --------------------------------------------------------------


def test_write_m3u_uses_relative_paths_into_album_folders(tmp_path):
    config = _make_config(tmp_path)

    f1 = os.path.join(str(tmp_path), "Album One", "01 - First.flac")
    f2 = os.path.join(str(tmp_path), "Album Two", "02 - Second.mp3")

    infos = [
        _make_info("A1", "01 - First", artist="Artist A", title="First", position=1),
        _make_info("A2", "02 - Second", artist="Artist B", title="Second", position=2),
    ]
    album_files = {1: f1, 2: f2}

    _playlist("My Mix", config)._write_m3u(infos, album_files, {})

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


def test_write_m3u_sanitizes_playlist_name_with_slash(tmp_path):
    """Regression (finding 1): a playlist name containing a path separator must
    be sanitized to a single filename component, not crash the write by treating
    the '/' as an (uncreated) subdirectory."""
    config = _make_config(tmp_path)

    f1 = os.path.join(str(tmp_path), "Album One", "01 - First.flac")
    info = _make_info("A1", "01 - First", artist="A", title="First", position=1)

    # Would raise FileNotFoundError if the name were used as a path (clean_filepath).
    _playlist("Rock/Metal", config)._write_m3u([info], {1: f1}, {})

    playlist_folder = os.path.join(str(tmp_path), "playlist")
    entries = os.listdir(playlist_folder)
    # The slash was stripped: a single .m3u file lands directly in the folder,
    # with no nested directory created from the separator.
    assert entries == ["RockMetal.m3u"]
    assert os.path.isfile(os.path.join(playlist_folder, "RockMetal.m3u"))


def test_write_m3u_omits_unlocated_tracks(tmp_path):
    config = _make_config(tmp_path)

    f1 = os.path.join(str(tmp_path), "Album One", "01 - First.flac")
    infos = [
        _make_info("A1", "01 - First", artist="Artist A", title="First", position=1),
        # Not located in an album and no single -> omitted.
        _make_info("A2", "02 - Second", artist="Artist B", title="Second", position=2),
    ]

    _playlist("Mix", config)._write_m3u(infos, {1: f1}, {})

    with open(os.path.join(str(tmp_path), "playlist", "Mix.m3u"), encoding="utf-8") as f:
        content = f.read()

    assert content.splitlines() == [
        "#EXTM3U",
        "#EXTINF:-1,Artist A - First",
        os.path.join("..", "Album One", "01 - First.flac"),
    ]


def test_write_m3u_references_singles_for_album_less_tracks(tmp_path):
    """Tracks with no album are referenced via the resolved single Track's
    download_path, keyed by (source, track_id)."""
    config = _make_config(tmp_path)

    single_file = os.path.join(str(tmp_path), "Some Artist", "Loose Track.flac")
    track = MagicMock(download_path=single_file)

    info = _make_info(
        None, "Loose Track", artist="Some Artist", title="Loose Track",
        track_id="sc123", source="soundcloud", position=1,
    )

    _playlist("Mix", config)._write_m3u(
        [info], {}, {("soundcloud", "sc123"): track}
    )

    with open(os.path.join(str(tmp_path), "playlist", "Mix.m3u"), encoding="utf-8") as f:
        content = f.read()

    assert content.splitlines() == [
        "#EXTM3U",
        "#EXTINF:-1,Some Artist - Loose Track",
        os.path.join("..", "Some Artist", "Loose Track.flac"),
    ]


def test_write_m3u_single_rerun_fallback_globs_existing_file(tmp_path):
    """An album-less single already on disk (skipped this run because it is in
    the database) is still referenced by globbing its destination folder."""
    config = _make_config(tmp_path)  # add_singles_to_folder False -> downloads root

    open(os.path.join(str(tmp_path), "Loose Track.flac"), "w").close()

    info = _make_info(
        None, "Loose Track", artist="Some Artist", title="Loose Track",
        track_id="sc1", source="soundcloud", position=1,
    )

    # resolved_singles is empty: the single was not (re)downloaded this run.
    _playlist("Mix", config)._write_m3u([info], {}, {})

    with open(os.path.join(str(tmp_path), "playlist", "Mix.m3u"), encoding="utf-8") as f:
        content = f.read()

    assert content.splitlines() == [
        "#EXTM3U",
        "#EXTINF:-1,Some Artist - Loose Track",
        os.path.join("..", "Loose Track.flac"),
    ]


def test_write_m3u_does_not_clobber_existing_file_when_nothing_located(tmp_path):
    """A re-run where nothing can be located must leave a previously-good m3u
    intact rather than truncating it to a header-only stub."""
    config = _make_config(tmp_path)

    playlist_folder = os.path.join(str(tmp_path), "playlist")
    os.makedirs(playlist_folder)
    m3u_path = os.path.join(playlist_folder, "Mix.m3u")
    good_contents = "#EXTM3U\n#EXTINF:-1,Artist - Song\n../Album/01.flac\n"
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write(good_contents)

    # Album not located and no single (file absent on disk) -> nothing to write.
    info = _make_info("A1", "01 - Missing", artist="Artist", title="Missing", position=1)
    _playlist("Mix", config)._write_m3u([info], {}, {})

    with open(m3u_path, encoding="utf-8") as f:
        assert f.read() == good_contents


# --- helpers -----------------------------------------------------------------


def test_singles_folder_without_add_singles_to_folder(tmp_path):
    config = _make_config(tmp_path, add_singles_to_folder=False)
    album_meta = MagicMock()

    # Goes straight into the downloads root; album metadata is not consulted.
    assert singles_folder(config, "qobuz", album_meta) == str(tmp_path)
    album_meta.format_folder_path.assert_not_called()


def test_singles_folder_with_add_singles_and_source_subdirs(tmp_path):
    config = _make_config(
        tmp_path, add_singles_to_folder=True, source_subdirectories=True
    )
    album_meta = MagicMock()
    album_meta.format_folder_path.return_value = "Artist - Album"

    assert singles_folder(config, "qobuz", album_meta) == os.path.join(
        str(tmp_path), "Qobuz", "Artist - Album"
    )


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
