# VirtualDJ Playlist Helper

VirtualDJ can mark songs as missing when music files have been moved to a new drive or folder. The built-in **Relocate missing file** action works, but it opens Explorer and requires you to find each file manually. That becomes slow when hundreds or thousands of playlist entries point to old paths.

This helper scans your VirtualDJ playlists, virtual folders, and database for missing file paths, searches folders you choose for safe filename matches, and updates VirtualDJ paths automatically when the match is clear.

The default mode is a dry run. Nothing is edited unless you explicitly apply fixes.

The helper does not read or match audio tag metadata. Matching is based on missing file paths, filenames, file sizes when available, optional whitespace repair, optional byte-identical candidate checks, and optional duplicate cleanup inside each playlist.

## What It Supports

- VirtualDJ playlists: `Playlists\*.m3u` and `Playlists\*.m3u8`
- Single-playlist mode by browsing for a file or dropping one onto the launcher
- VirtualDJ virtual folders: `Folders\**\*.vdjfolder`
- VirtualDJ database: `database.xml`
- Exact filename matching
- Optional filename matching without requiring the same extension
- Whitespace-normalized filename fallback for messy VirtualDJ paths
- File-size verification when VirtualDJ has a stored file size
- Scan-folder-priority fallback when no stored file size exists
- CSV reports for review
- Backups before any edit
- Stop and resume for interrupted direct folder scans
- Byte-identical duplicate candidate resolution
- Duplicate entry removal inside each individual playlist
- Optional Everything `es.exe` support for fast indexed searching
- Direct folder scanning fallback when Everything CLI is unavailable

## Requirements

- Windows
- VirtualDJ installed
- Python 3.10 or newer
- Your VirtualDJ user data folder, usually:

```text
C:\Users\<you>\Documents\VirtualDJ
```

Optional but recommended:

- Everything by Voidtools
- Everything command-line interface, `es.exe`

`Everything.exe` is the normal graphical app. `es.exe` is the command-line tool this helper can call to search Everything's index.

## Important Safety Notes

Close VirtualDJ before applying fixes. VirtualDJ may overwrite playlist or database files while it is running.

The helper is conservative:

- If a file size is available, it only auto-fixes when filename and size both match.
- If no file size is available, it only auto-fixes when exactly one filename match exists.
- If no file size is available and multiple filename matches exist, it can use the scan folder order you selected when the highest-priority scan folder has exactly one candidate.
- Ambiguous relocation candidates are skipped unless they are byte-identical and candidate deduplication is enabled.
- Size mismatches are skipped, except for the whitespace parent-folder repair case described below.
- Missing files that cannot be found are skipped.

When duplicate relocation candidates are byte-identical, the helper can resolve the reference by selecting one canonical path. It does not delete audio files.

When duplicate song entries appear inside the same playlist, the helper can remove later duplicate rows and keep the first occurrence. This cleanup is playlist-local: the same song can still appear in different playlists. It removes rows when the final path is the same, or when different existing paths have the same filename, same size, and same SHA-256 content hash.

When filename spacing differs, for example `Artist - Song .mp3` versus `Artist - Song.mp3`, the helper can use a whitespace-normalized fallback. If multiple candidates are found, it only selects one when file size or the missing path's parent folder isolates a single candidate.

When the extension differs, for example `Adriana Evans - Lucky Dayz.mp3` versus `Adriana Evans - Lucky Dayz.m4a`, the helper can use an opt-in extensionless fallback. This only compares the filename before the extension; it does not read audio tags.

The helper does not use ID3, FLAC, or other audio metadata tags to guess matches.

Apply mode creates backups before editing anything.

Undo restores the latest apply backup and creates a safety copy of the current file before overwriting it. New backups include a manifest so undo can restore them directly; older backups without a manifest still fall back to a directory-based restore path.

To restore the latest backup from the command line:

```bat
python vdj_relocator.py --undo
```

The same action is available through the script entry point when you need to revert the most recent apply run.

## Installation

1. Download or clone this repository.

2. Install Python from:

```text
https://www.python.org/downloads/windows/
```

During Python installation, enable:

```text
Add python.exe to PATH
```

3. Optional: install Everything.

Download Everything from:

```text
https://www.voidtools.com/
```

4. Optional: install `es.exe`.

Download the Everything Command-line Interface from:

```text
https://www.voidtools.com/downloads/
```

Extract `es.exe` to a folder in your `PATH`, or place it in:

```text
C:\Program Files\Everything
```

Test it in PowerShell:

```powershell
where.exe es.exe
Get-Command es.exe
es.exe -n 5 -full-path-and-name "test.mp3"
```

If `es.exe` is installed, this helper uses it automatically in `auto` search mode. If it is not installed, the helper scans the folders you choose directly.

## Quick Start With The GUI

1. Close VirtualDJ.

2. Double-click:

```bat
Run-VDJ-Relocator.bat
```

3. Confirm the **VirtualDJ folder**.

Use your VirtualDJ user data folder, not the application install folder.

Usually:

```text
C:\Users\<you>\Documents\VirtualDJ
```

Do not choose:

```text
C:\Program Files\VirtualDJ
```

4. Optional: choose a **Single playlist**.

Use **Browse** to select one `.m3u` or `.m3u8` file with Explorer. When a single playlist is selected, the helper processes only that playlist instead of all playlists in your VirtualDJ folder.

You can also drop one `.m3u` or `.m3u8` file onto `Run-VDJ-Relocator.bat`. The GUI opens with that playlist preloaded.

5. Add one or more **Scan folders**.

Choose folders where your relocated music files now exist, for example:

```text
D:\Music
E:\Music
F:\DJ Library
```

These folders also act as a safety filter when using Everything, so the helper does not relink to random duplicate files elsewhere on your computer.

Scan folder order matters. If VirtualDJ did not store a file size and the same filename exists in multiple scan folders, **Prefer scan folder order** lets the helper choose the single candidate from the earliest scan folder in your list.

If you only want to remove duplicates from a selected playlist and do not need missing-file relocation, scan folders are optional.

6. Leave **Include database.xml** checked unless you only want to repair playlists and virtual folders.

When a single playlist is selected, `database.xml` and VirtualDJ virtual folders are ignored for that run.

7. Leave **Resume interrupted scan** checked unless you want every run to start fresh.

8. Leave **Resolve byte-identical candidates** checked if you want byte-identical duplicate file candidates to resolve to one canonical path.

The helper verifies duplicate candidates by file size and SHA-256 content hash before choosing one. It prefers paths from the scan folders in the order you selected them.

9. Leave **Remove playlist duplicates** checked if you want duplicate song rows removed inside each playlist.

This only removes duplicate rows within the same `.m3u` or `.m3u8` playlist. It keeps the first occurrence in that playlist. The same song can remain in other playlists. Different existing paths are only treated as duplicates when they have the same filename, same size, and same SHA-256 content hash.

10. Leave **Repair whitespace variants** checked if you want the helper to repair filenames that only differ by extra or missing spaces.

Example:

```text
Missing VirtualDJ filename:
A Few Good Men - Walk You Thru .mp3

Actual filename:
A Few Good Men - Walk You Thru.mp3
```

If multiple whitespace-normalized candidates exist, the helper uses file size first. If file size is unavailable or stale, it may use parent-folder context only when exactly one candidate is in the same immediate parent folder.

Leave **Prefer scan folder order** checked if you want the helper to resolve no-filesize duplicate filename matches using the order of your scan folders.

Enable **Ignore file extension** if you want files with the same name but different audio extensions to be treated as candidates.

Example:

```text
Missing VirtualDJ filename:
Adriana Evans - Lucky Dayz.mp3

Actual filename:
Adriana Evans - Lucky Dayz.m4a
```

11. Choose **Search mode**:

- `auto`: use `es.exe` first, then direct folder scan if needed
- `everything`: require `es.exe`
- `scan`: direct folder scan only

12. Click **Dry Run**.

This writes a CSV report and makes no changes.

13. Review the newest CSV in:

```text
reports
```

Rows with this value are the fixes that would be applied:

```text
action = would_update
```

Rows with this value used byte-identical candidate resolution:

```text
match_status = deduped_exact_duplicate
```

Rows with this value remove duplicate rows from a playlist:

```text
match_status = duplicate_playlist_entry
```

Rows with these values used whitespace-normalized filename repair:

```text
match_status = matched_by_normalized_filename
match_status = matched_by_normalized_filename_and_size
match_status = matched_by_normalized_filename_and_parent
match_status = matched_by_normalized_filename_and_parent_size_mismatch
```

Rows with these values used scan folder order because VirtualDJ had no stored file size:

```text
match_status = matched_by_exact_filename_and_scan_root_priority_no_size
match_status = matched_by_normalized_filename_and_scan_root_priority_no_size
```

Rows with these values used extensionless matching:

```text
match_status = matched_by_filename_without_extension
match_status = matched_by_filename_without_extension_and_size
match_status = matched_by_filename_without_extension_size_mismatch
match_status = matched_by_filename_without_extension_and_parent
match_status = matched_by_filename_without_extension_and_parent_size_mismatch
match_status = matched_by_filename_without_extension_and_scan_root_priority_no_size
match_status = matched_by_filename_without_extension_and_scan_root_priority_size_mismatch
match_status = matched_by_normalized_filename_without_extension
match_status = matched_by_normalized_filename_without_extension_and_size
match_status = matched_by_normalized_filename_without_extension_size_mismatch
match_status = matched_by_normalized_filename_without_extension_and_parent
match_status = matched_by_normalized_filename_without_extension_and_parent_size_mismatch
match_status = matched_by_normalized_filename_without_extension_and_scan_root_priority_no_size
match_status = matched_by_normalized_filename_without_extension_and_scan_root_priority_size_mismatch
```

14. If the report looks correct, make sure VirtualDJ is closed, then click **Apply Fixes**.

15. Reopen VirtualDJ and check the repaired playlists.

## Command-Line Usage

Dry run:

```powershell
python .\vdj_relocator.py --no-gui --scan-root "D:\Music" --scan-root "E:\Music"
```

Apply confirmed fixes:

```powershell
python .\vdj_relocator.py --no-gui --apply --scan-root "D:\Music" --scan-root "E:\Music"
```

Force Everything CLI:

```powershell
python .\vdj_relocator.py --no-gui --search-mode everything --scan-root "D:\Music"
```

Force direct folder scanning:

```powershell
python .\vdj_relocator.py --no-gui --search-mode scan --scan-root "D:\Music"
```

Skip `database.xml` and only process playlists plus virtual folders:

```powershell
python .\vdj_relocator.py --no-gui --no-database --scan-root "D:\Music"
```

Process only one playlist:

```powershell
python .\vdj_relocator.py --no-gui --playlist "C:\Users\<you>\Documents\VirtualDJ\Playlists\Party.m3u" --scan-root "D:\Music"
```

Remove duplicates from one playlist without relocating missing files:

```powershell
python .\vdj_relocator.py --no-gui --playlist "C:\Users\<you>\Documents\VirtualDJ\Playlists\Party.m3u"
```

Ignore interrupted-scan checkpoints and start fresh:

```powershell
python .\vdj_relocator.py --no-gui --no-resume --scan-root "D:\Music"
```

Disable byte-identical candidate resolution:

```powershell
python .\vdj_relocator.py --no-gui --no-dedupe-exact --scan-root "D:\Music"
```

Disable duplicate row cleanup inside playlists:

```powershell
python .\vdj_relocator.py --no-gui --no-playlist-dedupe --scan-root "D:\Music"
```

Disable scan-folder-priority matching for entries without a stored file size:

```powershell
python .\vdj_relocator.py --no-gui --no-scan-root-priority --scan-root "D:\Music"
```

Enable extensionless matching:

```powershell
python .\vdj_relocator.py --no-gui --ignore-extension --scan-root "D:\Music"
```

Disable whitespace-normalized filename fallback:

```powershell
python .\vdj_relocator.py --no-gui --no-whitespace-fallback --scan-root "D:\Music"
```

## Reports

Reports are written to:

```text
reports\vdj-relocator-report-YYYYMMDD-HHMMSS.csv
```

Useful columns:

- `action`: `would_update`, `updated`, `would_remove_duplicate`, `removed_duplicate`, or `skipped`
- `match_status`: how the helper classified the match
- `reason`: why the file was updated or skipped
- `old_path`: the missing VirtualDJ path
- `new_path`: the replacement path, when one was selected
- `candidate_count`: number of filename matches found
- `candidates`: possible matches, for review

## Backups

Apply mode creates backups before editing files:

```text
backups\vdj-relocator-backup-YYYYMMDD-HHMMSS
```

Backups preserve the VirtualDJ folder structure under that backup directory.

## Stop And Resume

Click **Stop** to cancel a running dry run or apply run.

The helper stops at the next safe point. If **Resume interrupted scan** is enabled, direct folder scan progress is saved to:

```text
state\vdj-relocator-scan-checkpoint.json
```

The next run with the same scan folders, search mode, and missing filename set will reuse the checkpoint instead of starting the scan from scratch.

A successful run clears the checkpoint.

## How Matching Works

The helper first finds references where the path stored by VirtualDJ no longer exists. It then searches the selected scan folders for files with the same filename and extension.

Examples:

```text
Old missing path:
G:\Old Music\Aaliyah - Try Again.mp3

Candidate:
D:\Music\Aaliyah - Try Again.mp3
```

If VirtualDJ stored a file size, the helper checks the candidate size too. If there are multiple possible matches or the size does not match, the entry is skipped and listed in the report.

If VirtualDJ did not store a file size and multiple filename candidates exist, **Prefer scan folder order** can select one. It only does this when the earliest scan folder containing matches has exactly one candidate. This is meant for cases where you choose scan folders in trusted priority order, such as `D:\Music` before `E:\Afro`.

If **Ignore file extension** is enabled, the helper also searches common audio extensions for files with the same filename stem. A missing `.mp3` can therefore match an existing `.m4a`, `.flac`, `.wav`, and other common audio formats. If VirtualDJ stored a file size and the only extensionless candidate differs in size, the helper can still mark it for update, but the CSV reports that as `matched_by_filename_without_extension_size_mismatch`.

If **Resolve byte-identical candidates** is enabled and multiple relocation candidates have the same filename, same size, and same SHA-256 content hash, the helper picks one canonical path and uses that as the replacement. The canonical path is selected by scan folder order first, then shorter path, then alphabetical path.

This candidate dedupe only updates the VirtualDJ reference. It does not delete audio files from disk.

If **Remove playlist duplicates** is enabled, the helper then checks each playlist separately after planned path updates. It keeps the first row and removes later duplicate rows from that same playlist when the final path is the same, or when different existing paths have the same filename, same size, and same SHA-256 content hash. It does not compare one playlist against another playlist.

If **Repair whitespace variants** is enabled, the helper also compares filenames after collapsing repeated spaces and trimming spaces before the extension. This handles paths like `Song .mp3` when the actual file is `Song.mp3`. This is not a general fuzzy match: spelling, punctuation, filename text, and extension still need to match after whitespace normalization.

When whitespace-normalized matching finds multiple candidates and file-size verification does not isolate one, the helper may use parent-folder context. It only does this when exactly one candidate has the same immediate parent folder name as the missing VirtualDJ path.

## Troubleshooting

`es.exe` is not found:

- Install the Everything Command-line Interface.
- Make sure `es.exe` is in your `PATH` or in `C:\Program Files\Everything`.
- Test with `where.exe es.exe` or `Get-Command es.exe`.
- Or use `search-mode = scan`.

Everything returns no candidates:

- Make sure Everything is running.
- Make sure Everything has indexed the drive containing your music.
- Confirm your scan folder is broad enough.

The helper finds duplicates:

- Review the CSV rows with `match_status = ambiguous`.
- If they are byte-identical and **Resolve byte-identical candidates** is enabled, they should be reported as `deduped_exact_duplicate`.
- If they remain ambiguous, the files have different sizes or different content hashes.
- Move duplicates out of the selected scan roots, change scan folder priority, or repair those entries manually in VirtualDJ.

The helper removes playlist duplicates:

- Review the CSV rows with `match_status = duplicate_playlist_entry`.
- Duplicate playlist cleanup is per playlist only.
- The helper keeps the first occurrence in that playlist and removes later rows with the same final path or byte-identical same-filename files.
- The same song can still appear in other playlists.

Indexing is slow:

- Install `es.exe`; `Everything.exe` alone is not enough for command-line indexed searching.
- Use `search-mode = auto` or `search-mode = everything` after `es.exe` is installed.
- Select the narrowest scan folders that contain your relocated music.
- Direct scan filters by needed file extensions, but it still has to walk the selected folders.
- Everything search uses a bulk extension query per scan folder and filters filenames locally.

Whitespace variant was repaired but size differs:

- Review rows with `match_status = matched_by_normalized_filename_and_parent_size_mismatch`.
- The helper chose the candidate because the normalized filename and parent folder matched.
- This can happen when VirtualDJ's stored size came from an older copy or a differently encoded file.
- Review these rows before applying fixes.

The helper reports size mismatches:

- The filename exists, but the file is not byte-for-byte the same size VirtualDJ expected.
- This may be a different version, re-encoded file, remaster, edit, or duplicate.
- The helper skips these by design.

VirtualDJ still shows missing files after applying:

- Confirm VirtualDJ was closed during apply mode.
- Confirm the updated path exists.
- Run another dry run.
- Check whether the remaining rows are ambiguous, not found, or size mismatched.

## Project Files

- `vdj_relocator.py`: main helper
- `Run-VDJ-Relocator.bat`: Windows GUI launcher
- `reports`: generated CSV reports
- `backups`: generated backups before apply mode edits
- `state`: interrupted-scan checkpoints

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for the full text.
