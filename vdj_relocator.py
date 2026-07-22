#!/usr/bin/env python3
"""
VirtualDJ missing-file relinker.

Scans VirtualDJ playlists, virtual folders, and optionally database.xml for
missing file paths, searches user-selected folders for safe filename matches,
and writes a review report. Files are only edited when --apply is supplied.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import dataclasses
import datetime as dt
import hashlib
import html
import json
import ntpath
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
from typing import Callable


DEFAULT_VDJ_ROOT = Path.home() / "Documents" / "VirtualDJ"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPORT_DIR = SCRIPT_DIR / "reports"
DEFAULT_BACKUP_DIR = SCRIPT_DIR / "backups"
DEFAULT_STATE_DIR = SCRIPT_DIR / "state"

EXTVDJ_FILESIZE_RE = re.compile(r"<filesize>(\d+)</filesize>", re.IGNORECASE)
ATTR_RE_TEMPLATE = r"\b{0}\s*=\s*(['\"])(.*?)\1"
XML_TAG_RE = re.compile(r"<(?![!?/])[^>]+>", re.DOTALL)
WHITESPACE_RE = re.compile(r"\s+")
PATH_ATTRS = ("FilePath", "filepath", "File", "file", "Path", "path")
SIZE_ATTRS = ("FileSize", "filesize", "Size", "size")
AUDIO_FILE_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".alac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}


@dataclasses.dataclass
class SourceFile:
    path: Path
    kind: str
    encoding: str
    text: str


@dataclasses.dataclass
class MissingEntry:
    entry_id: int
    source_path: Path
    source_kind: str
    reference: str
    old_path: str
    filename: str
    expected_size: int | None
    edit_start: int
    edit_end: int
    edit_format: str
    candidate_count: int = 0
    candidates: list[str] = dataclasses.field(default_factory=list)
    new_path: str = ""
    match_status: str = "not_checked"
    action: str = "skipped"
    reason: str = ""


@dataclasses.dataclass
class PlaylistRecord:
    source_path: Path
    reference: str
    path_value: str
    filename: str
    expected_size: int | None
    record_start: int
    record_end: int
    path_start: int
    path_end: int


@dataclasses.dataclass
class PlaylistDuplicate:
    duplicate_id: int
    source_path: Path
    reference: str
    old_path: str
    final_path: str
    kept_path: str
    filename: str
    edit_start: int
    edit_end: int
    action: str = "would_remove_duplicate"
    match_status: str = "duplicate_playlist_entry"
    reason: str = "Duplicate playlist entry in this playlist; keeping the first occurrence."


@dataclasses.dataclass
class RunOptions:
    vdj_root: Path
    playlist_path: Path | None
    scan_roots: list[Path]
    include_database: bool
    apply: bool
    search_mode: str
    report_dir: Path
    backup_dir: Path
    state_dir: Path
    max_everything_results: int
    resume_scan: bool
    dedupe_exact_candidates: bool
    dedupe_playlist_entries: bool
    prefer_scan_root_order: bool
    ignore_file_extension: bool
    allow_whitespace_filename_fallback: bool


@dataclasses.dataclass
class RunResult:
    report_path: Path | None
    backup_path: Path | None
    total_missing: int
    would_update: int
    updated: int
    ambiguous: int
    not_found: int
    deduped: int
    playlist_duplicates: int
    skipped: int
    sources_changed: int
    search_engine: str
    canceled: bool = False
    resumed: bool = False


@dataclasses.dataclass
class SearchCheckpoint:
    fingerprint: str
    search_engine: str
    index: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    completed_roots: set[str] = dataclasses.field(default_factory=set)
    pending_root: str = ""
    pending_stack: list[str] = dataclasses.field(default_factory=list)
    scanned_files: int = 0
    result_count: int = 0
    resumed: bool = False


class OperationCancelled(Exception):
    pass


def log_noop(message: str) -> None:
    _ = message


def check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled


def detect_text_encoding(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "cp1252"


def write_text(path: Path, text: str, encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="") as handle:
        handle.write(text)


def line_content_and_newline(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


def line_offsets(text: str) -> list[int]:
    offsets = [0]
    for match in re.finditer(r"\n", text):
        offsets.append(match.end())
    return offsets


def line_number_from_offset(offsets: list[int], offset: int) -> int:
    return bisect.bisect_right(offsets, offset)


def normalize_for_compare(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def is_url(value: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://", value, re.IGNORECASE))


def is_windows_absolute(path_value: str) -> bool:
    if path_value.startswith("\\\\"):
        return True
    drive, _ = ntpath.splitdrive(path_value)
    return bool(drive)


class PathExistenceCache:
    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], bool] = {}

    def exists(self, path_value: str, source_path: Path) -> bool:
        if not path_value or is_url(path_value):
            return True
        expanded = os.path.expandvars(path_value.strip().strip('"'))
        if is_windows_absolute(expanded):
            key = ("absolute", "", ntpath.normcase(expanded))
            check_path = Path(expanded)
        else:
            source_parent = normalize_for_compare(source_path.parent)
            key = ("relative", source_parent, expanded)
            check_path = source_path.parent / expanded
        if key not in self._cache:
            self._cache[key] = check_path.exists()
        return self._cache[key]


def filename_from_path(path_value: str) -> str:
    cleaned = path_value.strip().strip('"')
    return ntpath.basename(cleaned.replace("/", "\\"))


def normalize_filename_for_match(filename: str) -> str:
    stem, ext = ntpath.splitext(filename)
    stem = WHITESPACE_RE.sub(" ", stem).strip()
    ext = ext.strip()
    return f"{stem}{ext}".lower()


def filename_stem_for_match(filename: str) -> str:
    stem, _ext = ntpath.splitext(filename)
    return stem.strip().lower()


def normalize_filename_stem_for_match(filename: str) -> str:
    stem, _ext = ntpath.splitext(filename)
    return WHITESPACE_RE.sub(" ", stem).strip().lower()


def normalize_path_component_for_match(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value).strip().lower()


def parent_folder_name(path_value: str) -> str:
    cleaned = path_value.strip().strip('"').replace("/", "\\").rstrip("\\")
    return ntpath.basename(ntpath.dirname(cleaned))


def exact_filename_key(filename: str) -> str:
    return f"exact:{filename.lower()}"


def normalized_filename_key(filename: str) -> str:
    return f"normalized:{normalize_filename_for_match(filename)}"


def extensionless_filename_key(filename: str) -> str:
    return f"stem:{filename_stem_for_match(filename)}"


def normalized_extensionless_filename_key(filename: str) -> str:
    return f"normalized_stem:{normalize_filename_stem_for_match(filename)}"


def attr_match(tag: str, attr_name: str) -> re.Match[str] | None:
    return re.search(ATTR_RE_TEMPLATE.format(re.escape(attr_name)), tag, re.IGNORECASE | re.DOTALL)


def attr_value_span(tag_start: int, match: re.Match[str]) -> tuple[int, int]:
    return tag_start + match.start(2), tag_start + match.end(2)


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def load_source(path: Path, kind: str) -> SourceFile:
    text, encoding = detect_text_encoding(path)
    return SourceFile(path=path, kind=kind, encoding=encoding, text=text)


def is_supported_playlist(path: Path) -> bool:
    return path.suffix.lower() in {".m3u", ".m3u8"}


def discover_sources(
    vdj_root: Path,
    include_database: bool,
    playlist_path: Path | None,
    log: Callable[[str], None],
) -> list[SourceFile]:
    sources: list[SourceFile] = []
    if playlist_path is not None:
        if not playlist_path.exists():
            raise FileNotFoundError(f"Selected playlist does not exist: {playlist_path}")
        if not playlist_path.is_file() or not is_supported_playlist(playlist_path):
            raise ValueError(f"Selected file is not a supported playlist: {playlist_path}")
        sources.append(load_source(playlist_path, "playlist"))
        log(f"Loaded selected playlist: {playlist_path}")
        return sources

    playlists = vdj_root / "Playlists"
    if playlists.exists():
        for pattern in ("*.m3u", "*.m3u8"):
            for path in sorted(playlists.glob(pattern)):
                sources.append(load_source(path, "playlist"))
    folders = vdj_root / "Folders"
    if folders.exists():
        for path in sorted(folders.rglob("*.vdjfolder")):
            sources.append(load_source(path, "virtual_folder"))
    database = vdj_root / "database.xml"
    if include_database and database.exists():
        sources.append(load_source(database, "database"))
    log(f"Loaded {len(sources)} VirtualDJ source files.")
    return sources


def is_playlist_metadata_line(stripped: str) -> bool:
    upper = stripped.upper()
    return upper.startswith("#EXT") and upper != "#EXTM3U"


def parse_playlist_records(source: SourceFile) -> list[PlaylistRecord]:
    records: list[PlaylistRecord] = []
    offset = 0
    metadata_start: int | None = None
    pending_size: int | None = None
    for line_number, raw_line in enumerate(source.text.splitlines(keepends=True), start=1):
        content, _newline = line_content_and_newline(raw_line)
        stripped = content.strip()
        if is_playlist_metadata_line(stripped):
            if metadata_start is None:
                metadata_start = offset
            size_match = EXTVDJ_FILESIZE_RE.search(stripped)
            if size_match:
                pending_size = parse_int(size_match.group(1))
        elif stripped.startswith("#") or not stripped:
            pass
        else:
            path_value = content
            filename = filename_from_path(path_value)
            if filename:
                records.append(
                    PlaylistRecord(
                        source_path=source.path,
                        reference=f"line {line_number}",
                        path_value=path_value,
                        filename=filename,
                        expected_size=pending_size,
                        record_start=metadata_start if metadata_start is not None else offset,
                        record_end=offset + len(raw_line),
                        path_start=offset,
                        path_end=offset + len(content),
                    )
                )
            metadata_start = None
            pending_size = None
        offset += len(raw_line)
    return records


def parse_m3u(source: SourceFile, existence_cache: PathExistenceCache, start_id: int) -> list[MissingEntry]:
    entries: list[MissingEntry] = []
    entry_id = start_id
    for record in parse_playlist_records(source):
        if not existence_cache.exists(record.path_value, source.path):
            entries.append(
                MissingEntry(
                    entry_id=entry_id,
                    source_path=source.path,
                    source_kind=source.kind,
                    reference=record.reference,
                    old_path=record.path_value,
                    filename=record.filename,
                    expected_size=record.expected_size,
                    edit_start=record.path_start,
                    edit_end=record.path_end,
                    edit_format="plain",
                )
            )
            entry_id += 1
    return entries


def parse_xml_path_attrs(source: SourceFile, existence_cache: PathExistenceCache, start_id: int) -> list[MissingEntry]:
    entries: list[MissingEntry] = []
    offsets = line_offsets(source.text)
    entry_id = start_id
    for index, tag_match in enumerate(XML_TAG_RE.finditer(source.text), start=1):
        tag = tag_match.group(0)
        path_match: re.Match[str] | None = None
        for path_attr in PATH_ATTRS:
            path_match = attr_match(tag, path_attr)
            if path_match:
                break
        if not path_match:
            continue
        path_value = html.unescape(path_match.group(2))
        filename = filename_from_path(path_value)
        if not filename or existence_cache.exists(path_value, source.path):
            continue
        expected_size: int | None = None
        for size_attr in SIZE_ATTRS:
            size_match = attr_match(tag, size_attr)
            if size_match:
                expected_size = parse_int(size_match.group(2))
                break
        edit_start, edit_end = attr_value_span(tag_match.start(), path_match)
        line_number = line_number_from_offset(offsets, tag_match.start())
        entries.append(
            MissingEntry(
                entry_id=entry_id,
                source_path=source.path,
                source_kind=source.kind,
                reference=f"tag {index}, line {line_number}",
                old_path=path_value,
                filename=filename,
                expected_size=expected_size,
                edit_start=edit_start,
                edit_end=edit_end,
                edit_format="xml_attr",
            )
        )
        entry_id += 1
    return entries


def parse_sources(sources: list[SourceFile], log: Callable[[str], None]) -> list[MissingEntry]:
    existence_cache = PathExistenceCache()
    entries: list[MissingEntry] = []
    next_id = 1
    for source in sources:
        if source.kind == "playlist":
            parsed = parse_m3u(source, existence_cache, next_id)
        else:
            parsed = parse_xml_path_attrs(source, existence_cache, next_id)
        entries.extend(parsed)
        next_id += len(parsed)
    log(f"Found {len(entries)} missing VirtualDJ references.")
    return entries


def checkpoint_file(state_dir: Path) -> Path:
    return state_dir / "vdj-relocator-scan-checkpoint.json"


def checkpoint_to_json(checkpoint: SearchCheckpoint) -> dict[str, object]:
    return {
        "fingerprint": checkpoint.fingerprint,
        "search_engine": checkpoint.search_engine,
        "index": checkpoint.index,
        "completed_roots": sorted(checkpoint.completed_roots),
        "pending_root": checkpoint.pending_root,
        "pending_stack": checkpoint.pending_stack,
        "scanned_files": checkpoint.scanned_files,
        "result_count": checkpoint.result_count,
    }


def checkpoint_from_json(data: dict[str, object]) -> SearchCheckpoint:
    raw_index = data.get("index", {})
    index: dict[str, list[str]] = {}
    if isinstance(raw_index, dict):
        for key, value in raw_index.items():
            if isinstance(key, str) and isinstance(value, list):
                index[key] = [str(item) for item in value]
    raw_completed = data.get("completed_roots", [])
    completed = {str(item) for item in raw_completed} if isinstance(raw_completed, list) else set()
    raw_stack = data.get("pending_stack", [])
    stack = [str(item) for item in raw_stack] if isinstance(raw_stack, list) else []
    return SearchCheckpoint(
        fingerprint=str(data.get("fingerprint", "")),
        search_engine=str(data.get("search_engine", "")),
        index=index,
        completed_roots=completed,
        pending_root=str(data.get("pending_root", "")),
        pending_stack=stack,
        scanned_files=int(data.get("scanned_files", 0) or 0),
        result_count=int(data.get("result_count", 0) or 0),
        resumed=True,
    )


def save_checkpoint(state_dir: Path, checkpoint: SearchCheckpoint) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file(state_dir).write_text(json.dumps(checkpoint_to_json(checkpoint), indent=2), encoding="utf-8")


def load_checkpoint(state_dir: Path, fingerprint: str, search_engine: str) -> SearchCheckpoint | None:
    path = checkpoint_file(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    checkpoint = checkpoint_from_json(data)
    if checkpoint.fingerprint != fingerprint or checkpoint.search_engine != search_engine:
        return None
    return checkpoint


def clear_checkpoint(state_dir: Path) -> None:
    path = checkpoint_file(state_dir)
    if path.exists():
        path.unlink()


def wanted_filename_sets(
    entries: list[MissingEntry],
    allow_whitespace: bool,
    ignore_file_extension: bool,
) -> tuple[set[str], set[str], set[str], set[str]]:
    exact = {exact_filename_key(entry.filename) for entry in entries}
    normalized: set[str] = set()
    if allow_whitespace:
        normalized = {normalized_filename_key(entry.filename) for entry in entries}
    extensionless: set[str] = set()
    normalized_extensionless: set[str] = set()
    if ignore_file_extension:
        extensionless = {extensionless_filename_key(entry.filename) for entry in entries}
        if allow_whitespace:
            normalized_extensionless = {normalized_extensionless_filename_key(entry.filename) for entry in entries}
    return exact, normalized, extensionless, normalized_extensionless


def wanted_extensions(entries: list[MissingEntry], ignore_file_extension: bool) -> set[str]:
    if ignore_file_extension:
        return set(AUDIO_FILE_EXTENSIONS)
    return {ntpath.splitext(entry.filename)[1].lower() for entry in entries if ntpath.splitext(entry.filename)[1]}


def scan_fingerprint(entries: list[MissingEntry], options: RunOptions, search_engine: str) -> str:
    payload = {
        "index_version": 2,
        "search_engine": search_engine,
        "playlist_path": normalize_for_compare(options.playlist_path) if options.playlist_path else "",
        "scan_roots": [normalize_for_compare(root) for root in options.scan_roots],
        "filenames": sorted({entry.filename.lower() for entry in entries}),
        "allow_whitespace": options.allow_whitespace_filename_fallback,
        "ignore_file_extension": options.ignore_file_extension,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def add_index_candidate(index: dict[str, list[str]], key: str, path: str) -> None:
    values = index.setdefault(key, [])
    norm = normalize_for_compare(path)
    if all(normalize_for_compare(existing) != norm for existing in values):
        values.append(path)


def file_is_under_root(path: str | Path, root: str | Path) -> bool:
    path_norm = normalize_for_compare(path)
    root_norm = normalize_for_compare(root)
    return path_norm == root_norm or path_norm.startswith(root_norm.rstrip("\\/") + os.sep)


def index_candidate_path(
    index: dict[str, list[str]],
    path: str,
    exact_wanted: set[str],
    normalized_wanted: set[str],
    extensionless_wanted: set[str],
    normalized_extensionless_wanted: set[str],
) -> bool:
    name = ntpath.basename(path.replace("/", "\\"))
    found = False
    exact_key = exact_filename_key(name)
    normalized_key = normalized_filename_key(name)
    extensionless_key = extensionless_filename_key(name)
    normalized_extensionless_key = normalized_extensionless_filename_key(name)
    if exact_key in exact_wanted:
        add_index_candidate(index, exact_key, path)
        found = True
    if normalized_key in normalized_wanted:
        add_index_candidate(index, normalized_key, path)
        found = True
    if extensionless_key in extensionless_wanted:
        add_index_candidate(index, extensionless_key, path)
        found = True
    if normalized_extensionless_key in normalized_extensionless_wanted:
        add_index_candidate(index, normalized_extensionless_key, path)
        found = True
    return found


def find_es_executable() -> str | None:
    found = shutil.which("es.exe") or shutil.which("es")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Everything" / "es.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Everything" / "es.exe",
        SCRIPT_DIR / "es.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def everything_extension_query(entries: list[MissingEntry], ignore_file_extension: bool) -> str:
    extensions = sorted(ext.lstrip(".") for ext in wanted_extensions(entries, ignore_file_extension) if ext)
    if not extensions:
        return ""
    return f"ext:{';'.join(extensions)}"


def everything_scan_root(
    es: str,
    root: Path,
    query_text: str,
    max_results: int,
    checkpoint: SearchCheckpoint,
    exact_wanted: set[str],
    normalized_wanted: set[str],
    extensionless_wanted: set[str],
    normalized_extensionless_wanted: set[str],
    cancel_event: threading.Event | None,
) -> None:
    command = [es, "-path", str(root), "-full-path-and-name"]
    if max_results > 0:
        command.extend(["-n", str(max_results)])
    if query_text:
        command.append(query_text)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            check_cancel(cancel_event)
            candidate = line.strip()
            if not candidate or not file_is_under_root(candidate, root):
                continue
            if index_candidate_path(
                checkpoint.index,
                candidate,
                exact_wanted,
                normalized_wanted,
                extensionless_wanted,
                normalized_extensionless_wanted,
            ):
                checkpoint.result_count += 1
        stderr = process.stderr.read() if process.stderr is not None else ""
        return_code = process.wait()
    except OperationCancelled:
        process.terminate()
        raise
    if return_code not in (0, 1):
        raise RuntimeError(stderr.strip() or f"es.exe failed with exit code {return_code}")


def build_index_everything(
    entries: list[MissingEntry],
    options: RunOptions,
    log: Callable[[str], None],
    cancel_event: threading.Event | None,
) -> tuple[dict[str, list[str]], bool]:
    es = find_es_executable()
    if not es:
        raise RuntimeError("es.exe was not found")
    exact_wanted, normalized_wanted, extensionless_wanted, normalized_extensionless_wanted = wanted_filename_sets(
        entries,
        options.allow_whitespace_filename_fallback,
        options.ignore_file_extension,
    )
    fingerprint = scan_fingerprint(entries, options, "everything")
    checkpoint = load_checkpoint(options.state_dir, fingerprint, "everything") if options.resume_scan else None
    if checkpoint:
        log("Resuming Everything search checkpoint.")
    else:
        checkpoint = SearchCheckpoint(fingerprint=fingerprint, search_engine="everything")
    query_text = everything_extension_query(entries, options.ignore_file_extension)
    for root in options.scan_roots:
        root_text = str(root)
        root_key = normalize_for_compare(root)
        if root_key in checkpoint.completed_roots:
            continue
        check_cancel(cancel_event)
        if query_text:
            log(f"Searching Everything index under {root_text} for wanted extensions...")
        else:
            log(f"Searching Everything index under {root_text}...")
        everything_scan_root(
            es,
            root,
            query_text,
            options.max_everything_results,
            checkpoint,
            exact_wanted,
            normalized_wanted,
            extensionless_wanted,
            normalized_extensionless_wanted,
            cancel_event,
        )
        checkpoint.completed_roots.add(root_key)
        if options.resume_scan:
            save_checkpoint(options.state_dir, checkpoint)
    if options.resume_scan:
        clear_checkpoint(options.state_dir)
    return checkpoint.index, checkpoint.resumed


def build_index_by_scan(
    entries: list[MissingEntry],
    options: RunOptions,
    log: Callable[[str], None],
    cancel_event: threading.Event | None,
) -> tuple[dict[str, list[str]], bool]:
    exact_wanted, normalized_wanted, extensionless_wanted, normalized_extensionless_wanted = wanted_filename_sets(
        entries,
        options.allow_whitespace_filename_fallback,
        options.ignore_file_extension,
    )
    wanted_exts = wanted_extensions(entries, options.ignore_file_extension)
    fingerprint = scan_fingerprint(entries, options, "scan")
    checkpoint = load_checkpoint(options.state_dir, fingerprint, "scan") if options.resume_scan else None
    if checkpoint:
        log("Resuming direct scan checkpoint.")
    else:
        checkpoint = SearchCheckpoint(fingerprint=fingerprint, search_engine="scan")
    for root in options.scan_roots:
        root_key = normalize_for_compare(root)
        if root_key in checkpoint.completed_roots:
            continue
        if checkpoint.pending_root == root_key and checkpoint.pending_stack:
            stack = [Path(item) for item in checkpoint.pending_stack]
        else:
            stack = [root]
            checkpoint.pending_root = root_key
        log(f"Scanning folder {root}...")
        save_counter = 0
        while stack:
            check_cancel(cancel_event)
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    for item in iterator:
                        check_cancel(cancel_event)
                        try:
                            if item.is_dir(follow_symlinks=False):
                                stack.append(Path(item.path))
                            elif item.is_file(follow_symlinks=False):
                                checkpoint.scanned_files += 1
                                if checkpoint.scanned_files % 25000 == 0:
                                    log(f"Scanned {checkpoint.scanned_files} files...")
                                if wanted_exts and ntpath.splitext(item.name)[1].lower() not in wanted_exts:
                                    continue
                                if index_candidate_path(
                                    checkpoint.index,
                                    item.path,
                                    exact_wanted,
                                    normalized_wanted,
                                    extensionless_wanted,
                                    normalized_extensionless_wanted,
                                ):
                                    checkpoint.result_count += 1
                        except OSError:
                            continue
            except OSError:
                continue
            save_counter += 1
            if options.resume_scan and save_counter >= 1000:
                checkpoint.pending_stack = [str(item) for item in stack]
                save_checkpoint(options.state_dir, checkpoint)
                save_counter = 0
        checkpoint.pending_root = ""
        checkpoint.pending_stack = []
        checkpoint.completed_roots.add(root_key)
        if options.resume_scan:
            save_checkpoint(options.state_dir, checkpoint)
    if options.resume_scan:
        clear_checkpoint(options.state_dir)
    log(f"Scanned {checkpoint.scanned_files} files and found {checkpoint.result_count} candidate hits.")
    return checkpoint.index, checkpoint.resumed


def build_search_index(
    entries: list[MissingEntry],
    options: RunOptions,
    log: Callable[[str], None],
    cancel_event: threading.Event | None,
) -> tuple[dict[str, list[str]], str, bool]:
    if not entries:
        return {}, "none", False
    if options.search_mode == "scan":
        index, resumed = build_index_by_scan(entries, options, log, cancel_event)
        return index, "scan", resumed
    if options.search_mode == "everything":
        index, resumed = build_index_everything(entries, options, log, cancel_event)
        return index, "everything", resumed
    try:
        index, resumed = build_index_everything(entries, options, log, cancel_event)
        return index, "everything", resumed
    except Exception as exc:
        log(f"Everything search unavailable ({exc}); falling back to direct folder scan.")
        index, resumed = build_index_by_scan(entries, options, log, cancel_event)
        return index, "scan", resumed


def safe_file_size(path: str | Path) -> int | None:
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def sha256_file(path: str | Path, cancel_event: threading.Event | None) -> str | None:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while True:
                check_cancel(cancel_event)
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def candidate_sort_key(path: str, scan_roots: list[Path]) -> tuple[int, int, str]:
    priority = len(scan_roots)
    for index, root in enumerate(scan_roots):
        if file_is_under_root(path, root):
            priority = index
            break
    return priority, len(path), path.lower()


def canonical_candidate(paths: list[str], scan_roots: list[Path]) -> str:
    return sorted(paths, key=lambda item: candidate_sort_key(item, scan_roots))[0]


def scan_root_priority_candidate(candidates: list[str], scan_roots: list[Path]) -> str | None:
    if len(candidates) < 2 or not scan_roots:
        return None
    buckets: dict[int, list[str]] = {}
    for candidate in candidates:
        for index, root in enumerate(scan_roots):
            if file_is_under_root(candidate, root):
                buckets.setdefault(index, []).append(candidate)
                break
    if not buckets:
        return None
    first_priority = min(buckets)
    first_candidates = buckets[first_priority]
    if len(first_candidates) == 1:
        return first_candidates[0]
    return None


def dedupe_exact_candidates(
    candidates: list[str],
    scan_roots: list[Path],
    cancel_event: threading.Event | None,
) -> str | None:
    if len(candidates) < 2:
        return None
    sizes: dict[int, list[str]] = {}
    for candidate in candidates:
        size = safe_file_size(candidate)
        if size is None:
            return None
        sizes.setdefault(size, []).append(candidate)
    if len(sizes) != 1:
        return None
    hashes: dict[str, list[str]] = {}
    for candidate in candidates:
        digest = sha256_file(candidate, cancel_event)
        if digest is None:
            return None
        hashes.setdefault(digest, []).append(candidate)
    if len(hashes) != 1:
        return None
    return canonical_candidate(candidates, scan_roots)


def unique_existing_candidates(candidates: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        norm = normalize_for_compare(candidate)
        if norm in seen:
            continue
        if not Path(candidate).exists():
            continue
        seen.add(norm)
        result.append(candidate)
    return result


def parent_context_candidate(entry: MissingEntry, candidates: list[str]) -> str | None:
    old_parent = normalize_path_component_for_match(parent_folder_name(entry.old_path))
    if not old_parent:
        return None
    matches = [
        candidate
        for candidate in candidates
        if normalize_path_component_for_match(parent_folder_name(candidate)) == old_parent
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def mark_update(entry: MissingEntry, candidate: str, status: str, reason: str) -> None:
    entry.new_path = candidate
    entry.match_status = status
    entry.action = "would_update"
    entry.reason = reason


def mark_skip(entry: MissingEntry, status: str, reason: str) -> None:
    entry.new_path = ""
    entry.match_status = status
    entry.action = "skipped"
    entry.reason = reason


def resolve_candidates(
    entry: MissingEntry,
    candidates: list[str],
    match_kind: str,
    options: RunOptions,
    cancel_event: threading.Event | None,
) -> None:
    candidates = unique_existing_candidates(candidates)
    entry.candidates = sorted(candidates, key=lambda item: item.lower())
    entry.candidate_count = len(candidates)
    if not candidates:
        mark_skip(entry, "not_found", "No matching file was found in the selected scan folders.")
        return

    prefixes = {
        "exact": "matched_by_exact_filename",
        "normalized": "matched_by_normalized_filename",
        "extensionless": "matched_by_filename_without_extension",
        "normalized_extensionless": "matched_by_normalized_filename_without_extension",
    }
    prefix = prefixes[match_kind]
    extensionless_match = match_kind in {"extensionless", "normalized_extensionless"}
    parent_context_allowed = match_kind in {"normalized", "extensionless", "normalized_extensionless"}
    if entry.expected_size is not None:
        size_matches = [candidate for candidate in candidates if safe_file_size(candidate) == entry.expected_size]
        if len(size_matches) == 1:
            mark_update(entry, size_matches[0], f"{prefix}_and_size", "Filename and stored file size match.")
            return
        if len(size_matches) > 1:
            if options.dedupe_exact_candidates:
                deduped = dedupe_exact_candidates(size_matches, options.scan_roots, cancel_event)
                if deduped:
                    mark_update(entry, deduped, "deduped_exact_duplicate", "Multiple candidates are byte-identical; selected canonical path.")
                    return
            mark_skip(entry, "ambiguous", "Multiple candidates match the stored file size.")
            return
        if extensionless_match and len(candidates) == 1:
            mark_update(
                entry,
                candidates[0],
                f"{prefix}_size_mismatch",
                "Filename matches when extension is ignored, but VirtualDJ's stored file size differs.",
            )
            return
        if parent_context_allowed:
            parent_candidate = parent_context_candidate(entry, candidates)
            if parent_candidate:
                mark_update(
                    entry,
                    parent_candidate,
                    f"{prefix}_and_parent_size_mismatch",
                    "Filename and parent folder match, but VirtualDJ's stored file size differs.",
                )
                return
        if extensionless_match and options.prefer_scan_root_order:
            priority_candidate = scan_root_priority_candidate(candidates, options.scan_roots)
            if priority_candidate:
                mark_update(
                    entry,
                    priority_candidate,
                    f"{prefix}_and_scan_root_priority_size_mismatch",
                    "Filename matches when extension is ignored; stored file size differs, so selected the only candidate in the highest-priority scan folder.",
                )
                return
        mark_skip(entry, "size_mismatch", "Filename exists, but no candidate matches VirtualDJ's stored file size.")
        return

    if len(candidates) == 1:
        mark_update(entry, candidates[0], prefix, "Exactly one filename candidate was found.")
        return
    if options.dedupe_exact_candidates:
        deduped = dedupe_exact_candidates(candidates, options.scan_roots, cancel_event)
        if deduped:
            mark_update(entry, deduped, "deduped_exact_duplicate", "Multiple candidates are byte-identical; selected canonical path.")
            return
    if parent_context_allowed:
        parent_candidate = parent_context_candidate(entry, candidates)
        if parent_candidate:
            mark_update(
                entry,
                parent_candidate,
                f"{prefix}_and_parent",
                "Filename and parent folder identify one candidate.",
            )
            return
    if options.prefer_scan_root_order:
        priority_candidate = scan_root_priority_candidate(candidates, options.scan_roots)
        if priority_candidate:
            mark_update(
                entry,
                priority_candidate,
                f"{prefix}_and_scan_root_priority_no_size",
                "No stored file size; multiple filename candidates found, selected the only candidate in the highest-priority scan folder.",
            )
            return
    mark_skip(entry, "ambiguous", "Multiple candidates were found and none could be selected safely.")


def resolve_matches(
    entries: list[MissingEntry],
    index: dict[str, list[str]],
    options: RunOptions,
    log: Callable[[str], None],
    cancel_event: threading.Event | None,
) -> None:
    for entry in entries:
        check_cancel(cancel_event)
        exact_candidates = index.get(exact_filename_key(entry.filename), [])
        if exact_candidates:
            resolve_candidates(entry, exact_candidates, "exact", options, cancel_event)
            continue
        if options.allow_whitespace_filename_fallback:
            normalized_candidates = index.get(normalized_filename_key(entry.filename), [])
            if normalized_candidates:
                resolve_candidates(entry, normalized_candidates, "normalized", options, cancel_event)
                continue
        if options.ignore_file_extension:
            extensionless_candidates = index.get(extensionless_filename_key(entry.filename), [])
            if extensionless_candidates:
                resolve_candidates(entry, extensionless_candidates, "extensionless", options, cancel_event)
                continue
            if options.allow_whitespace_filename_fallback:
                normalized_extensionless_candidates = index.get(normalized_extensionless_filename_key(entry.filename), [])
                if normalized_extensionless_candidates:
                    resolve_candidates(entry, normalized_extensionless_candidates, "normalized_extensionless", options, cancel_event)
                    continue
        mark_skip(entry, "not_found", "No matching file was found in the selected scan folders.")
    log("Resolved candidate matches.")


def playlist_reference_path(path_value: str, source_path: Path) -> Path | None:
    cleaned = path_value.strip().strip('"')
    if not cleaned or is_url(cleaned):
        return None
    expanded = os.path.expandvars(cleaned).replace("/", "\\")
    if not is_windows_absolute(expanded):
        return source_path.parent / expanded
    return Path(expanded)


def canonical_playlist_reference_key(path_value: str, source_path: Path) -> str | None:
    resolved_path = playlist_reference_path(path_value, source_path)
    if resolved_path is None:
        return None
    return normalize_for_compare(resolved_path)


def plan_playlist_duplicate_removals(
    sources: list[SourceFile],
    entries: list[MissingEntry],
    options: RunOptions,
    log: Callable[[str], None],
    cancel_event: threading.Event | None,
) -> list[PlaylistDuplicate]:
    if not options.dedupe_playlist_entries:
        return []

    replacements = {
        (entry.source_path, entry.edit_start): entry.new_path
        for entry in entries
        if entry.source_kind == "playlist" and entry.action == "would_update" and entry.new_path
    }
    duplicates: list[PlaylistDuplicate] = []
    duplicate_id = 1

    def add_duplicate(
        duplicate_keys: set[tuple[Path, int]],
        source: SourceFile,
        record: PlaylistRecord,
        final_path: str,
        kept_record: PlaylistRecord,
        kept_final_path: str,
        reason: str,
    ) -> None:
        nonlocal duplicate_id
        key = (source.path, record.record_start)
        if key in duplicate_keys:
            return
        duplicate_keys.add(key)
        duplicates.append(
            PlaylistDuplicate(
                duplicate_id=duplicate_id,
                source_path=source.path,
                reference=record.reference,
                old_path=record.path_value,
                final_path=final_path,
                kept_path=kept_final_path,
                filename=record.filename,
                edit_start=record.record_start,
                edit_end=record.record_end,
                reason=reason,
            )
        )
        duplicate_id += 1

    for source in sources:
        if source.kind != "playlist":
            continue
        planned_records: list[tuple[PlaylistRecord, str]] = []
        duplicate_keys: set[tuple[Path, int]] = set()
        seen: dict[str, tuple[PlaylistRecord, str]] = {}
        for record in parse_playlist_records(source):
            check_cancel(cancel_event)
            final_path = replacements.get((source.path, record.path_start), record.path_value)
            planned_records.append((record, final_path))
            key = canonical_playlist_reference_key(final_path, source.path)
            if key is None:
                continue
            kept = seen.get(key)
            if kept is None:
                seen[key] = (record, final_path)
                continue
            kept_record, kept_final_path = kept
            add_duplicate(
                duplicate_keys,
                source,
                record,
                final_path,
                kept_record,
                kept_final_path,
                (
                    "Duplicate playlist entry in this playlist; final path matches "
                    f"the first occurrence at {kept_record.reference}."
                ),
            )
        content_groups: dict[tuple[str, int], list[tuple[PlaylistRecord, str, Path]]] = {}
        for record, final_path in planned_records:
            if (source.path, record.record_start) in duplicate_keys:
                continue
            local_path = playlist_reference_path(final_path, source.path)
            if local_path is None:
                continue
            size = safe_file_size(local_path)
            if size is None:
                continue
            filename_key = filename_from_path(final_path).lower()
            content_groups.setdefault((filename_key, size), []).append((record, final_path, local_path))
        for group in content_groups.values():
            if len(group) < 2:
                continue
            seen_hashes: dict[str, tuple[PlaylistRecord, str]] = {}
            for record, final_path, local_path in group:
                check_cancel(cancel_event)
                digest = sha256_file(local_path, cancel_event)
                if digest is None:
                    continue
                kept = seen_hashes.get(digest)
                if kept is None:
                    seen_hashes[digest] = (record, final_path)
                    continue
                kept_record, kept_final_path = kept
                add_duplicate(
                    duplicate_keys,
                    source,
                    record,
                    final_path,
                    kept_record,
                    kept_final_path,
                    (
                        "Duplicate playlist entry in this playlist; file name, size, "
                        f"and SHA-256 content match the first occurrence at {kept_record.reference}."
                    ),
                )
    if duplicates:
        log(f"Found {len(duplicates)} duplicate playlist entries to remove within individual playlists.")
        mark_missing_entries_removed_by_playlist_dedupe(entries, duplicates)
    return duplicates


def mark_missing_entries_removed_by_playlist_dedupe(
    entries: list[MissingEntry],
    duplicates: list[PlaylistDuplicate],
) -> None:
    duplicate_ranges_by_source: dict[Path, list[PlaylistDuplicate]] = {}
    for duplicate in duplicates:
        duplicate_ranges_by_source.setdefault(duplicate.source_path, []).append(duplicate)
    for entry in entries:
        for duplicate in duplicate_ranges_by_source.get(entry.source_path, []):
            if duplicate.edit_start <= entry.edit_start < duplicate.edit_end:
                entry.action = duplicate.action
                entry.match_status = duplicate.match_status
                entry.new_path = duplicate.final_path
                entry.reason = duplicate.reason
                break


def source_relative_path(source_path: Path, vdj_root: Path) -> Path:
    try:
        return source_path.resolve().relative_to(vdj_root.resolve())
    except ValueError:
        return Path(source_path.name)


def replacement_text(entry: MissingEntry) -> str:
    if entry.edit_format == "xml_attr":
        return html.escape(entry.new_path, quote=True)
    return entry.new_path


def backup_manifest_path(backup_root: Path) -> Path:
    return backup_root / "backup-manifest.json"


def write_backup_manifest(backup_root: Path, manifest_entries: list[dict[str, str]]) -> None:
    manifest_path = backup_manifest_path(backup_root)
    manifest_path.write_text(json.dumps({"version": 1, "files": manifest_entries}, indent=2), encoding="utf-8")


def load_backup_manifest(backup_root: Path) -> list[tuple[Path, Path]]:
    manifest_path = backup_manifest_path(backup_root)
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    files = manifest.get("files", [])
    if not isinstance(files, list):
        return []
    entries: list[tuple[Path, Path]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        backup_path_value = item.get("backup_path")
        target_path_value = item.get("target_path")
        if not backup_path_value or not target_path_value:
            continue
        backup_path = Path(backup_path_value)
        target_path = Path(target_path_value)
        if not backup_path.is_absolute():
            backup_path = backup_root / backup_path
        if not target_path.is_absolute():
            target_path = Path(target_path_value)
        entries.append((backup_path, target_path))
    return entries


def resolve_backup_target(target_path: str | Path, vdj_root: Path) -> Path:
    path = Path(target_path)
    if path.is_absolute():
        return path
    return vdj_root / path


def iter_backup_restore_targets(backup_root: Path, vdj_root: Path) -> list[tuple[Path, Path]]:
    manifest_entries = load_backup_manifest(backup_root)
    if manifest_entries:
        restore_targets: list[tuple[Path, Path]] = []
        for backup_path, target_path in manifest_entries:
            if not backup_path.is_absolute():
                backup_path = backup_root / backup_path
            target_path = resolve_backup_target(target_path, vdj_root)
            restore_targets.append((backup_path, target_path))
        return restore_targets

    restore_targets: list[tuple[Path, Path]] = []
    for path in sorted(backup_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "backup-manifest.json":
            continue
        try:
            relative = path.relative_to(backup_root)
        except ValueError:
            continue
        if relative.parts and relative.parts[0] == "undo-safety":
            continue
        restore_targets.append((path, vdj_root / relative))
    return restore_targets


def restore_latest_backup(options: RunOptions, log: Callable[[str], None] = log_noop) -> bool:
    if not options.backup_dir.exists():
        log(f"Backup folder does not exist: {options.backup_dir}")
        return False

    backup_roots = sorted(
        [path for path in options.backup_dir.glob("vdj-relocator-backup-*") if path.is_dir()],
        key=lambda path: path.name,
        reverse=True,
    )
    if not backup_roots:
        log(f"No backup folders found in {options.backup_dir}")
        return False

    backup_root = backup_roots[0]
    restore_targets = iter_backup_restore_targets(backup_root, options.vdj_root)
    if not restore_targets:
        log(f"No files available to restore from {backup_root}")
        return False

    restored_count = 0
    for backup_path, target_path in restore_targets:
        if not backup_path.exists():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            try:
                relative_target = target_path.relative_to(options.vdj_root)
            except ValueError:
                relative_target = Path(target_path.name)
            safety_path = backup_root / "undo-safety" / relative_target
            safety_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_path, safety_path)
        shutil.copy2(backup_path, target_path)
        restored_count += 1

    log(f"Restored {restored_count} files from {backup_root}")
    return restored_count > 0


def apply_changes(
    sources: list[SourceFile],
    entries: list[MissingEntry],
    playlist_duplicates: list[PlaylistDuplicate],
    options: RunOptions,
    log: Callable[[str], None],
) -> tuple[Path | None, int]:
    updates = [entry for entry in entries if entry.action == "would_update"]
    removals = [duplicate for duplicate in playlist_duplicates if duplicate.action == "would_remove_duplicate"]
    if not updates and not removals:
        return None, 0
    backup_root = options.backup_dir / f"vdj-relocator-backup-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_root.mkdir(parents=True, exist_ok=True)
    source_map = {source.path: source for source in sources}
    changed_sources = 0
    manifest_entries: list[dict[str, str]] = []
    changed_paths = {entry.source_path for entry in updates} | {duplicate.source_path for duplicate in removals}
    for source_path in sorted(changed_paths):
        source = source_map[source_path]
        relative = source_relative_path(source_path, options.vdj_root)
        backup_path = backup_root / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, backup_path)
        manifest_entries.append({"backup_path": str(relative), "target_path": str(relative)})
        source_entries = [entry for entry in updates if entry.source_path == source_path]
        source_removals = [duplicate for duplicate in removals if duplicate.source_path == source_path]
        operations: list[tuple[int, int, str]] = []
        operations.extend((entry.edit_start, entry.edit_end, replacement_text(entry)) for entry in source_entries)
        operations.extend((duplicate.edit_start, duplicate.edit_end, "") for duplicate in source_removals)
        text = source.text
        for edit_start, edit_end, replacement in sorted(operations, key=lambda item: item[0], reverse=True):
            text = text[:edit_start] + replacement + text[edit_end:]
        write_text(source_path, text, source.encoding)
        for entry in source_entries:
            entry.action = "updated"
            entry.reason = entry.reason.replace("Would update", "Updated")
        for duplicate in source_removals:
            duplicate.action = "removed_duplicate"
        for entry in entries:
            if entry.source_path != source_path or entry.action != "would_remove_duplicate":
                continue
            if any(duplicate.edit_start <= entry.edit_start < duplicate.edit_end for duplicate in source_removals):
                entry.action = "removed_duplicate"
        changed_sources += 1
    write_backup_manifest(backup_root, manifest_entries)
    log(f"Applied changes to {changed_sources} source files.")
    return backup_root, changed_sources


def report_columns() -> list[str]:
    return [
        "entry_id",
        "source_kind",
        "source_path",
        "reference",
        "action",
        "match_status",
        "reason",
        "old_path",
        "new_path",
        "filename",
        "expected_size",
        "candidate_count",
        "candidates",
    ]


def write_report(entries: list[MissingEntry], playlist_duplicates: list[PlaylistDuplicate], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"vdj-relocator-report-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    with report_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=report_columns())
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {
                    "entry_id": entry.entry_id,
                    "source_kind": entry.source_kind,
                    "source_path": str(entry.source_path),
                    "reference": entry.reference,
                    "action": entry.action,
                    "match_status": entry.match_status,
                    "reason": entry.reason,
                    "old_path": entry.old_path,
                    "new_path": entry.new_path,
                    "filename": entry.filename,
                    "expected_size": entry.expected_size or "",
                    "candidate_count": entry.candidate_count,
                    "candidates": " | ".join(entry.candidates),
                }
            )
        for duplicate in playlist_duplicates:
            writer.writerow(
                {
                    "entry_id": f"playlist-duplicate-{duplicate.duplicate_id}",
                    "source_kind": "playlist",
                    "source_path": str(duplicate.source_path),
                    "reference": duplicate.reference,
                    "action": duplicate.action,
                    "match_status": duplicate.match_status,
                    "reason": duplicate.reason,
                    "old_path": duplicate.old_path,
                    "new_path": duplicate.final_path,
                    "filename": duplicate.filename,
                    "expected_size": "",
                    "candidate_count": "",
                    "candidates": f"kept: {duplicate.kept_path}",
                }
            )
    return report_path


def summarize(
    entries: list[MissingEntry],
    playlist_duplicates: list[PlaylistDuplicate],
    report_path: Path | None,
    backup_path: Path | None,
    sources_changed: int,
    search_engine: str,
    canceled: bool,
    resumed: bool,
) -> RunResult:
    return RunResult(
        report_path=report_path,
        backup_path=backup_path,
        total_missing=len(entries),
        would_update=sum(1 for entry in entries if entry.action == "would_update"),
        updated=sum(1 for entry in entries if entry.action == "updated"),
        ambiguous=sum(1 for entry in entries if entry.match_status == "ambiguous"),
        not_found=sum(1 for entry in entries if entry.match_status == "not_found"),
        deduped=sum(1 for entry in entries if entry.match_status == "deduped_exact_duplicate"),
        playlist_duplicates=len(playlist_duplicates),
        skipped=sum(1 for entry in entries if entry.action == "skipped"),
        sources_changed=sources_changed,
        search_engine=search_engine,
        canceled=canceled,
        resumed=resumed,
    )


def run_relocator(
    options: RunOptions,
    log: Callable[[str], None] = log_noop,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    sources: list[SourceFile] = []
    entries: list[MissingEntry] = []
    playlist_duplicates: list[PlaylistDuplicate] = []
    report_path: Path | None = None
    backup_path: Path | None = None
    search_engine = "none"
    resumed = False
    sources_changed = 0
    canceled = False
    try:
        check_cancel(cancel_event)
        sources = discover_sources(options.vdj_root, options.include_database, options.playlist_path, log)
        entries = parse_sources(sources, log)
        check_cancel(cancel_event)
        index, search_engine, resumed = build_search_index(entries, options, log, cancel_event)
        check_cancel(cancel_event)
        resolve_matches(entries, index, options, log, cancel_event)
        check_cancel(cancel_event)
        playlist_duplicates = plan_playlist_duplicate_removals(sources, entries, options, log, cancel_event)
        check_cancel(cancel_event)
        if options.apply:
            backup_path, sources_changed = apply_changes(sources, entries, playlist_duplicates, options, log)
        report_path = write_report(entries, playlist_duplicates, options.report_dir)
        log(f"Report written: {report_path}")
    except OperationCancelled:
        canceled = True
        log("Operation stopped by user.")
        if entries or playlist_duplicates:
            report_path = write_report(entries, playlist_duplicates, options.report_dir)
            log(f"Partial report written: {report_path}")
    return summarize(entries, playlist_duplicates, report_path, backup_path, sources_changed, search_engine, canceled, resumed)


def format_result(result: RunResult) -> str:
    lines = [
        f"Search engine: {result.search_engine}",
        f"Missing references: {result.total_missing}",
        f"Would update: {result.would_update}",
        f"Updated: {result.updated}",
        f"Skipped: {result.skipped}",
        f"Ambiguous: {result.ambiguous}",
        f"Not found: {result.not_found}",
        f"Deduped candidate files: {result.deduped}",
        f"Playlist duplicate rows: {result.playlist_duplicates}",
        f"Source files changed: {result.sources_changed}",
    ]
    if result.resumed:
        lines.append("Resumed from checkpoint: yes")
    if result.canceled:
        lines.append("Canceled: yes")
    if result.report_path:
        lines.append(f"Report: {result.report_path}")
    if result.backup_path:
        lines.append(f"Backup: {result.backup_path}")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Relink missing VirtualDJ playlist/folder/database paths by exact filename match."
    )
    parser.add_argument("dropped_files", nargs="*", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--gui", action="store_true", help="Launch the folder-picker GUI.")
    parser.add_argument("--no-gui", action="store_true", help="Do not launch the GUI when scan roots are omitted.")
    parser.add_argument("--vdj-root", type=Path, default=DEFAULT_VDJ_ROOT, help=f"VirtualDJ folder. Default: {DEFAULT_VDJ_ROOT}")
    parser.add_argument("--playlist", type=Path, help="Only process this .m3u or .m3u8 playlist file.")
    parser.add_argument("--scan-root", type=Path, action="append", default=[], help="Folder to search for relocated music. May be supplied multiple times.")
    parser.add_argument("--apply", action="store_true", help="Write confirmed replacements. Without this, only a report is written.")
    parser.add_argument("--undo", action="store_true", help="Restore the latest apply backup and save safety copies of overwritten files.")
    parser.add_argument("--no-database", action="store_true", help="Do not scan/update database.xml; only playlists and .vdjfolder files are checked.")
    parser.add_argument("--search-mode", choices=("auto", "scan", "everything"), default="auto", help="auto uses Everything CLI if es.exe exists, otherwise scans selected folders.")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR, help=f"Report output folder. Default: {DEFAULT_REPORT_DIR}")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help=f"Backup output folder. Default: {DEFAULT_BACKUP_DIR}")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help=f"Resume checkpoint folder. Default: {DEFAULT_STATE_DIR}")
    parser.add_argument("--max-everything-results", type=int, default=1000000, help="Per-scan-folder result limit when using es.exe. Use 0 for no limit.")
    parser.add_argument("--no-resume", action="store_true", help="Ignore and do not write interrupted-scan checkpoints.")
    parser.add_argument("--no-dedupe-exact", action="store_true", help="Do not consolidate byte-identical duplicate file candidates; leave them ambiguous.")
    parser.add_argument("--no-playlist-dedupe", action="store_true", help="Do not remove duplicate song entries within each playlist.")
    parser.add_argument("--no-scan-root-priority", action="store_true", help="Do not use scan folder order to resolve no-size ambiguous filename matches.")
    parser.add_argument("--ignore-extension", action="store_true", help="Treat audio files with the same name but different extensions as filename matches.")
    parser.add_argument("--no-whitespace-fallback", action="store_true", help="Do not repair filename matches that only differ by extra or missing whitespace.")
    return parser


def playlist_path_from_args(args: argparse.Namespace) -> Path | None:
    if args.playlist:
        return args.playlist
    for dropped_file in args.dropped_files:
        if is_supported_playlist(dropped_file):
            return dropped_file
    return None


def options_from_args(args: argparse.Namespace) -> RunOptions:
    return RunOptions(
        vdj_root=args.vdj_root,
        playlist_path=playlist_path_from_args(args),
        scan_roots=args.scan_root,
        include_database=not args.no_database,
        apply=args.apply,
        search_mode=args.search_mode,
        report_dir=args.report_dir,
        backup_dir=args.backup_dir,
        state_dir=args.state_dir,
        max_everything_results=args.max_everything_results,
        resume_scan=not args.no_resume,
        dedupe_exact_candidates=not args.no_dedupe_exact,
        dedupe_playlist_entries=not args.no_playlist_dedupe,
        prefer_scan_root_order=not args.no_scan_root_priority,
        ignore_file_extension=args.ignore_extension,
        allow_whitespace_filename_fallback=not args.no_whitespace_fallback,
    )


def launch_gui(initial_playlist: Path | None = None) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("VirtualDJ Playlist Helper")
    root.geometry("980x700")

    vdj_var = tk.StringVar(value=str(DEFAULT_VDJ_ROOT))
    playlist_var = tk.StringVar(value=str(initial_playlist) if initial_playlist else "")
    search_mode_var = tk.StringVar(value="auto")
    include_database_var = tk.BooleanVar(value=True)
    resume_var = tk.BooleanVar(value=True)
    dedupe_var = tk.BooleanVar(value=True)
    playlist_dedupe_var = tk.BooleanVar(value=True)
    scan_root_priority_var = tk.BooleanVar(value=True)
    ignore_extension_var = tk.BooleanVar(value=False)
    whitespace_var = tk.BooleanVar(value=True)
    status_queue: queue.Queue[str] = queue.Queue()
    cancel_event = threading.Event()
    worker: threading.Thread | None = None

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)
    main.columnconfigure(1, weight=1)
    main.rowconfigure(3, weight=1)
    main.rowconfigure(8, weight=1)

    ttk.Label(main, text="VirtualDJ folder").grid(row=0, column=0, sticky="w")
    vdj_entry = ttk.Entry(main, textvariable=vdj_var)
    vdj_entry.grid(row=0, column=1, sticky="ew", padx=(8, 8))

    def browse_vdj() -> None:
        selected = filedialog.askdirectory(title="Choose VirtualDJ folder", initialdir=vdj_var.get() or str(Path.home()))
        if selected:
            vdj_var.set(selected)

    ttk.Button(main, text="Browse", command=browse_vdj).grid(row=0, column=2, sticky="ew")

    ttk.Label(main, text="Single playlist").grid(row=1, column=0, sticky="w", pady=(10, 0))
    playlist_entry = ttk.Entry(main, textvariable=playlist_var)
    playlist_entry.grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(10, 0))
    playlist_buttons = ttk.Frame(main)
    playlist_buttons.grid(row=1, column=2, sticky="ew", pady=(10, 0))

    def browse_playlist() -> None:
        initial_dir = str(Path(playlist_var.get()).parent) if playlist_var.get().strip() else str(DEFAULT_VDJ_ROOT / "Playlists")
        selected = filedialog.askopenfilename(
            title="Choose one VirtualDJ playlist",
            initialdir=initial_dir,
            filetypes=[
                ("VirtualDJ playlists", "*.m3u *.m3u8"),
                ("M3U playlists", "*.m3u"),
                ("M3U8 playlists", "*.m3u8"),
                ("All files", "*.*"),
            ],
        )
        if selected:
            playlist_var.set(selected)

    def clear_playlist() -> None:
        playlist_var.set("")

    ttk.Button(playlist_buttons, text="Browse", command=browse_playlist).pack(side="left", fill="x", expand=True)
    ttk.Button(playlist_buttons, text="Clear", command=clear_playlist).pack(side="left", fill="x", expand=True, padx=(6, 0))

    ttk.Label(main, text="Scan folders").grid(row=2, column=0, sticky="nw", pady=(10, 0))
    scan_list = tk.Listbox(main, height=7, selectmode="extended")
    scan_list.grid(row=2, column=1, rowspan=2, sticky="nsew", padx=(8, 8), pady=(10, 0))
    scan_buttons = ttk.Frame(main)
    scan_buttons.grid(row=2, column=2, sticky="new", pady=(10, 0))

    def add_scan_folder() -> None:
        selected = filedialog.askdirectory(title="Choose folder to scan for relocated music", initialdir=str(Path.home()))
        if selected:
            existing = set(scan_list.get(0, tk.END))
            if selected not in existing:
                scan_list.insert(tk.END, selected)

    def remove_scan_folder() -> None:
        for index in reversed(scan_list.curselection()):
            scan_list.delete(index)

    ttk.Button(scan_buttons, text="Add", command=add_scan_folder).pack(fill="x")
    ttk.Button(scan_buttons, text="Remove", command=remove_scan_folder).pack(fill="x", pady=(8, 0))

    checks = ttk.Frame(main)
    checks.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 0))
    ttk.Checkbutton(checks, text="Include database.xml", variable=include_database_var).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(checks, text="Resume interrupted scan", variable=resume_var).grid(row=0, column=1, sticky="w", padx=(18, 0))
    ttk.Checkbutton(checks, text="Repair whitespace variants", variable=whitespace_var).grid(row=0, column=2, sticky="w", padx=(18, 0))
    ttk.Checkbutton(checks, text="Resolve byte-identical candidates", variable=dedupe_var).grid(row=1, column=0, sticky="w", pady=(6, 0))
    ttk.Checkbutton(checks, text="Remove playlist duplicates", variable=playlist_dedupe_var).grid(row=1, column=1, sticky="w", padx=(18, 0), pady=(6, 0))
    ttk.Checkbutton(checks, text="Prefer scan folder order", variable=scan_root_priority_var).grid(row=1, column=2, sticky="w", padx=(18, 0), pady=(6, 0))
    ttk.Checkbutton(checks, text="Ignore file extension", variable=ignore_extension_var).grid(row=2, column=0, sticky="w", pady=(6, 0))

    mode_frame = ttk.Frame(main)
    mode_frame.grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))
    ttk.Label(mode_frame, text="Search mode").pack(side="left")
    ttk.OptionMenu(mode_frame, search_mode_var, "auto", "auto", "everything", "scan").pack(side="left", padx=(8, 0))

    button_frame = ttk.Frame(main)
    button_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))
    dry_button = ttk.Button(button_frame, text="Dry Run")
    apply_button = ttk.Button(button_frame, text="Apply Fixes")
    undo_button = ttk.Button(button_frame, text="Undo Last")
    stop_button = ttk.Button(button_frame, text="Stop", state="disabled")
    dry_button.pack(side="left")
    apply_button.pack(side="left", padx=(8, 0))
    undo_button.pack(side="left", padx=(8, 0))
    stop_button.pack(side="left", padx=(8, 0))

    ttk.Label(main, text="Status").grid(row=7, column=0, sticky="w", pady=(12, 0))
    status_text = tk.Text(main, height=14, wrap="word")
    status_text.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(4, 0))

    def append_status(message: str) -> None:
        status_text.insert(tk.END, message + "\n")
        status_text.see(tk.END)

    def poll_status() -> None:
        while True:
            try:
                append_status(status_queue.get_nowait())
            except queue.Empty:
                break
        root.after(100, poll_status)

    def set_running(running: bool) -> None:
        state = "disabled" if running else "normal"
        dry_button.configure(state=state)
        apply_button.configure(state=state)
        undo_button.configure(state=state)
        stop_button.configure(state="normal" if running else "disabled")

    def build_options(apply: bool) -> RunOptions | None:
        scan_roots = [Path(scan_list.get(index)) for index in range(scan_list.size())]
        playlist_text = playlist_var.get().strip()
        playlist_path = Path(playlist_text) if playlist_text else None
        if playlist_path is not None and not is_supported_playlist(playlist_path):
            messagebox.showerror("Unsupported playlist", "Choose a .m3u or .m3u8 playlist file.")
            return None
        if not scan_roots and playlist_path is None:
            messagebox.showerror("Missing input", "Add at least one scan folder or choose a single playlist.")
            return None
        return RunOptions(
            vdj_root=Path(vdj_var.get()),
            playlist_path=playlist_path,
            scan_roots=scan_roots,
            include_database=include_database_var.get(),
            apply=apply,
            search_mode=search_mode_var.get(),
            report_dir=DEFAULT_REPORT_DIR,
            backup_dir=DEFAULT_BACKUP_DIR,
            state_dir=DEFAULT_STATE_DIR,
            max_everything_results=1000000,
            resume_scan=resume_var.get(),
            dedupe_exact_candidates=dedupe_var.get(),
            dedupe_playlist_entries=playlist_dedupe_var.get(),
            prefer_scan_root_order=scan_root_priority_var.get(),
            ignore_file_extension=ignore_extension_var.get(),
            allow_whitespace_filename_fallback=whitespace_var.get(),
        )

    def run_job(apply: bool) -> None:
        nonlocal worker
        options = build_options(apply)
        if options is None:
            return
        cancel_event.clear()
        set_running(True)
        status_text.delete("1.0", tk.END)

        def worker_main() -> None:
            try:
                result = run_relocator(options, log=status_queue.put, cancel_event=cancel_event)
                status_queue.put(format_result(result))
                if result.canceled:
                    root.after(0, lambda: messagebox.showwarning("Stopped", "The operation was stopped. Review the partial report if one was written."))
                else:
                    root.after(0, lambda: messagebox.showinfo("Finished", format_result(result)))
            except Exception as exc:
                status_queue.put(f"Error: {exc}")
                root.after(0, lambda: messagebox.showerror("Error", str(exc)))
            finally:
                root.after(0, lambda: set_running(False))

        worker = threading.Thread(target=worker_main, daemon=True)
        worker.start()

    def run_undo() -> None:
        nonlocal worker
        options = RunOptions(
            vdj_root=Path(vdj_var.get()),
            playlist_path=None,
            scan_roots=[],
            include_database=include_database_var.get(),
            apply=False,
            search_mode=search_mode_var.get(),
            report_dir=DEFAULT_REPORT_DIR,
            backup_dir=DEFAULT_BACKUP_DIR,
            state_dir=DEFAULT_STATE_DIR,
            max_everything_results=1000000,
            resume_scan=resume_var.get(),
            dedupe_exact_candidates=dedupe_var.get(),
            dedupe_playlist_entries=playlist_dedupe_var.get(),
            prefer_scan_root_order=scan_root_priority_var.get(),
            ignore_file_extension=ignore_extension_var.get(),
            allow_whitespace_filename_fallback=whitespace_var.get(),
        )
        set_running(True)
        status_text.delete("1.0", tk.END)

        def worker_main() -> None:
            try:
                restored = restore_latest_backup(options, log=status_queue.put)
                status_queue.put(f"Undo {'succeeded' if restored else 'failed'}")
                root.after(0, lambda: messagebox.showinfo("Undo", "Undo completed." if restored else "No matching backup was found to restore."))
            except Exception as exc:
                status_queue.put(f"Error: {exc}")
                root.after(0, lambda: messagebox.showerror("Error", str(exc)))
            finally:
                root.after(0, lambda: set_running(False))

        worker = threading.Thread(target=worker_main, daemon=True)
        worker.start()

    def stop_job() -> None:
        cancel_event.set()
        append_status("Stop requested. Waiting for the current safe point...")

    dry_button.configure(command=lambda: run_job(False))
    apply_button.configure(command=lambda: run_job(True))
    undo_button.configure(command=run_undo)
    stop_button.configure(command=stop_job)
    poll_status()
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    playlist_path = playlist_path_from_args(args)
    if args.undo:
        options = options_from_args(args)
        restored = restore_latest_backup(options, log=print)
        print(f"Undo {'succeeded' if restored else 'failed'}")
        return 0 if restored else 1
    if args.gui or (not args.no_gui and not args.scan_root):
        launch_gui(playlist_path)
        return 0
    if not args.scan_root and playlist_path is None:
        parser.error("at least one --scan-root or --playlist is required when --no-gui is used")
    options = options_from_args(args)
    result = run_relocator(options, log=print)
    print(format_result(result))
    return 1 if result.canceled else 0


if __name__ == "__main__":
    raise SystemExit(main())
