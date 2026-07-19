# VirtualDJ Missing File Relocator

VirtualDJ can mark songs as missing when music files have been moved to a new drive or folder. The built-in **Relocate missing file** action works, but it opens Explorer and requires you to find each file manually. That becomes slow when hundreds or thousands of playlist entries point to old paths.

This helper scans your VirtualDJ playlists, virtual folders, and database for missing file paths, searches folders you choose for safe filename matches, and updates VirtualDJ paths automatically when the match is clear.

The default mode is a dry run. Nothing is edited unless you explicitly apply fixes.

The helper does not read or match audio tag metadata. Matching is based on missing file paths, filenames, file sizes when available, optional whitespace repair, and optional byte-identical duplicate checks.

## What It Supports

- VirtualDJ playlists: `Playlists\*.m3u` and `Playlists\*.m3u8`
- VirtualDJ virtual folders: `Folders\**\*.vdjfolder`
- VirtualDJ database: `database.xml`
- Exact filename matching
- Whitespace-normalized filename fallback for messy VirtualDJ paths
- File-size verification when VirtualDJ has a stored file size
- CSV reports for review
- Backups before any edit
- Stop and resume for interrupted direct folder scans
- Exact duplicate candidate deduplication
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
- Ambiguous duplicates are skipped unless they are byte-identical and deduplication is enabled.
- Size mismatches are skipped, except for the whitespace parent-folder repair case described below.
- Missing files that cannot be found are skipped.

When duplicate candidates are byte-identical, the helper can deduplicate the reference by selecting one canonical path. It does not delete audio files.

When filename spacing differs, for example `Artist - Song .mp3` versus `Artist - Song.mp3`, the helper can use a whitespace-normalized fallback. If multiple candidates are found, it only selects one when file size or the missing path's parent folder isolates a single candidate.

The helper does not use ID3, FLAC, or other audio metadata tags to guess matches.

Apply mode creates backups before editing anything.

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
where es
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

4. Add one or more **Scan folders**.

Choose folders where your relocated music files now exist, for example:

```text
D:\Music
E:\Music
F:\DJ Library
```

These folders also act as a safety filter when using Everything, so the helper does not relink to random duplicate files elsewhere on your computer.

5. Leave **Include database.xml** checked unless you only want to repair playlists and virtual folders.

6. Leave **Resume interrupted scan** checked unless you want every run to start fresh.

7. Leave **Deduplicate exact matches** checked if you want byte-identical duplicate candidates to resolve to one canonical path.

The helper verifies duplicate candidates by file size and SHA-256 content hash before choosing one. It prefers paths from the scan folders in the order you selected them.

8. Leave **Repair whitespace variants** checked if you want the helper to repair filenames that only differ by extra or missing spaces.

Example:

```text
Missing VirtualDJ filename:
A Few Good Men - Walk You Thru .mp3

Actual filename:
A Few Good Men - Walk You Thru.mp3
```

If multiple whitespace-normalized candidates exist, the helper uses file size first. If file size is unavailable or stale, it may use parent-folder context only when exactly one candidate is in the same immediate parent folder.

9. Choose **Search mode**:

- `auto`: use `es.exe` first, then direct folder scan if needed
- `everything`: require `es.exe`
- `scan`: direct folder scan only

10. Click **Dry Run**.

This writes a CSV report and makes no changes.

11. Review the newest CSV in:

```text
reports
```

Rows with this value are the fixes that would be applied:

```text
action = would_update
```

Rows with this value used duplicate candidate deduplication:

```text
match_status = deduped_exact_duplicate
```

Rows with these values used whitespace-normalized filename repair:

```text
match_status = matched_by_normalized_filename
match_status = matched_by_normalized_filename_and_size
match_status = matched_by_normalized_filename_and_parent
match_status = matched_by_normalized_filename_and_parent_size_mismatch
```

12. If the report looks correct, make sure VirtualDJ is closed, then click **Apply Fixes**.

13. Reopen VirtualDJ and check the repaired playlists.

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

Ignore interrupted-scan checkpoints and start fresh:

```powershell
python .\vdj_relocator.py --no-gui --no-resume --scan-root "D:\Music"
```

Disable exact duplicate candidate deduplication:

```powershell
python .\vdj_relocator.py --no-gui --no-dedupe-exact --scan-root "D:\Music"
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

- `action`: `would_update`, `updated`, or `skipped`
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

If **Deduplicate exact matches** is enabled and multiple candidates have the same filename, same size, and same SHA-256 content hash, the helper picks one canonical path and uses that as the replacement. The canonical path is selected by scan folder order first, then shorter path, then alphabetical path.

This only updates the VirtualDJ reference. It does not remove rows from playlists and it does not delete duplicate files from disk.

If **Repair whitespace variants** is enabled, the helper also compares filenames after collapsing repeated spaces and trimming spaces before the extension. This handles paths like `Song .mp3` when the actual file is `Song.mp3`. This is not a general fuzzy match: spelling, punctuation, filename text, and extension still need to match after whitespace normalization.

When whitespace-normalized matching finds multiple candidates and file-size verification does not isolate one, the helper may use parent-folder context. It only does this when exactly one candidate has the same immediate parent folder name as the missing VirtualDJ path.

## Troubleshooting

`es.exe` is not found:

- Install the Everything Command-line Interface.
- Make sure `es.exe` is in your `PATH` or in `C:\Program Files\Everything`.
- Test with `where es`.
- Or use `search-mode = scan`.

Everything returns no candidates:

- Make sure Everything is running.
- Make sure Everything has indexed the drive containing your music.
- Confirm your scan folder is broad enough.

The helper finds duplicates:

- Review the CSV rows with `match_status = ambiguous`.
- If they are byte-identical and **Deduplicate exact matches** is enabled, they should be reported as `deduped_exact_duplicate`.
- If they remain ambiguous, the files have different sizes or different content hashes.
- Move duplicates out of the selected scan roots, change scan folder priority, or repair those entries manually in VirtualDJ.

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

Add your preferred license before publishing this repository publicly.
