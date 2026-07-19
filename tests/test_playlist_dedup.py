import csv
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vdj_relocator import RunOptions, run_relocator


class PlaylistDedupTests(unittest.TestCase):
    def make_options(self, root: Path, apply: bool = True) -> RunOptions:
        return RunOptions(
            vdj_root=root / "VirtualDJ",
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


if __name__ == "__main__":
    unittest.main()
