#!/usr/bin/env python3
"""
Fast threaded file transfer helper for large ROM libraries.

Modes:
  adb-pull    Copy files from an Android device to this PC.
  adb-push    Copy files from this PC to an Android device.
  local-copy  Copy files between normal folders/drives.

The ADB modes are designed for Android handhelds such as the Retroid Pocket 5.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import os
import posixpath
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


WINDOWS_INVALID_CHARS = set('<>:"\\|?*')


@dataclass(frozen=True)
class TransferItem:
    rel: str
    source: str
    dest: str
    size: int | None = None
    mtime: int | None = None


@dataclass
class TransferResult:
    item: TransferItem
    status: str
    message: str = ""
    bytes_done: int = 0


@dataclass(frozen=True)
class RemoteDirEntry:
    name: str
    path: str


class Progress:
    def __init__(self, total_files: int, total_bytes: int | None, interval: float) -> None:
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.interval = interval
        self.started_at = time.monotonic()
        self.lock = threading.Lock()
        self.transferred_files = 0
        self.skipped_files = 0
        self.failed_files = 0
        self.transferred_bytes = 0
        self.skipped_bytes = 0
        self._done = threading.Event()
        self._last_line_len = 0

    def add(self, result: TransferResult) -> None:
        size = result.bytes_done if result.bytes_done else result.item.size or 0
        with self.lock:
            if result.status == "copied":
                self.transferred_files += 1
                self.transferred_bytes += size
            elif result.status == "skipped":
                self.skipped_files += 1
                self.skipped_bytes += size
            else:
                self.failed_files += 1

    def complete(self) -> None:
        self._done.set()

    def run(self) -> None:
        while not self._done.wait(self.interval):
            self.print_line(final=False)
        self.print_line(final=True)
        print()

    def print_line(self, final: bool) -> None:
        with self.lock:
            finished_files = self.transferred_files + self.skipped_files + self.failed_files
            elapsed = max(time.monotonic() - self.started_at, 0.001)
            speed = self.transferred_bytes / elapsed
            finished_bytes = self.transferred_bytes + self.skipped_bytes

            if self.total_bytes:
                percent = min(100.0, (finished_bytes / self.total_bytes) * 100)
                byte_part = f"{human_bytes(finished_bytes)} / {human_bytes(self.total_bytes)} ({percent:5.1f}%)"
            else:
                byte_part = f"{human_bytes(self.transferred_bytes)} copied"

            line = (
                f"{byte_part} | files {finished_files}/{self.total_files} | "
                f"copied {self.transferred_files}, skipped {self.skipped_files}, failed {self.failed_files} | "
                f"{human_bytes(speed)}/s"
            )
            if final:
                line = "Done: " + line

            padding = " " * max(0, self._last_line_len - len(line))
            print("\r" + line + padding, end="", flush=True)
            self._last_line_len = len(line)


def human_bytes(value: float | int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def adb_base(adb: str, serial: str | None) -> list[str]:
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    return cmd


def run_checked(cmd: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        check=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def check_adb(adb: str, serial: str | None) -> None:
    try:
        result = run_checked(adb_base(adb, serial) + ["get-state"], capture=True)
    except FileNotFoundError as exc:
        raise SystemExit(
            "ADB was not found. Install Android Platform Tools, then either add adb.exe to PATH "
            "or pass --adb C:\\path\\to\\adb.exe."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise SystemExit(
            "ADB could not see a ready device. Enable USB debugging on the Retroid, accept the "
            f"trust prompt, then retry. Details: {stderr or exc}"
        ) from exc

    if result.stdout.strip() != "device":
        raise SystemExit(f"ADB state is {result.stdout.strip()!r}, not 'device'.")


def remote_root_normalized(remote: str) -> str:
    if remote != "/":
        return remote.rstrip("/")
    return remote


def remote_find_root(remote: str) -> str:
    root = remote_root_normalized(remote)
    if root == "/":
        return root
    return root + "/."


def clean_remote_path(path: str) -> str:
    return path.replace("/./", "/").removesuffix("/.")


def remote_parent(remote: str) -> str:
    remote = remote_root_normalized(remote)
    if remote == "/":
        return "/"
    return posixpath.dirname(remote) or "/"


def remote_join(root: str, rel: str) -> str:
    root = remote_root_normalized(root)
    if not rel:
        return root
    return posixpath.join(root, *rel.replace("\\", "/").split("/"))


def remote_rel(root: str, full_path: str) -> str:
    root = remote_root_normalized(root)
    if root == "/":
        return full_path.lstrip("/")
    prefix = root + "/"
    if full_path == root:
        return posixpath.basename(full_path)
    if full_path.startswith(prefix):
        return full_path[len(prefix) :]
    return posixpath.basename(full_path)


def rel_to_local(root: Path, rel: str) -> Path:
    parts = [part for part in rel.replace("\\", "/").split("/") if part and part != "."]
    return root.joinpath(*parts)


def local_rel(root: Path, file_path: Path) -> str:
    return file_path.relative_to(root).as_posix()


def clean_remote_line(line: str) -> str:
    return line.rstrip("\r")


def list_remote_files(adb: str, serial: str | None, remote: str) -> list[TransferItem]:
    root = remote_root_normalized(remote)
    find_root = remote_find_root(root)
    script = (
        "if [ ! -d {root} ]; then "
        "echo '__FAST_TRANSFER_MISSING__' 1>&2; exit 2; "
        "else "
        "find {find_root} -type f -exec stat -c '%s\t%Y\t%n' {{}} \\; ; "
        "fi"
    ).format(root=sh_quote(root), find_root=sh_quote(find_root))
    cmd = adb_base(adb, serial) + ["shell", script]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    items: list[TransferItem] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = clean_remote_line(line).split("\t", 2)
        if len(parts) != 3:
            continue
        size_raw, mtime_raw, full_path = parts
        full_path = clean_remote_path(full_path)
        try:
            size = int(size_raw)
        except ValueError:
            size = None
        try:
            mtime = int(float(mtime_raw))
        except ValueError:
            mtime = None
        rel = remote_rel(root, full_path)
        items.append(TransferItem(rel=rel, source=full_path, dest="", size=size, mtime=mtime))

    if items:
        return items

    if "__FAST_TRANSFER_MISSING__" in (result.stderr or ""):
        raise SystemExit(
            f"Remote folder not found: {remote}. Use the Retroid Browser to choose the exact folder, "
            "especially if the files are on a removable SD card."
        )

    fallback = list_remote_files_without_stat(adb, serial, remote)
    if fallback is not None:
        return fallback

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise SystemExit(f"Could not scan remote path {remote!r}: {stderr or result}") from None
    return []


def list_remote_dirs(adb: str, serial: str | None, remote: str) -> list[RemoteDirEntry]:
    root = remote_root_normalized(remote)
    script = (
        "DIR={root}; "
        "if [ ! -d \"$DIR\" ]; then "
        "echo '__FAST_TRANSFER_MISSING__' 1>&2; exit 2; "
        "fi; "
        "for p in \"$DIR\"/* \"$DIR\"/.[!.]* \"$DIR\"/..?*; do "
        "[ -d \"$p\" ] || continue; "
        "case \"$p\" in \"$DIR/.\"|\"$DIR/..\") continue ;; esac; "
        "printf '%s\\n' \"$p\"; "
        "done"
    ).format(root=sh_quote(root))
    result = subprocess.run(
        adb_base(adb, serial) + ["shell", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if "__FAST_TRANSFER_MISSING__" in (result.stderr or ""):
        raise SystemExit(f"Remote folder not found: {remote}")
    if result.returncode != 0 and not result.stdout.strip():
        stderr = (result.stderr or "").strip()
        raise SystemExit(f"Could not list Retroid folder {remote!r}: {stderr or result}")

    entries: list[RemoteDirEntry] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        path = clean_remote_line(line)
        if not path or path in seen:
            continue
        seen.add(path)
        name = posixpath.basename(path.rstrip("/")) or path
        entries.append(RemoteDirEntry(name=name, path=path))
    return sorted(entries, key=lambda entry: entry.name.casefold())


def remote_dir_exists(adb: str, serial: str | None, remote: str) -> bool:
    script = f"[ -d {sh_quote(remote_root_normalized(remote))} ]"
    result = subprocess.run(
        adb_base(adb, serial) + ["shell", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def find_remote_storage_roots(adb: str, serial: str | None) -> list[RemoteDirEntry]:
    roots: list[RemoteDirEntry] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        normalized = remote_root_normalized(path)
        if normalized in seen:
            return
        if not remote_dir_exists(adb, serial, normalized):
            return
        seen.add(normalized)
        roots.append(RemoteDirEntry(name=posixpath.basename(normalized) or normalized, path=normalized))

    add("/sdcard")
    add("/storage/emulated/0")

    for parent in ("/storage", "/mnt/media_rw"):
        try:
            for entry in list_remote_dirs(adb, serial, parent):
                if entry.name in {"emulated", "self"}:
                    continue
                add(entry.path)
        except SystemExit:
            continue

    return sorted(roots, key=lambda entry: (0 if "-" in entry.name else 1, entry.name.casefold()))


def find_remote_rom_roots(adb: str, serial: str | None) -> list[RemoteDirEntry]:
    rom_roots: list[RemoteDirEntry] = []
    seen: set[str] = set()
    for root in find_remote_storage_roots(adb, serial):
        for folder_name in ("ROMs", "Roms", "roms"):
            candidate = remote_join(root.path, folder_name)
            if candidate in seen:
                continue
            if remote_dir_exists(adb, serial, candidate):
                seen.add(candidate)
                rom_roots.append(RemoteDirEntry(name=f"{root.name}/{folder_name}", path=candidate))
    return rom_roots


def list_remote_files_without_stat(adb: str, serial: str | None, remote: str) -> list[TransferItem] | None:
    root = remote_root_normalized(remote)
    find_root = remote_find_root(root)
    script = (
        "if [ ! -d {root} ]; then "
        "echo '__FAST_TRANSFER_MISSING__' 1>&2; exit 2; "
        "else "
        "find {find_root} -type f -print; "
        "fi"
    ).format(root=sh_quote(root), find_root=sh_quote(find_root))
    result = subprocess.run(
        adb_base(adb, serial) + ["shell", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if "__FAST_TRANSFER_MISSING__" in (result.stderr or ""):
        raise SystemExit(
            f"Remote folder not found: {remote}. Use the Retroid Browser to choose the exact folder, "
            "especially if the files are on a removable SD card."
        )
    if result.returncode != 0 and not result.stdout.strip():
        return None

    items: list[TransferItem] = []
    for line in result.stdout.splitlines():
        full_path = clean_remote_path(clean_remote_line(line))
        if not full_path:
            continue
        rel = remote_rel(root, full_path)
        items.append(TransferItem(rel=rel, source=full_path, dest="", size=None, mtime=None))
    return items


def list_local_files(root: Path, includes: Sequence[str], excludes: Sequence[str]) -> list[TransferItem]:
    root = root.resolve()
    if not root.exists():
        raise SystemExit(f"Local source does not exist: {root}")
    if not root.is_dir():
        raise SystemExit(f"Local source must be a folder: {root}")

    items: list[TransferItem] = []
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        for filename in filenames:
            file_path = current_path / filename
            try:
                stat = file_path.stat()
            except OSError as exc:
                print(f"Skipping unreadable file: {file_path} ({exc})", file=sys.stderr)
                continue
            rel = local_rel(root, file_path)
            if not matches_filters(rel, includes, excludes):
                continue
            items.append(
                TransferItem(
                    rel=rel,
                    source=str(file_path),
                    dest="",
                    size=stat.st_size,
                    mtime=int(stat.st_mtime),
                )
            )
    return items


def matches_filters(rel: str, includes: Sequence[str], excludes: Sequence[str]) -> bool:
    normalized = rel.replace("\\", "/")
    if includes and not any(fnmatch.fnmatch(normalized, pattern) for pattern in includes):
        return False
    if excludes and any(fnmatch.fnmatch(normalized, pattern) for pattern in excludes):
        return False
    return True


def apply_remote_filters(
    items: Iterable[TransferItem],
    includes: Sequence[str],
    excludes: Sequence[str],
) -> list[TransferItem]:
    return [item for item in items if matches_filters(item.rel, includes, excludes)]


def with_dest(items: Iterable[TransferItem], dest_fn) -> list[TransferItem]:
    return [
        TransferItem(rel=item.rel, source=item.source, dest=dest_fn(item), size=item.size, mtime=item.mtime)
        for item in items
    ]


def local_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def copy_local_file(item: TransferItem, force: bool, buffer_mb: int) -> TransferResult:
    source = Path(item.source)
    dest = Path(item.dest)

    if not force and dest.exists() and item.size is not None and local_size(dest) == item.size:
        return TransferResult(item, "skipped", bytes_done=item.size)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_with_buffer(source, dest, buffer_mb * 1024 * 1024)
        shutil.copystat(source, dest, follow_symlinks=True)
        if item.size is not None and local_size(dest) != item.size:
            return TransferResult(item, "failed", "size mismatch after copy")
        return TransferResult(item, "copied", bytes_done=item.size or 0)
    except Exception as exc:  # noqa: BLE001 - report every per-file failure and continue.
        return TransferResult(item, "failed", str(exc))


def copy_with_buffer(source: Path, dest: Path, buffer_size: int) -> None:
    with source.open("rb", buffering=0) as src_file:
        with dest.open("wb", buffering=0) as dst_file:
            shutil.copyfileobj(src_file, dst_file, length=buffer_size)


def adb_pull_file(adb: str, serial: str | None, item: TransferItem, force: bool) -> TransferResult:
    dest = Path(item.dest)
    if not force and dest.exists() and item.size is not None and local_size(dest) == item.size:
        if item.mtime is not None:
            try:
                os.utime(dest, (item.mtime, item.mtime))
            except OSError:
                pass
        return TransferResult(item, "skipped", bytes_done=item.size)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = adb_base(adb, serial) + ["pull", "-a", item.source, str(dest)]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            return TransferResult(item, "failed", (result.stderr or result.stdout).strip())
        if item.size is not None and local_size(dest) != item.size:
            return TransferResult(item, "failed", "size mismatch after pull")
        if item.mtime is not None:
            try:
                os.utime(dest, (item.mtime, item.mtime))
            except OSError:
                pass
        return TransferResult(item, "copied", bytes_done=item.size or 0)
    except Exception as exc:  # noqa: BLE001
        return TransferResult(item, "failed", str(exc))


def adb_push_file(
    adb: str,
    serial: str | None,
    item: TransferItem,
    force: bool,
    remote_sizes: dict[str, int],
    verify: bool,
) -> TransferResult:
    if not force and item.size is not None and remote_sizes.get(item.rel) == item.size:
        return TransferResult(item, "skipped", bytes_done=item.size)

    try:
        cmd = adb_base(adb, serial) + ["push", item.source, item.dest]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            return TransferResult(item, "failed", (result.stderr or result.stdout).strip())
        if verify and item.size is not None:
            remote_size = adb_remote_size(adb, serial, item.dest)
            if remote_size != item.size:
                return TransferResult(item, "failed", f"remote size mismatch: expected {item.size}, got {remote_size}")
        return TransferResult(item, "copied", bytes_done=item.size or 0)
    except Exception as exc:  # noqa: BLE001
        return TransferResult(item, "failed", str(exc))


def adb_remote_size(adb: str, serial: str | None, remote_path: str) -> int | None:
    script = f"stat -c '%s' {sh_quote(remote_path)} 2>/dev/null || true"
    result = subprocess.run(
        adb_base(adb, serial) + ["shell", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = result.stdout.strip().splitlines()
    if not output:
        return None
    try:
        return int(output[-1])
    except ValueError:
        return None


def ensure_remote_root(adb: str, serial: str | None, remote: str) -> None:
    script = f"mkdir -p {sh_quote(remote_root_normalized(remote))}"
    try:
        run_checked(adb_base(adb, serial) + ["shell", script], capture=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise SystemExit(f"Could not create remote folder {remote!r}: {stderr or exc}") from exc


def ensure_remote_dirs(adb: str, serial: str | None, remote_root: str, rel_dirs: Iterable[str]) -> None:
    dirs = sorted({remote_join(remote_root, rel) for rel in rel_dirs if rel and rel != "."})
    if not dirs:
        return

    batch: list[str] = []
    batch_len = 0
    for directory in dirs:
        quoted = sh_quote(directory)
        if batch and batch_len + len(quoted) + 1 > 24000:
            run_mkdir_batch(adb, serial, batch)
            batch = []
            batch_len = 0
        batch.append(quoted)
        batch_len += len(quoted) + 1
    if batch:
        run_mkdir_batch(adb, serial, batch)


def run_mkdir_batch(adb: str, serial: str | None, quoted_dirs: Sequence[str]) -> None:
    script = "mkdir -p " + " ".join(quoted_dirs)
    try:
        run_checked(adb_base(adb, serial) + ["shell", script], capture=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise SystemExit(f"Could not create remote folders: {stderr or exc}") from exc


def validate_windows_paths(items: Iterable[TransferItem]) -> list[str]:
    bad: list[str] = []
    for item in items:
        parts = item.rel.split("/")
        if any(any(char in WINDOWS_INVALID_CHARS for char in part) for part in parts):
            bad.append(item.rel)
            if len(bad) >= 10:
                break
    return bad


def run_threaded(
    items: list[TransferItem],
    jobs: int,
    progress_interval: float,
    worker,
    *,
    dry_run: bool,
) -> int:
    total_bytes = sum(item.size for item in items if item.size is not None)
    total_bytes_value = total_bytes if any(item.size is not None for item in items) else None

    if dry_run:
        print(f"Dry run: {len(items)} files, {human_bytes(total_bytes)} planned.")
        for item in items[:20]:
            print(f"  {item.rel} -> {item.dest}")
        if len(items) > 20:
            print(f"  ...and {len(items) - 20} more")
        return 0

    progress = Progress(len(items), total_bytes_value, progress_interval)
    progress_thread = threading.Thread(target=progress.run, daemon=True)
    progress_thread.start()

    failures: queue.Queue[TransferResult] = queue.Queue()
    copied_or_skipped = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = [executor.submit(worker, item) for item in sort_for_transfer(items)]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                progress.add(result)
                if result.status == "failed":
                    failures.put(result)
                else:
                    copied_or_skipped += 1
    finally:
        progress.complete()
        progress_thread.join()

    if not failures.empty():
        print("\nFailures:")
        shown = 0
        while not failures.empty() and shown < 20:
            result = failures.get()
            print(f"  {result.item.rel}: {result.message}")
            shown += 1
        remaining = failures.qsize()
        if remaining:
            print(f"  ...and {remaining} more failures")
        return 1

    print(f"Finished {copied_or_skipped} files successfully.")
    return 0


def sort_for_transfer(items: list[TransferItem]) -> list[TransferItem]:
    return sorted(items, key=lambda item: item.size or 0, reverse=True)


def add_common_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--include", action="append", default=[], help="Only copy paths matching this glob. Can be repeated.")
    parser.add_argument("--exclude", action="append", default=[], help="Skip paths matching this glob. Can be repeated.")
    parser.add_argument("--jobs", type=int, default=4, help="Parallel transfers. Start with 4 for ADB, 8-16 for local disks.")
    parser.add_argument("--force", action="store_true", help="Overwrite even when the destination file has the same size.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied without transferring files.")
    parser.add_argument("--progress-interval", type=float, default=2.0, help="Seconds between progress updates.")


def add_adb_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adb", default="adb", help="Path to adb.exe, or just 'adb' if it is on PATH.")
    parser.add_argument("--serial", help="ADB device serial if more than one device is attached.")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Threaded transfer helper for large ROM folders.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    pull = subparsers.add_parser("adb-pull", help="Copy from Android/Retroid to this PC.")
    add_common_filters(pull)
    add_adb_options(pull)
    pull.add_argument("--remote", required=True, help="Remote Android folder, for example /sdcard/ROMs")
    pull.add_argument("--local", required=True, help="Local destination folder on this PC.")

    push = subparsers.add_parser("adb-push", help="Copy from this PC to Android/Retroid.")
    add_common_filters(push)
    add_adb_options(push)
    push.add_argument("--local", required=True, help="Local source folder on this PC.")
    push.add_argument("--remote", required=True, help="Remote Android destination folder, for example /sdcard/ROMs")
    push.add_argument("--verify", action="store_true", help="Check remote file size after each push. Slower, but safer.")
    push.add_argument(
        "--no-remote-scan",
        action="store_true",
        help="Do not scan destination first; useful when the remote folder is empty.",
    )

    local = subparsers.add_parser("local-copy", help="Copy between normal local folders/drives.")
    add_common_filters(local)
    local.add_argument("--source", required=True, help="Source folder.")
    local.add_argument("--dest", required=True, help="Destination folder.")
    local.add_argument("--buffer-mb", type=int, default=16, help="Copy buffer size per worker.")

    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if getattr(args, "buffer_mb", 16) < 1:
        parser.error("--buffer-mb must be at least 1")
    return args


def cmd_adb_pull(args: argparse.Namespace) -> int:
    check_adb(args.adb, args.serial)
    print(f"Scanning Retroid folder: {args.remote}")
    remote_items = apply_remote_filters(list_remote_files(args.adb, args.serial, args.remote), args.include, args.exclude)
    local_root = Path(args.local).resolve()
    bad_paths = validate_windows_paths(remote_items)
    if bad_paths:
        print("Some remote filenames are not valid Windows paths. Rename these on the Retroid first:")
        for path in bad_paths:
            print(f"  {path}")
        return 1
    items = with_dest(remote_items, lambda item: str(rel_to_local(local_root, item.rel)))
    print(f"Found {len(items)} files totaling {human_bytes(sum(item.size or 0 for item in items))}.")
    return run_threaded(
        items,
        args.jobs,
        args.progress_interval,
        lambda item: adb_pull_file(args.adb, args.serial, item, args.force),
        dry_run=args.dry_run,
    )


def cmd_adb_push(args: argparse.Namespace) -> int:
    check_adb(args.adb, args.serial)
    local_root = Path(args.local).resolve()
    print(f"Scanning PC folder: {local_root}")
    local_items = list_local_files(local_root, args.include, args.exclude)

    remote_sizes: dict[str, int] = {}
    if not args.force and not args.no_remote_scan:
        print(f"Scanning Retroid destination for files that can be skipped: {args.remote}")
        remote_items = list_remote_files(args.adb, args.serial, args.remote)
        remote_sizes = {item.rel: item.size for item in remote_items if item.size is not None}

    if not args.dry_run:
        print("Creating Retroid folders...")
        ensure_remote_root(args.adb, args.serial, args.remote)
        parent_dirs = {posixpath.dirname(item.rel) for item in local_items}
        ensure_remote_dirs(args.adb, args.serial, args.remote, parent_dirs)

    items = with_dest(local_items, lambda item: remote_join(args.remote, item.rel))
    print(f"Found {len(items)} files totaling {human_bytes(sum(item.size or 0 for item in items))}.")
    return run_threaded(
        items,
        args.jobs,
        args.progress_interval,
        lambda item: adb_push_file(args.adb, args.serial, item, args.force, remote_sizes, args.verify),
        dry_run=args.dry_run,
    )


def cmd_local_copy(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    dest = Path(args.dest).resolve()
    if source == dest:
        raise SystemExit("Source and destination are the same folder.")
    try:
        dest.relative_to(source)
    except ValueError:
        pass
    else:
        raise SystemExit("Destination is inside the source folder. Choose a separate destination folder.")
    print(f"Scanning local folder: {source}")
    source_items = list_local_files(source, args.include, args.exclude)
    items = with_dest(source_items, lambda item: str(rel_to_local(dest, item.rel)))
    print(f"Found {len(items)} files totaling {human_bytes(sum(item.size or 0 for item in items))}.")
    return run_threaded(
        items,
        args.jobs,
        args.progress_interval,
        lambda item: copy_local_file(item, args.force, args.buffer_mb),
        dry_run=args.dry_run,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.mode == "adb-pull":
        return cmd_adb_pull(args)
    if args.mode == "adb-push":
        return cmd_adb_push(args)
    if args.mode == "local-copy":
        return cmd_local_copy(args)
    raise SystemExit(f"Unknown mode: {args.mode}")


if __name__ == "__main__":
    raise SystemExit(main())
