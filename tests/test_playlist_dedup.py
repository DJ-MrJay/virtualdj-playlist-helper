import csv
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vdj_relocator import RunOptions, build_arg_parser, playlist_path_from_args, restore_latest_backup, run_relocator


class PlaylistDedupTests(unittest.TestCase):
    def make_options(self, root: Path, apply: bool = True) -> RunOptions:
        return RunOptions(
            vdj_root=root / "VirtualDJ",
            playlist_path=None,
            scan_roots=[root / "Music"],
            include_database=False,
            apply=apply,
            search_mode="scan",
            report_dir=root / "reports",
            backup_dir=root / "backups",
            state_dir=root / "state",
            max_everything_results=1000000,
            resume_scan=False,
            dedupe_exact_candidates=True,
            dedupe_playlist_entries=True,
            prefer_scan_root_order=True,
            ignore_file_extension=False,
            allow_whitespace_filename_fallback=True,
        )

    def test_playlist_dedupe_is_scoped_to_each_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist_dir = root / "VirtualDJ" / "Playlists"
            music_dir = root / "Music"
            playlist_dir.mkdir(parents=True)
            music_dir.mkdir()
            song = music_dir / "Song.mp3"
            other = music_dir / "Other.mp3"
            song.write_bytes(b"song")
            other.write_bytes(b"other")

            playlist_a = playlist_dir / "Playlist A.m3u"
            playlist_b = playlist_dir / "Playlist B.m3u"
            playlist_a.write_text(
                "\r\n".join(
                    [
                        "#EXTM3U",
                        "#EXTINF:-1,Song",
                        "#EXTVDJ:<filesize>4</filesize>",
                        str(song),
                        "#EXTINF:-1,Song duplicate",
                        "#EXTVDJ:<filesize>4</filesize>",
                        str(song),
                        "#EXTINF:-1,Other",
                        str(other),
                        "",
                    ]
                ),
                encoding="ascii",
            )
            playlist_b.write_text(
                "\r\n".join(
                    [
                        "#EXTM3U",
                        "#EXTINF:-1,Song",
                        "#EXTVDJ:<filesize>4</filesize>",
                        str(song),
                        "",
                    ]
                ),
                encoding="ascii",
            )

            result = run_relocator(self.make_options(root))

            self.assertEqual(result.total_missing, 0)
            self.assertEqual(result.playlist_duplicates, 1)
            self.assertEqual(result.sources_changed, 1)
            self.assertEqual(playlist_a.read_text(encoding="ascii").count(str(song)), 1)
            self.assertEqual(playlist_b.read_text(encoding="ascii").count(str(song)), 1)

            with result.report_path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            duplicate_rows = [row for row in rows if row["match_status"] == "duplicate_playlist_entry"]
            self.assertEqual(len(duplicate_rows), 1)
            self.assertEqual(Path(duplicate_rows[0]["source_path"]).name, "Playlist A.m3u")

    def test_duplicate_after_relocation_updates_first_and_removes_second(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist_dir = root / "VirtualDJ" / "Playlists"
            music_dir = root / "Music"
            playlist_dir.mkdir(parents=True)
            music_dir.mkdir()
            song = music_dir / "Song.mp3"
            song.write_bytes(b"song")

            playlist = playlist_dir / "Relocate.m3u"
            playlist.write_text(
                "\r\n".join(
                    [
                        "#EXTM3U",
                        "#EXTINF:-1,Song old A",
                        "#EXTVDJ:<filesize>4</filesize>",
                        r"Z:\OldA\Song .mp3",
                        "#EXTINF:-1,Song old B",
                        "#EXTVDJ:<filesize>4</filesize>",
                        r"Y:\OldB\Song .mp3",
                        "",
                    ]
                ),
                encoding="ascii",
            )

            result = run_relocator(self.make_options(root))
            content = playlist.read_text(encoding="ascii")

            self.assertEqual(result.total_missing, 2)
            self.assertEqual(result.updated, 1)
            self.assertEqual(result.playlist_duplicates, 1)
            self.assertEqual(content.count(str(song)), 1)
            self.assertNotIn(r"Z:\OldA\Song .mp3", content)
            self.assertNotIn(r"Y:\OldB\Song .mp3", content)

    def test_byte_identical_different_locations_are_deduped_per_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist_dir = root / "VirtualDJ" / "Playlists"
            music_a = root / "Music" / "A"
            music_b = root / "Music" / "B"
            playlist_dir.mkdir(parents=True)
            music_a.mkdir(parents=True)
            music_b.mkdir(parents=True)
            song_a = music_a / "Song.mp3"
            song_b = music_b / "Song.mp3"
            song_a.write_bytes(b"same audio")
            song_b.write_bytes(b"same audio")

            playlist_a = playlist_dir / "Playlist A.m3u"
            playlist_b = playlist_dir / "Playlist B.m3u"
            playlist_a.write_text(
                "\r\n".join(["#EXTM3U", str(song_a), str(song_b), ""]),
                encoding="ascii",
            )
            playlist_b.write_text(
                "\r\n".join(["#EXTM3U", str(song_b), ""]),
                encoding="ascii",
            )

            result = run_relocator(self.make_options(root))
            content_a = playlist_a.read_text(encoding="ascii")
            content_b = playlist_b.read_text(encoding="ascii")

            self.assertEqual(result.total_missing, 0)
            self.assertEqual(result.playlist_duplicates, 1)
            self.assertIn(str(song_a), content_a)
            self.assertNotIn(str(song_b), content_a)
            self.assertIn(str(song_b), content_b)

    def test_single_playlist_option_limits_processing_to_selected_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist_dir = root / "VirtualDJ" / "Playlists"
            music_dir = root / "Music"
            playlist_dir.mkdir(parents=True)
            music_dir.mkdir()
            song = music_dir / "Song.mp3"
            song.write_bytes(b"song")

            selected_playlist = playlist_dir / "Selected.m3u"
            other_playlist = playlist_dir / "Other.m3u"
            selected_playlist.write_text(
                "\r\n".join(["#EXTM3U", str(song), str(song), ""]),
                encoding="ascii",
            )
            other_playlist.write_text(
                "\r\n".join(["#EXTM3U", str(song), str(song), ""]),
                encoding="ascii",
            )

            options = self.make_options(root)
            options.playlist_path = selected_playlist
            options.scan_roots = []
            result = run_relocator(options)

            self.assertEqual(result.playlist_duplicates, 1)
            self.assertEqual(result.sources_changed, 1)
            self.assertEqual(selected_playlist.read_text(encoding="ascii").count(str(song)), 1)
            self.assertEqual(other_playlist.read_text(encoding="ascii").count(str(song)), 2)

    def test_dropped_playlist_argument_is_used_as_single_playlist(self) -> None:
        parser = build_arg_parser()
        dropped_playlist = Path(r"C:\Users\Mr. Jay\Documents\VirtualDJ\Playlists\Party.m3u")
        args = parser.parse_args(["--gui", str(dropped_playlist)])

        self.assertEqual(playlist_path_from_args(args), dropped_playlist)

    def test_undo_restores_latest_backup_and_saves_safety_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist_dir = root / "VirtualDJ" / "Playlists"
            music_dir = root / "Music"
            playlist_dir.mkdir(parents=True)
            music_dir.mkdir()
            song = music_dir / "Song.mp3"
            song.write_bytes(b"song")

            playlist = playlist_dir / "Relocate.m3u"
            playlist.write_text(
                "\r\n".join(["#EXTM3U", r"Z:\Missing\Song.mp3", ""]),
                encoding="ascii",
            )

            original_text = playlist.read_text(encoding="ascii")

            options = self.make_options(root)
            result = run_relocator(options)
            self.assertIsNotNone(result.backup_path)
            self.assertEqual(result.updated, 1)

            playlist.write_text("changed state", encoding="ascii")

            self.assertTrue(restore_latest_backup(options))
            self.assertEqual(playlist.read_text(encoding="ascii"), original_text)
            safety_path = result.backup_path / "undo-safety" / "Playlists" / "Relocate.m3u"
            self.assertTrue(safety_path.exists())
            self.assertEqual(safety_path.read_text(encoding="ascii"), "changed state")

    def test_restore_falls_back_to_legacy_backups_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vdj_root = root / "VirtualDJ"
            backup_dir = root / "backups"
            playlist_dir = vdj_root / "Playlists"
            playlist_dir.mkdir(parents=True)
            backup_root = backup_dir / "vdj-relocator-backup-legacy"
            backup_root.mkdir(parents=True)

            playlist = playlist_dir / "Legacy.m3u"
            original_text = "#EXTM3U\n\nLegacy\n"
            playlist.write_text(original_text, encoding="ascii")
            backup_playlist = backup_root / "Playlists" / "Legacy.m3u"
            backup_playlist.parent.mkdir(parents=True)
            backup_playlist.write_text(original_text, encoding="ascii")

            options = self.make_options(root)
            options.backup_dir = backup_dir
            options.vdj_root = vdj_root

            playlist.write_text("changed", encoding="ascii")
            self.assertTrue(restore_latest_backup(options))
            self.assertEqual(playlist.read_text(encoding="ascii"), original_text)

    def test_scan_root_priority_resolves_no_size_ambiguous_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist_dir = root / "VirtualDJ" / "Playlists"
            music_a = root / "MusicA"
            music_b = root / "MusicB"
            playlist_dir.mkdir(parents=True)
            music_a.mkdir()
            music_b.mkdir()
            preferred = music_a / "Song.mp3"
            fallback = music_b / "Song.mp3"
            preferred.write_bytes(b"preferred")
            fallback.write_bytes(b"different fallback")
            playlist = playlist_dir / "No Size.m3u"
            playlist.write_text(
                "\r\n".join(["#EXTM3U", r"Z:\Missing\Song.mp3", ""]),
                encoding="ascii",
            )

            options = self.make_options(root, apply=False)
            options.scan_roots = [music_a, music_b]
            result = run_relocator(options)

            self.assertEqual(result.would_update, 1)
            with result.report_path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["new_path"], str(preferred))
            self.assertEqual(rows[0]["match_status"], "matched_by_exact_filename_and_scan_root_priority_no_size")

    def test_ignore_extension_matches_same_stem_audio_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist_dir = root / "VirtualDJ" / "Playlists"
            music_dir = root / "Music"
            playlist_dir.mkdir(parents=True)
            music_dir.mkdir()
            replacement = music_dir / "Adriana Evans - Lucky Dayz.m4a"
            replacement.write_bytes(b"m4a replacement")
            playlist = playlist_dir / "Ignore Extension.m3u"
            playlist.write_text(
                "\r\n".join(["#EXTM3U", r"Z:\Missing\Adriana Evans - Lucky Dayz.mp3", ""]),
                encoding="ascii",
            )

            options = self.make_options(root, apply=False)
            options.ignore_file_extension = True
            result = run_relocator(options)

            self.assertEqual(result.would_update, 1)
            with result.report_path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["new_path"], str(replacement))
            self.assertEqual(rows[0]["match_status"], "matched_by_filename_without_extension")

    def test_ignore_extension_can_resolve_stored_size_mismatch_when_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            playlist_dir = root / "VirtualDJ" / "Playlists"
            music_dir = root / "Music"
            playlist_dir.mkdir(parents=True)
            music_dir.mkdir()
            replacement = music_dir / "Adriana Evans - Lucky Dayz.m4a"
            replacement.write_bytes(b"m4a replacement")
            playlist = playlist_dir / "Ignore Extension Size.m3u"
            playlist.write_text(
                "\r\n".join(
                    [
                        "#EXTM3U",
                        "#EXTVDJ:<filesize>999</filesize>",
                        r"Z:\Missing\Adriana Evans - Lucky Dayz.mp3",
                        "",
                    ]
                ),
                encoding="ascii",
            )

            options = self.make_options(root, apply=False)
            options.ignore_file_extension = True
            result = run_relocator(options)

            self.assertEqual(result.would_update, 1)
            with result.report_path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["new_path"], str(replacement))
            self.assertEqual(rows[0]["match_status"], "matched_by_filename_without_extension_size_mismatch")


if __name__ == "__main__":
    unittest.main()
