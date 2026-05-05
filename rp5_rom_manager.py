#!/usr/bin/env python3
"""
Desktop GUI for fast_transfer.py.

The GUI intentionally reuses the same transfer engine as the command-line
tool, so the threaded copy behavior and all advanced options remain available.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import importlib.util
import os
import platform
import posixpath
import queue as queue_module
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Callable, Iterable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import fast_transfer as engine


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PLATFORM_TOOLS_PAGE = "https://developer.android.com/tools/releases/platform-tools"
PLATFORM_TOOLS_ZIP = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"


def load_checker_engine():
    checker_path = Path(__file__).with_name("rp5_sd_sync_checker.pyw")
    loader = SourceFileLoader("rp5_checker_engine", str(checker_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"Could not load {checker_path.name}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


try:
    checker = load_checker_engine()
    CHECKER_LOAD_ERROR = None
except Exception as exc:  # noqa: BLE001 - show the checker tab with a useful error.
    checker = None
    CHECKER_LOAD_ERROR = str(exc)


MODE_LABELS = {
    "adb-pull": "Retroid to PC",
    "adb-push": "PC to Retroid",
    "local-copy": "PC folder to PC folder",
}


@dataclass(frozen=True)
class TransferSettings:
    mode: str
    adb: str
    serial: str | None
    remote: str
    local: str
    source: str
    dest: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    jobs: int
    force: bool
    dry_run: bool
    progress_interval: float
    verify: bool
    no_remote_scan: bool
    buffer_mb: int
    log_every_file: bool


@dataclass(frozen=True)
class StorageChoice:
    kind: str
    label: str
    path: str
    recommended: bool = False


@dataclass(frozen=True)
class PreflightProfile:
    cpu_count: int
    cpu_name: str
    total_memory_bytes: int | None
    available_memory_bytes: int | None
    adb_jobs: int
    local_jobs: int
    buffer_mb: int
    pc_path: str
    pc_free_bytes: int | None
    pc_total_bytes: int | None
    device_ready: bool
    devices: tuple[str, ...]
    selected_serial: str | None
    storage_choices: tuple[StorageChoice, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class SpeedProbeResult:
    best_jobs: int
    best_mbps: float
    trials: tuple[tuple[int, float, int, int], ...]
    sample_files: int
    sample_bytes: int


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def windows_memory_status() -> tuple[int | None, int | None]:
    if os.name != "nt":
        return None, None
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return int(status.ullTotalPhys), int(status.ullAvailPhys)
    return None, None


def processor_name() -> str:
    name = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "")
    return " ".join(name.split()) or "CPU"


def recommend_jobs() -> tuple[int, int, int, int | None, int | None, str]:
    cpu_count = os.cpu_count() or 4
    total_memory, available_memory = windows_memory_status()

    memory_factor = 1.0
    if available_memory is not None:
        if available_memory < 2 * 1024**3:
            memory_factor = 0.45
        elif available_memory < 4 * 1024**3:
            memory_factor = 0.65
        elif available_memory < 8 * 1024**3:
            memory_factor = 0.85

    adb_ceiling = 12 if cpu_count >= 16 else 10 if cpu_count >= 12 else 8 if cpu_count >= 8 else 4
    adb_jobs = clamp(int(round(min(cpu_count, adb_ceiling) * memory_factor)), 2, adb_ceiling)

    local_ceiling = 32 if cpu_count >= 12 else 24 if cpu_count >= 8 else 16
    local_jobs = clamp(int(round(cpu_count * 2 * memory_factor)), 4, local_ceiling)

    if available_memory and available_memory >= 16 * 1024**3 and cpu_count >= 8:
        buffer_mb = 64
    elif available_memory and available_memory < 4 * 1024**3:
        buffer_mb = 8
    elif cpu_count >= 8:
        buffer_mb = 32
    else:
        buffer_mb = 16
    return adb_jobs, local_jobs, buffer_mb, total_memory, available_memory, processor_name()


def existing_path_anchor(raw_path: str) -> Path:
    if raw_path:
        path = Path(raw_path).expanduser()
        if path.exists():
            return path if path.is_dir() else path.parent
        for parent in path.parents:
            if parent.exists():
                return parent
    return Path.home()


def disk_usage_for(raw_path: str) -> tuple[str, int | None, int | None]:
    anchor = existing_path_anchor(raw_path)
    try:
        usage = shutil.disk_usage(anchor)
    except OSError:
        return str(anchor), None, None
    return str(anchor), usage.free, usage.total


def list_ready_adb_devices(adb: str) -> list[str]:
    result = subprocess.run(
        [adb, "devices"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        return []
    ready: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            ready.append(parts[0])
    return ready


def classify_storage_root(path: str) -> str:
    name = posixpath.basename(path.rstrip("/"))
    if path.endswith("/ROMs") or path.endswith("/Roms") or path.endswith("/roms"):
        return "Likely ROM folder"
    if path.startswith("/storage/") and "-" in name:
        return "Removable SD root"
    if path in {"/sdcard", "/storage/emulated/0", "/storage/self/primary"}:
        return "Internal shared storage"
    if path.startswith("/mnt/media_rw"):
        return "Media mount"
    return "Storage root"


def build_storage_choices(
    rom_roots: Iterable[engine.RemoteDirEntry],
    storage_roots: Iterable[engine.RemoteDirEntry],
    current_remote: str,
) -> tuple[StorageChoice, ...]:
    choices: list[StorageChoice] = []
    seen: set[str] = set()

    def add(path: str, kind: str | None = None, label: str | None = None, recommended: bool = False) -> None:
        normalized = engine.remote_root_normalized(path)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        choices.append(
            StorageChoice(
                kind=kind or classify_storage_root(normalized),
                label=label or posixpath.basename(normalized.rstrip("/")) or normalized,
                path=normalized,
                recommended=recommended,
            )
        )

    for index, entry in enumerate(rom_roots):
        add(entry.path, "Likely ROM folder", entry.name, recommended=index == 0)
    for entry in storage_roots:
        add(entry.path, classify_storage_root(entry.path), entry.name, recommended=False)
    if current_remote:
        add(current_remote, "Current path", posixpath.basename(current_remote.rstrip("/")) or current_remote)
    return tuple(choices)


class EventBus:
    def __init__(self, target: queue_module.Queue) -> None:
        self.target = target

    def emit(self, event: str, **payload) -> None:
        self.target.put((event, payload))


class CheckerEventWindow:
    def __init__(self, target: queue_module.Queue) -> None:
        self.target = target

    def write_event_value(self, event: str, value) -> None:
        self.target.put(("checker_event", {"event": event, "value": value}))


class TransferWorker:
    def __init__(
        self,
        settings: TransferSettings,
        events: EventBus,
        stop_event: threading.Event,
    ) -> None:
        self.settings = settings
        self.events = events
        self.stop_event = stop_event
        self.active_lock = threading.Lock()
        self.active_slots = 0
        self.active_adb = 0
        self.submitted_items = 0
        self.total_items = 0

    def run(self) -> None:
        try:
            items, worker = self.prepare_transfer()
            self.total_items = len(items)
            total_bytes = sum(item.size or 0 for item in items)
            has_known_sizes = any(item.size is not None for item in items)
            self.events.emit(
                "plan",
                files=len(items),
                bytes=total_bytes,
                has_known_sizes=has_known_sizes,
            )

            if self.settings.dry_run:
                self.emit_dry_run(items, total_bytes)
                self.events.emit("done", code=0, stopped=False)
                return

            code = self.execute_items(items, worker)
            self.events.emit("done", code=code, stopped=self.stop_event.is_set())
        except SystemExit as exc:
            self.events.emit("error", message=str(exc) or "The transfer could not continue.")
        except Exception as exc:  # noqa: BLE001 - surface unexpected failures to the UI.
            details = "".join(traceback.format_exception(exc))
            self.events.emit("error", message=str(exc), details=details)

    def prepare_transfer(self) -> tuple[list[engine.TransferItem], Callable[[engine.TransferItem], engine.TransferResult]]:
        if self.settings.mode == "adb-pull":
            return self.prepare_adb_pull()
        if self.settings.mode == "adb-push":
            return self.prepare_adb_push()
        if self.settings.mode == "local-copy":
            return self.prepare_local_copy()
        raise RuntimeError(f"Unknown mode: {self.settings.mode}")

    def prepare_adb_pull(self) -> tuple[list[engine.TransferItem], Callable[[engine.TransferItem], engine.TransferResult]]:
        self.events.emit("phase", message="Checking Retroid connection...")
        engine.check_adb(self.settings.adb, self.settings.serial)

        self.events.emit("phase", message=f"Scanning Retroid folder: {self.settings.remote}")
        remote_items = engine.apply_remote_filters(
            engine.list_remote_files(self.settings.adb, self.settings.serial, self.settings.remote),
            self.settings.include,
            self.settings.exclude,
        )
        bad_paths = engine.validate_windows_paths(remote_items)
        if bad_paths:
            lines = "\n".join(f"  {path}" for path in bad_paths)
            raise RuntimeError(
                "Some Retroid filenames are not valid Windows paths. Rename these first:\n" + lines
            )

        local_root = Path(self.settings.local).resolve()
        items = engine.with_dest(remote_items, lambda item: str(engine.rel_to_local(local_root, item.rel)))
        self.events.emit("phase", message=f"Ready to pull {len(items)} files.")
        return items, self.adb_pull_worker

    def prepare_adb_push(self) -> tuple[list[engine.TransferItem], Callable[[engine.TransferItem], engine.TransferResult]]:
        self.events.emit("phase", message="Checking Retroid connection...")
        engine.check_adb(self.settings.adb, self.settings.serial)

        local_root = Path(self.settings.local).resolve()
        self.events.emit("phase", message=f"Scanning PC folder: {local_root}")
        local_items = engine.list_local_files(local_root, self.settings.include, self.settings.exclude)

        remote_sizes: dict[str, int] = {}
        if not self.settings.force and not self.settings.no_remote_scan:
            self.events.emit("phase", message=f"Scanning Retroid destination: {self.settings.remote}")
            remote_items = engine.list_remote_files(self.settings.adb, self.settings.serial, self.settings.remote)
            remote_sizes = {item.rel: item.size for item in remote_items if item.size is not None}

        if not self.settings.dry_run:
            self.events.emit("phase", message="Creating Retroid folders...")
            engine.ensure_remote_root(self.settings.adb, self.settings.serial, self.settings.remote)
            parent_dirs = {posixpath.dirname(item.rel) for item in local_items}
            engine.ensure_remote_dirs(self.settings.adb, self.settings.serial, self.settings.remote, parent_dirs)

        items = engine.with_dest(local_items, lambda item: engine.remote_join(self.settings.remote, item.rel))
        self.events.emit("phase", message=f"Ready to push {len(items)} files.")
        self.remote_sizes = remote_sizes
        return items, self.adb_push_worker

    def prepare_local_copy(self) -> tuple[list[engine.TransferItem], Callable[[engine.TransferItem], engine.TransferResult]]:
        source = Path(self.settings.source).resolve()
        dest = Path(self.settings.dest).resolve()
        if source == dest:
            raise RuntimeError("Source and destination are the same folder.")
        try:
            dest.relative_to(source)
        except ValueError:
            pass
        else:
            raise RuntimeError("Destination is inside the source folder. Choose a separate destination folder.")

        self.events.emit("phase", message=f"Scanning local folder: {source}")
        source_items = engine.list_local_files(source, self.settings.include, self.settings.exclude)
        items = engine.with_dest(source_items, lambda item: str(engine.rel_to_local(dest, item.rel)))
        self.events.emit("phase", message=f"Ready to copy {len(items)} files.")
        return items, self.local_copy_worker

    def adb_pull_worker(self, item: engine.TransferItem) -> engine.TransferResult:
        dest = Path(item.dest)
        if not self.settings.force and dest.exists() and item.size is not None and engine.local_size(dest) == item.size:
            if item.mtime is not None:
                try:
                    os.utime(dest, (item.mtime, item.mtime))
                except OSError:
                    pass
            return engine.TransferResult(item, "skipped", bytes_done=item.size)
        self.bump_active_adb(1)
        try:
            return engine.adb_pull_file(self.settings.adb, self.settings.serial, item, True)
        finally:
            self.bump_active_adb(-1)

    def adb_push_worker(self, item: engine.TransferItem) -> engine.TransferResult:
        remote_sizes = getattr(self, "remote_sizes", {})
        if not self.settings.force and item.size is not None and remote_sizes.get(item.rel) == item.size:
            return engine.TransferResult(item, "skipped", bytes_done=item.size)
        self.bump_active_adb(1)
        try:
            return engine.adb_push_file(
                self.settings.adb,
                self.settings.serial,
                item,
                True,
                remote_sizes,
                self.settings.verify,
            )
        finally:
            self.bump_active_adb(-1)

    def local_copy_worker(self, item: engine.TransferItem) -> engine.TransferResult:
        return engine.copy_local_file(item, self.settings.force, self.settings.buffer_mb)

    def bump_active_adb(self, delta: int) -> None:
        with self.active_lock:
            self.active_adb = max(0, self.active_adb + delta)
            self.emit_worker_update_locked()

    def bump_active_slot(self, delta: int) -> None:
        with self.active_lock:
            self.active_slots = max(0, self.active_slots + delta)
            self.emit_worker_update_locked()

    def emit_worker_update_locked(self) -> None:
        self.events.emit(
            "workers",
            active_slots=self.active_slots,
            active_adb=self.active_adb,
            jobs=self.settings.jobs,
            submitted=self.submitted_items,
            total=self.total_items,
        )

    def emit_dry_run(self, items: list[engine.TransferItem], total_bytes: int) -> None:
        self.events.emit(
            "log",
            message=f"Dry run: {len(items)} files, {engine.human_bytes(total_bytes)} planned.",
        )
        for item in items[:150]:
            self.events.emit("log", message=f"  {item.rel} -> {item.dest}")
        if len(items) > 150:
            self.events.emit("log", message=f"  ...and {len(items) - 150} more")

    def execute_items(
        self,
        items: list[engine.TransferItem],
        worker: Callable[[engine.TransferItem], engine.TransferResult],
    ) -> int:
        failures = 0
        submitted = 0
        sorted_items = engine.sort_for_transfer(items)
        item_iter = iter(sorted_items)
        pending: dict[concurrent.futures.Future, engine.TransferItem] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.settings.jobs) as executor:
            submitted = self.fill_pending(executor, item_iter, pending, submitted, worker)

            while pending:
                done, _ = concurrent.futures.wait(
                    pending,
                    timeout=0.25,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done:
                    continue

                for future in done:
                    item = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        result = engine.TransferResult(item, "failed", str(exc))
                    if result.status == "failed":
                        failures += 1
                    self.events.emit("result", result=result)

                submitted = self.fill_pending(executor, item_iter, pending, submitted, worker)

        if self.stop_event.is_set():
            return 2 if failures == 0 else 1
        return 1 if failures else 0

    def fill_pending(
        self,
        executor: concurrent.futures.ThreadPoolExecutor,
        item_iter: Iterable[engine.TransferItem],
        pending: dict[concurrent.futures.Future, engine.TransferItem],
        submitted: int,
        worker: Callable[[engine.TransferItem], engine.TransferResult],
    ) -> int:
        while len(pending) < self.settings.jobs and not self.stop_event.is_set():
            try:
                item = next(item_iter)
            except StopIteration:
                return submitted
            future = executor.submit(self.safe_worker_call, worker, item)
            pending[future] = item
            submitted += 1
            with self.active_lock:
                self.submitted_items = submitted
                self.emit_worker_update_locked()
        return submitted

    def safe_worker_call(
        self,
        worker: Callable[[engine.TransferItem], engine.TransferResult],
        item: engine.TransferItem,
    ) -> engine.TransferResult:
        if self.stop_event.is_set():
            return engine.TransferResult(item, "skipped", "Stopped before this file started.", 0)
        self.bump_active_slot(1)
        try:
            return worker(item)
        finally:
            self.bump_active_slot(-1)


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event=None) -> None:
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.tip,
            text=self.text,
            justify="left",
            background="#111827",
            foreground="#f9fafb",
            padx=10,
            pady=7,
            relief="solid",
            borderwidth=1,
            wraplength=320,
        )
        label.pack()

    def hide(self, _event=None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None


class FastTransferGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("RP5 ROM Manager")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(1120, max(900, screen_w - 90))
        height = min(720, max(560, screen_h - 120))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(860, width), min(540, height))

        self.events: queue_module.Queue = queue_module.Queue()
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.transfer_started_at: float | None = None
        self.transfer_finished_at: float | None = None
        self.live_interval_ms = 500

        self.total_files = 0
        self.total_bytes = 0
        self.has_known_sizes = True
        self.done_files = 0
        self.copied_files = 0
        self.skipped_files = 0
        self.failed_files = 0
        self.copied_bytes = 0
        self.skipped_bytes = 0

        checker_config = checker.config if checker is not None else {}
        default_adb = checker_config.get("adb_exe", "") or "adb"
        default_serial = checker_config.get("adb_device", "")
        default_remote = checker_config.get("adb_root", "") or "/sdcard/ROMs"
        default_local = checker_config.get("dest_folder", "") or str(Path.home() / "Retroid ROM Backup")

        self.mode_var = tk.StringVar(value="adb-pull")
        self.adb_var = tk.StringVar(value=default_adb)
        self.serial_var = tk.StringVar(value=default_serial)
        self.remote_var = tk.StringVar(value=default_remote)
        self.local_var = tk.StringVar(value=default_local)
        self.source_var = tk.StringVar(value=checker_config.get("source_folder", ""))
        self.dest_var = tk.StringVar(value=default_local)
        self.remote_browser_path_var = tk.StringVar(value="/sdcard")
        self.remote_browser_status_var = tk.StringVar(value="Load a Retroid folder to browse.")
        self.jobs_var = tk.StringVar(value="4")
        self.force_var = tk.BooleanVar(value=False)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.progress_interval_var = tk.StringVar(value="2.0")
        self.verify_var = tk.BooleanVar(value=False)
        self.no_remote_scan_var = tk.BooleanVar(value=False)
        self.buffer_mb_var = tk.StringVar(value="16")
        self.log_every_file_var = tk.BooleanVar(value=False)

        self.status_var = tk.StringVar(value="Ready.")
        self.files_var = tk.StringVar(value="0 / 0 files")
        self.bytes_var = tk.StringVar(value="0 B")
        self.speed_var = tk.StringVar(value="0 B/s")
        self.counts_var = tk.StringVar(value="Copied 0 | Skipped 0 | Failed 0")
        self.workers_var = tk.StringVar(value="Workers 0/0 | ADB 0 | Queue 0/0")

        self.checker_worker_thread: threading.Thread | None = None
        self.checker_busy = False
        self.checker_issues: list[dict] = []
        self.checker_visible_issues: list[dict] = []
        self.checker_counts: dict | None = None
        self.checker_scan_signature: str | None = None
        self.checker_last_scan_settings: dict | None = None

        self.checker_source_mode_var = tk.StringVar(
            value=checker_config.get("source_mode", checker.MODE_ADB if checker is not None else "ADB (USB)")
        )
        self.checker_source_folder_var = tk.StringVar(value=checker_config.get("source_folder", ""))
        self.checker_dest_folder_var = tk.StringVar(value=default_local)
        self.checker_ftp_hostport_var = tk.StringVar(value=checker_config.get("ftp_hostport", ""))
        self.checker_ftp_root_var = tk.StringVar(value=checker_config.get("ftp_root", "/"))
        self.checker_ftp_username_var = tk.StringVar(value=checker_config.get("ftp_username", ""))
        self.checker_ftp_password_var = tk.StringVar(value=checker_config.get("ftp_password", ""))
        self.checker_adb_exe_var = tk.StringVar(value=default_adb)
        self.checker_adb_device_var = tk.StringVar(value=default_serial)
        self.checker_adb_root_var = tk.StringVar(value=default_remote)
        self.checker_thorough_var = tk.BooleanVar(value=bool(checker_config.get("thorough", False)))
        self.checker_filter_var = tk.StringVar(value=checker_config.get("filter", "All"))
        self.checker_status_var = tk.StringVar(value="Ready.")
        self.checker_summary_var = tk.StringVar(value="No scan yet.")
        self.checker_progress_text_var = tk.StringVar(value="Idle")
        self.checker_issue_count_var = tk.StringVar(value="Showing 0 issue(s)")

        self.include_text: tk.Text
        self.exclude_text: tk.Text
        self.command_text: tk.Text
        self.log_text: tk.Text
        self.progress: ttk.Progressbar
        self.remote_browser_frame: ttk.Frame
        self.remote_browser_listbox: tk.Listbox
        self.remote_browser_entries: list[engine.RemoteDirEntry] = []
        self.remote_browser_buttons: list[ttk.Button] = []
        self.remote_browser_busy = False
        self.tooltips: list[Tooltip] = []
        self.preflight_thread: threading.Thread | None = None
        self.preflight_busy = False
        self.speed_probe_thread: threading.Thread | None = None
        self.speed_probe_busy = False
        self.adb_install_thread: threading.Thread | None = None
        self.adb_install_busy = False
        self.preflight_profile: PreflightProfile | None = None
        self.pending_after_preflight: str | None = None
        self.storage_choices: list[StorageChoice] = []
        self.simple_mode_var = tk.BooleanVar(value=False)
        self.auto_tune_var = tk.BooleanVar(value=True)
        self.preflight_status_var = tk.StringVar(value="Preflight will check the RP5, folders, storage roots, and job recommendations.")
        self.preflight_jobs_var = tk.StringVar(value="Recommended jobs: waiting.")
        self.preflight_pc_var = tk.StringVar(value="PC folder: waiting.")
        self.preflight_device_var = tk.StringVar(value="Device: waiting.")
        self.speed_probe_var = tk.StringVar(value="Speed probe: not run yet.")
        self.preflight_tree: ttk.Treeview
        self.preflight_buttons: list[ttk.Button] = []
        self.checker_source_panels: dict[str, ttk.Frame] = {}
        self.checker_action_buttons: list[ttk.Button] = []
        self.checker_issue_tree: ttk.Treeview
        self.checker_log_text: tk.Text
        self.checker_progress: ttk.Progressbar
        self.checker_device_combo: ttk.Combobox

        self.build_style()
        self.build_ui()
        self.bind_updates()
        self.refresh_mode()
        self.refresh_command_preview()
        self.root.after(100, self.process_events)
        self.root.after(500, self.tick_live_stats)
        self.root.after(350, self.start_preflight_scan)

    def build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self.bg = "#eef2f7"
        self.panel = "#ffffff"
        self.panel_soft = "#f8fafc"
        self.text = "#0f172a"
        self.muted = "#64748b"
        self.accent = "#2563eb"
        self.accent_dark = "#1d4ed8"
        self.good = "#059669"
        self.bad = "#dc2626"

        self.root.configure(background=self.bg)
        style.configure(".", font=("Segoe UI", 10), background=self.bg, foreground=self.text)
        style.configure("TFrame", background=self.bg)
        style.configure("Card.TFrame", background=self.panel, relief="flat")
        style.configure("Soft.TFrame", background=self.panel_soft)
        style.configure("TLabel", background=self.bg, foreground=self.text)
        style.configure("Card.TLabel", background=self.panel, foreground=self.text)
        style.configure("Muted.Card.TLabel", background=self.panel, foreground=self.muted)
        style.configure("Hero.TLabel", background=self.bg, foreground=self.text, font=("Segoe UI Semibold", 18))
        style.configure("Subhero.TLabel", background=self.bg, foreground=self.muted)
        style.configure("Section.Card.TLabel", background=self.panel, font=("Segoe UI Semibold", 12))
        style.configure("Stat.Card.TLabel", background=self.panel, font=("Segoe UI Semibold", 11))
        style.configure("TButton", padding=(12, 8))
        style.configure("Accent.TButton", background=self.accent, foreground="#ffffff", borderwidth=0)
        style.map("Accent.TButton", background=[("active", self.accent_dark), ("disabled", "#93c5fd")])
        style.configure("Mode.TRadiobutton", background=self.panel, foreground=self.text, padding=(10, 7))
        style.configure("TCheckbutton", background=self.panel, foreground=self.text)
        style.configure("Horizontal.TProgressbar", troughcolor="#dbe4f0", background=self.good, bordercolor="#dbe4f0")
        style.configure("TLabelframe", background=self.panel)
        style.configure("TLabelframe.Label", background=self.panel, foreground=self.text, font=("Segoe UI Semibold", 11))
        style.configure("TNotebook", background=self.bg, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 9), font=("Segoe UI Semibold", 10))
        style.configure("Treeview", rowheight=24, fieldbackground="#f8fafc", background="#f8fafc", foreground=self.text)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="RP5 ROM Manager", style="Hero.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Fast threaded transfers plus scan, compare, and repair tools for your Retroid library.",
            style="Subhero.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.simple_mode_button = ttk.Button(header, text="Simple Mode", command=self.toggle_simple_mode)
        self.simple_mode_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(14, 0))
        self.add_tip(
            self.simple_mode_button,
            "Switches between the full advanced layout and a calmer simple layout. Simple mode hides expert tuning, command previews, filters, and raw logs while keeping the main transfer and checker actions available.",
        )

        self.build_profile_strip(outer)

        notebook = ttk.Notebook(outer)
        self.notebook = notebook
        notebook.pack(fill="both", expand=True)
        setup_tab = ttk.Frame(notebook)
        transfer_tab = ttk.Frame(notebook)
        checker_tab = ttk.Frame(notebook)
        notebook.add(setup_tab, text="Setup")
        notebook.add(transfer_tab, text="Fast Transfer")
        notebook.add(checker_tab, text="Sync Checker")

        setup_body = self.make_scroll_body(setup_tab, padding=(0, 10, 0, 0))
        self.build_preflight_panel(setup_body)

        body = self.make_scroll_body(transfer_tab, padding=(0, 10, 0, 0))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)

        left = ttk.Frame(body, style="Card.TFrame", padding=18)
        left.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 12))
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(body, style="Card.TFrame", padding=18)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)

        self.transfer_log_card = ttk.Frame(body, style="Card.TFrame", padding=14)
        self.transfer_log_card.grid(row=1, column=1, sticky="nsew", pady=(12, 0))
        self.transfer_log_card.columnconfigure(0, weight=1)
        self.transfer_log_card.rowconfigure(1, weight=1)

        self.build_mode_section(left)
        self.build_path_section(left)
        self.build_remote_browser_section(left)
        self.build_filter_section(left)
        self.build_options_section(right)
        self.build_progress_section(right)
        self.build_log_section(self.transfer_log_card)
        self.build_checker_tab(checker_tab)
        self.apply_simple_mode()

    def make_scroll_body(self, parent: ttk.Frame, padding=(0, 0, 0, 0)) -> ttk.Frame:
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        canvas = tk.Canvas(container, background=self.bg, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas, padding=padding)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")

        def sync_scroll_region(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def sync_width(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        def wheel(event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        body.bind("<Configure>", sync_scroll_region)
        canvas.bind("<Configure>", sync_width)
        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        return body

    def add_tip(self, widget: tk.Widget, text: str) -> tk.Widget:
        self.tooltips.append(Tooltip(widget, text))
        return widget

    def build_profile_strip(self, parent: ttk.Frame) -> None:
        strip = ttk.Frame(parent, style="Card.TFrame", padding=(12, 8))
        strip.pack(fill="x", pady=(0, 10))
        strip.columnconfigure(0, weight=2)
        strip.columnconfigure(1, weight=1)
        strip.columnconfigure(2, weight=1)
        strip.columnconfigure(3, weight=1)
        ttk.Label(strip, textvariable=self.preflight_status_var, style="Muted.Card.TLabel", wraplength=460).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 12),
        )
        ttk.Label(strip, textvariable=self.preflight_device_var, style="Stat.Card.TLabel", wraplength=240).grid(
            row=0,
            column=1,
            sticky="w",
            padx=(0, 12),
        )
        ttk.Label(strip, textvariable=self.preflight_jobs_var, style="Stat.Card.TLabel", wraplength=260).grid(
            row=0,
            column=2,
            sticky="w",
            padx=(0, 12),
        )
        ttk.Label(strip, textvariable=self.speed_probe_var, style="Stat.Card.TLabel", wraplength=260).grid(
            row=0,
            column=3,
            sticky="w",
            padx=(0, 12),
        )
        setup_button = ttk.Button(strip, text="Setup", command=self.show_setup_tab)
        setup_button.grid(row=0, column=4, sticky="e", padx=(0, 8))
        self.add_tip(setup_button, "Opens the Setup tab with storage choices, ADB install, USB help, and preflight details.")
        run_button = ttk.Button(strip, text="Preflight", command=self.start_preflight_scan)
        run_button.grid(row=0, column=5, sticky="e")
        self.add_tip(run_button, "Runs the shared setup scan again without leaving the current tab.")
        self.profile_strip_buttons = [setup_button, run_button]
        self.add_tip(
            strip,
            "Compact status for the shared Library Profile. The full Setup tab has detected storage choices and help actions.",
        )

    def show_setup_tab(self) -> None:
        if hasattr(self, "notebook"):
            self.notebook.select(0)

    def toggle_simple_mode(self) -> None:
        self.simple_mode_var.set(not self.simple_mode_var.get())
        self.apply_simple_mode()

    def apply_simple_mode(self) -> None:
        simple = self.simple_mode_var.get()
        if hasattr(self, "simple_mode_button"):
            self.simple_mode_button.configure(text="Switch to Advanced" if simple else "Simple Mode")
        if not hasattr(self, "transfer_filters_container"):
            return

        if simple:
            self.transfer_filters_container.pack_forget()
            self.transfer_options_label.grid_remove()
            self.transfer_options_frame.grid_remove()
            self.transfer_command_label.grid_remove()
            self.transfer_command_frame.grid_remove()
            self.transfer_log_card.grid_remove()
            if hasattr(self, "checker_log_card"):
                self.checker_log_card.grid_remove()
            self.preflight_status_var.set(
                "Simple Mode is on. Core actions stay visible; advanced filters, tuning, command preview, and raw logs are hidden."
            )
        else:
            self.transfer_filters_container.pack(fill="both", expand=False, pady=(0, 0))
            self.transfer_options_label.grid()
            self.transfer_options_frame.grid()
            self.transfer_command_label.grid()
            self.transfer_command_frame.grid()
            self.transfer_log_card.grid(row=1, column=1, sticky="nsew", pady=(12, 0))
            if hasattr(self, "checker_log_card"):
                self.checker_log_card.grid(row=1, column=1, sticky="nsew")
            if self.preflight_profile is None:
                self.preflight_status_var.set("Advanced Mode is on. Full controls, logs, filters, and command preview are visible.")

    def build_preflight_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Card.TFrame", padding=14)
        panel.pack(fill="x", pady=(0, 14))
        panel.columnconfigure(0, weight=1)
        panel.columnconfigure(1, weight=2)

        status = ttk.Frame(panel, style="Card.TFrame")
        status.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        status.columnconfigure(0, weight=1)
        ttk.Label(status, text="Library Profile", style="Section.Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(status, textvariable=self.preflight_status_var, style="Muted.Card.TLabel", wraplength=360).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(8, 0),
        )
        ttk.Label(status, textvariable=self.preflight_device_var, style="Stat.Card.TLabel", wraplength=360).grid(
            row=2,
            column=0,
            sticky="w",
            pady=(8, 0),
        )
        ttk.Label(status, textvariable=self.preflight_jobs_var, style="Stat.Card.TLabel", wraplength=360).grid(
            row=3,
            column=0,
            sticky="w",
            pady=(4, 0),
        )
        ttk.Label(status, textvariable=self.preflight_pc_var, style="Stat.Card.TLabel", wraplength=360).grid(
            row=4,
            column=0,
            sticky="w",
            pady=(4, 0),
        )
        ttk.Checkbutton(
            status,
            text="Use preflight job recommendation",
            variable=self.auto_tune_var,
            command=self.apply_preflight_recommendations,
        ).grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.add_tip(
            status,
            "Preflight is the shared setup scan. It checks your connected RP5, storage roots, PC free space, CPU count, and recommended copy settings before transfers or checker scans run.",
        )

        buttons = ttk.Frame(status, style="Card.TFrame")
        buttons.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        buttons.columnconfigure((0, 1), weight=1)
        run_button = ttk.Button(buttons, text="Run Preflight", style="Accent.TButton", command=self.start_preflight_scan)
        run_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.add_tip(run_button, "Runs the shared setup scan again. Use this after changing USB cables, reconnecting the RP5, changing your PC backup folder, or installing ADB.")
        both_button = ttk.Button(buttons, text="Use Selected for Both", command=lambda: self.apply_selected_storage_choice("both"))
        both_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.add_tip(both_button, "Applies the selected Retroid storage path to both Fast Transfer and Sync Checker so both tabs point at the same ROM folder.")
        transfer_button = ttk.Button(buttons, text="Use for Transfer", command=lambda: self.apply_selected_storage_choice("transfer"))
        transfer_button.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(8, 0))
        self.add_tip(transfer_button, "Uses the selected Retroid path only for the Fast Transfer tab. Useful when you want to copy files without changing checker settings.")
        checker_button = ttk.Button(buttons, text="Use for Checker", command=lambda: self.apply_selected_storage_choice("checker"))
        checker_button.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(8, 0))
        self.add_tip(checker_button, "Uses the selected Retroid path only for the Sync Checker tab. Useful when you want to compare a specific ROM folder.")
        browse_button = ttk.Button(buttons, text="Browse Selected", command=self.browse_selected_storage_choice)
        browse_button.grid(row=2, column=0, sticky="ew", padx=(0, 6), pady=(8, 0))
        self.add_tip(browse_button, "Opens the selected Retroid path in the Retroid Browser so you can drill into subfolders before choosing a final path.")
        scan_button = ttk.Button(buttons, text="Scan Selected", command=self.scan_selected_storage_choice)
        scan_button.grid(row=2, column=1, sticky="ew", padx=(6, 0), pady=(8, 0))
        self.add_tip(scan_button, "Applies the selected path to Sync Checker and immediately starts a compare scan against your PC backup folder.")
        adb_button = ttk.Button(buttons, text="Install ADB", command=self.install_adb_here)
        adb_button.grid(row=3, column=0, sticky="ew", padx=(0, 6), pady=(8, 0))
        self.add_tip(adb_button, "Downloads the official Windows SDK Platform-Tools from Google, extracts adb.exe into this app folder, fills in the ADB fields, then reruns preflight.")
        usb_button = ttk.Button(buttons, text="USB Debugging Help", command=self.show_usb_debugging_help)
        usb_button.grid(row=3, column=1, sticky="ew", padx=(6, 0), pady=(8, 0))
        self.add_tip(usb_button, "Shows the RP5 steps for enabling Developer Options, turning on USB debugging, and accepting the trust prompt so ADB can see the device.")
        tune_button = ttk.Button(buttons, text="Tune Speed", command=self.start_speed_probe)
        tune_button.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.add_tip(
            tune_button,
            "Benchmarks several ADB worker counts using a small temporary copy, then applies the fastest jobs setting. This is the best way to know whether more ADB processes actually improve your RP5 transfer rate.",
        )
        self.preflight_buttons.extend([run_button, both_button, transfer_button, checker_button, browse_button, scan_button, adb_button, usb_button, tune_button])

        chooser = ttk.Frame(panel, style="Card.TFrame")
        chooser.grid(row=0, column=1, sticky="nsew")
        chooser.columnconfigure(0, weight=1)
        chooser.rowconfigure(1, weight=1)
        ttk.Label(chooser, text="Detected Retroid Storage", style="Section.Card.TLabel").grid(row=0, column=0, sticky="w")
        columns = ("recommended", "kind", "path")
        self.preflight_tree = ttk.Treeview(chooser, columns=columns, show="headings", selectmode="browse", height=6)
        self.preflight_tree.heading("recommended", text="")
        self.preflight_tree.heading("kind", text="Type")
        self.preflight_tree.heading("path", text="Path")
        self.preflight_tree.column("recommended", width=34, minwidth=30, anchor="center")
        self.preflight_tree.column("kind", width=150, minwidth=120, anchor="w")
        self.preflight_tree.column("path", width=520, minwidth=260, anchor="w")
        self.preflight_tree.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.preflight_tree.bind("<Double-Button-1>", lambda _event: self.apply_selected_storage_choice("both"))
        scrollbar = ttk.Scrollbar(chooser, orient="vertical", command=self.preflight_tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns", pady=(8, 0))
        self.preflight_tree.configure(yscrollcommand=scrollbar.set)
        self.add_tip(
            self.preflight_tree,
            "Shows storage roots and likely ROM folders found on the RP5. Pick the ROMs folder, usually something like /storage/4A21-0000/ROMs, then apply it to Transfer, Checker, or both.",
        )
        ttk.Label(chooser, textvariable=self.speed_probe_var, style="Muted.Card.TLabel", wraplength=720).grid(
            row=2,
            column=0,
            sticky="w",
            pady=(8, 0),
        )
        self.update_preflight_buttons()

    def build_mode_section(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Transfer Mode", style="Section.Card.TLabel").pack(anchor="w")
        mode_frame = ttk.Frame(parent, style="Card.TFrame")
        mode_frame.pack(fill="x", pady=(10, 18))
        for value, label in MODE_LABELS.items():
            button = ttk.Radiobutton(
                mode_frame,
                text=label,
                value=value,
                variable=self.mode_var,
                style="Mode.TRadiobutton",
                command=self.refresh_mode,
            )
            button.pack(side="left", padx=(0, 8))
            descriptions = {
                "adb-pull": "Copies ROMs from the RP5 to your PC backup folder over USB with parallel workers.",
                "adb-push": "Copies ROMs from your PC backup folder back to the RP5 over USB. Use verify if you want extra safety.",
                "local-copy": "Copies between normal Windows folders, useful for card readers or moving backups between drives.",
            }
            self.add_tip(button, descriptions[value])

    def build_path_section(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Paths", style="Section.Card.TLabel").pack(anchor="w")
        self.path_frame = ttk.Frame(parent, style="Card.TFrame")
        self.path_frame.pack(fill="x", pady=(10, 18))
        self.path_frame.columnconfigure(1, weight=1)

    def build_remote_browser_section(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Retroid Browser", style="Section.Card.TLabel").pack(anchor="w")
        self.remote_browser_frame = ttk.Frame(parent, style="Card.TFrame")
        self.remote_browser_frame.pack(fill="both", expand=False, pady=(10, 18))
        self.remote_browser_frame.columnconfigure(1, weight=1)

        ttk.Label(self.remote_browser_frame, text="Current folder", style="Muted.Card.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 10),
            pady=(0, 8),
        )
        browser_entry = ttk.Entry(self.remote_browser_frame, textvariable=self.remote_browser_path_var)
        browser_entry.grid(row=0, column=1, sticky="ew", pady=(0, 8))
        browser_entry.bind("<Return>", lambda _event: self.load_remote_browser())
        load_button = ttk.Button(self.remote_browser_frame, text="Load", command=self.load_remote_browser)
        load_button.grid(row=0, column=2, sticky="ew", padx=(8, 0), pady=(0, 8))
        self.add_tip(load_button, "Loads the folder typed in Current folder so you can browse the RP5 path tree.")
        self.remote_browser_buttons.append(load_button)

        quick = ttk.Frame(self.remote_browser_frame, style="Card.TFrame")
        quick.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        quick.columnconfigure((0, 1, 2), weight=1)
        quick_buttons = [
            ("Find Storage", self.detect_remote_storage),
            ("Use Folder", self.use_remote_browser_folder),
            ("Up", self.open_remote_parent),
            ("/storage", lambda: self.jump_remote_browser("/storage")),
            ("/sdcard", lambda: self.jump_remote_browser("/sdcard")),
            ("/mnt/media_rw", lambda: self.jump_remote_browser("/mnt/media_rw")),
        ]
        for index, (label, command) in enumerate(quick_buttons):
            button = ttk.Button(quick, text=label, command=command)
            button.grid(
                row=index // 3,
                column=index % 3,
                sticky="ew",
                padx=(0 if index % 3 == 0 else 4, 0),
                pady=(0 if index < 3 else 4, 0),
            )
            self.remote_browser_buttons.append(button)
            tips = {
                "Find Storage": "Scans common Android storage locations and likely ROMs folders. This is how the app finds removable SD paths like /storage/4A21-0000.",
                "Use Folder": "Applies the currently shown or selected RP5 folder to the Fast Transfer path.",
                "Up": "Moves the Retroid Browser one folder up.",
                "/storage": "Jumps to Android's storage folder, where removable SD cards usually appear.",
                "/sdcard": "Jumps to internal shared storage. This may not be the removable SD card.",
                "/mnt/media_rw": "Jumps to another Android removable-media mount area. Useful if /storage does not show the SD card clearly.",
            }
            self.add_tip(button, tips[label])

        list_frame = ttk.Frame(self.remote_browser_frame, style="Card.TFrame")
        list_frame.grid(row=2, column=0, columnspan=3, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.remote_browser_listbox = tk.Listbox(
            list_frame,
            height=7,
            borderwidth=1,
            relief="solid",
            background="#f8fafc",
            foreground=self.text,
            selectbackground="#bfdbfe",
            selectforeground=self.text,
            activestyle="none",
            exportselection=False,
            font=("Segoe UI", 9),
        )
        self.remote_browser_listbox.grid(row=0, column=0, sticky="nsew")
        self.remote_browser_listbox.bind("<Double-Button-1>", lambda _event: self.open_selected_remote_folder())
        self.remote_browser_listbox.bind("<Return>", lambda _event: self.open_selected_remote_folder())
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.remote_browser_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.remote_browser_listbox.configure(yscrollcommand=scrollbar.set)

        status = ttk.Label(
            self.remote_browser_frame,
            textvariable=self.remote_browser_status_var,
            style="Muted.Card.TLabel",
        )
        status.grid(row=3, column=0, columnspan=3, sticky="w", pady=(7, 0))

        self.add_tip(
            self.remote_browser_listbox,
            "Double-click a folder to open it. Use Folder applies the current folder to the transfer path.",
        )

    def rebuild_path_fields(self) -> None:
        for child in self.path_frame.winfo_children():
            child.destroy()

        row = 0
        if self.mode_var.get() in {"adb-pull", "adb-push"}:
            row = self.add_entry_row(
                self.path_frame,
                row,
                "ADB",
                self.adb_var,
                browse=lambda: self.browse_file(self.adb_var, "Choose adb.exe"),
                help_text="Use 'adb' if Android Platform Tools is already on PATH.",
            )
            row = self.add_entry_row(
                self.path_frame,
                row,
                "Serial",
                self.serial_var,
                help_text="Only needed when more than one Android device is connected.",
            )

        if self.mode_var.get() == "adb-pull":
            row = self.add_entry_row(
                self.path_frame,
                row,
                "Retroid folder",
                self.remote_var,
                browse=self.browse_remote_from_transfer_path,
                help_text="Example: /sdcard/ROMs",
            )
            self.add_entry_row(
                self.path_frame,
                row,
                "PC destination",
                self.local_var,
                browse=lambda: self.browse_folder(self.local_var, "Choose PC destination"),
            )
        elif self.mode_var.get() == "adb-push":
            row = self.add_entry_row(
                self.path_frame,
                row,
                "PC source",
                self.local_var,
                browse=lambda: self.browse_folder(self.local_var, "Choose PC source"),
            )
            self.add_entry_row(
                self.path_frame,
                row,
                "Retroid folder",
                self.remote_var,
                browse=self.browse_remote_from_transfer_path,
                help_text="Example: /sdcard/ROMs",
            )
        else:
            row = self.add_entry_row(
                self.path_frame,
                row,
                "Source folder",
                self.source_var,
                browse=lambda: self.browse_folder(self.source_var, "Choose source folder"),
            )
            self.add_entry_row(
                self.path_frame,
                row,
                "Destination",
                self.dest_var,
                browse=lambda: self.browse_folder(self.dest_var, "Choose destination folder"),
            )

    def build_filter_section(self, parent: ttk.Frame) -> None:
        self.transfer_filters_container = ttk.Frame(parent, style="Card.TFrame")
        self.transfer_filters_container.pack(fill="both", expand=False, pady=(0, 0))
        ttk.Label(self.transfer_filters_container, text="Filters", style="Section.Card.TLabel").pack(anchor="w")
        filters = ttk.Frame(self.transfer_filters_container, style="Card.TFrame")
        filters.pack(fill="both", expand=False, pady=(10, 0))
        filters.columnconfigure(0, weight=1)
        filters.columnconfigure(1, weight=1)

        include_box = ttk.Frame(filters, style="Card.TFrame")
        include_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(include_box, text="Include globs", style="Muted.Card.TLabel").pack(anchor="w")
        self.include_text = self.make_text(include_box, height=5)
        self.include_text.pack(fill="both", expand=True, pady=(5, 0))
        self.add_tip(self.include_text, "Optional filter. Add one pattern per line to copy or scan only matching files, such as PS2/* or GC/*. Leave blank to include everything.")

        exclude_box = ttk.Frame(filters, style="Card.TFrame")
        exclude_box.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(exclude_box, text="Exclude globs", style="Muted.Card.TLabel").pack(anchor="w")
        self.exclude_text = self.make_text(exclude_box, height=5)
        self.exclude_text.pack(fill="both", expand=True, pady=(5, 0))
        self.add_tip(self.exclude_text, "Optional filter. Add one pattern per line to skip matching files or folders, such as cache/* or screenshots/*. This is useful for ignoring artwork caches or temp folders.")

    def build_options_section(self, parent: ttk.Frame) -> None:
        self.transfer_options_label = ttk.Label(parent, text="Options", style="Section.Card.TLabel")
        self.transfer_options_label.grid(row=0, column=0, sticky="w")
        options = ttk.Frame(parent, style="Card.TFrame")
        self.transfer_options_frame = options
        options.grid(row=1, column=0, sticky="ew", pady=(10, 18))
        options.columnconfigure(1, weight=1)

        self.add_spin_row(options, 0, "Parallel jobs", self.jobs_var, 1, 64, "Start with 4 for ADB, 8-16 for local disks.")
        self.add_spin_row(options, 1, "Update every", self.progress_interval_var, 0.25, 10, "Progress refresh interval in seconds.", increment=0.25)
        self.add_spin_row(options, 2, "Buffer MB", self.buffer_mb_var, 1, 256, "Local-copy buffer per worker.")

        checks = ttk.Frame(options, style="Card.TFrame")
        checks.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        checks.columnconfigure(0, weight=1)
        checks.columnconfigure(1, weight=1)

        self.force_check = ttk.Checkbutton(checks, text="Overwrite same-size files", variable=self.force_var, command=self.refresh_command_preview)
        self.force_check.grid(row=0, column=0, sticky="w", pady=2)
        self.add_tip(self.force_check, "Copies files even when the destination already has the same size. Useful when you suspect a same-size file might still be different.")
        self.dry_run_check = ttk.Checkbutton(checks, text="Dry run only", variable=self.dry_run_var, command=self.refresh_command_preview)
        self.dry_run_check.grid(row=0, column=1, sticky="w", pady=2)
        self.add_tip(self.dry_run_check, "Preview the transfer plan without copying anything. Good for checking paths, filters, and file counts before committing.")
        self.verify_check = ttk.Checkbutton(checks, text="Verify pushed file sizes", variable=self.verify_var, command=self.refresh_command_preview)
        self.verify_check.grid(row=1, column=0, sticky="w", pady=2)
        self.add_tip(self.verify_check, "After pushing files to the RP5, checks that the remote file size matches. Safer but slower.")
        self.no_scan_check = ttk.Checkbutton(checks, text="Skip Retroid destination scan", variable=self.no_remote_scan_var, command=self.refresh_command_preview)
        self.no_scan_check.grid(row=1, column=1, sticky="w", pady=2)
        self.add_tip(self.no_scan_check, "Skips scanning the RP5 destination before pushing. Faster when the target folder is empty, riskier when it may already contain files.")
        self.log_file_check = ttk.Checkbutton(checks, text="Log every file", variable=self.log_every_file_var)
        self.log_file_check.grid(row=2, column=0, sticky="w", pady=2)
        self.add_tip(self.log_file_check, "Writes each copied or skipped file into the log. Useful for auditing, but noisy for large libraries.")

        self.transfer_command_label = ttk.Label(parent, text="Command Preview", style="Section.Card.TLabel")
        self.transfer_command_label.grid(row=2, column=0, sticky="w")
        preview_frame = ttk.Frame(parent, style="Card.TFrame")
        self.transfer_command_frame = preview_frame
        preview_frame.grid(row=3, column=0, sticky="ew", pady=(10, 18))
        preview_frame.columnconfigure(0, weight=1)
        self.command_text = self.make_text(preview_frame, height=4, mono=True)
        self.command_text.grid(row=0, column=0, sticky="ew")
        self.command_text.configure(state="disabled")
        self.add_tip(self.command_text, "Shows the equivalent command-line transfer. You do not need this for normal use, but it is useful for troubleshooting or repeatable scripted copies.")
        copy_button = ttk.Button(preview_frame, text="Copy Command", command=self.copy_command)
        copy_button.grid(row=1, column=0, sticky="e", pady=(8, 0))
        self.add_tip(copy_button, "Copies the command preview to the clipboard so you can run or save the same transfer outside the GUI.")

    def build_progress_section(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Transfer", style="Section.Card.TLabel").grid(row=4, column=0, sticky="w")
        progress_frame = ttk.Frame(parent, style="Card.TFrame")
        progress_frame.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        progress_frame.columnconfigure(0, weight=1)

        ttk.Label(progress_frame, textvariable=self.status_var, style="Muted.Card.TLabel").grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(10, 10))

        stats = ttk.Frame(progress_frame, style="Card.TFrame")
        stats.grid(row=2, column=0, sticky="ew")
        stats.columnconfigure((0, 1), weight=1)
        ttk.Label(stats, textvariable=self.files_var, style="Stat.Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(stats, textvariable=self.bytes_var, style="Stat.Card.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(stats, textvariable=self.speed_var, style="Stat.Card.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(stats, textvariable=self.counts_var, style="Stat.Card.TLabel").grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(stats, textvariable=self.workers_var, style="Stat.Card.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.add_tip(
            stats,
            "Worker slots are the Python copy workers currently busy. ADB active is the number of live ADB copy processes; skipped files do not start ADB, and the RP5/ADB server may throttle transfers even when slots are available.",
        )

        buttons = ttk.Frame(progress_frame, style="Card.TFrame")
        buttons.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        buttons.columnconfigure((0, 1, 2), weight=1)
        self.check_button = ttk.Button(buttons, text="Check Device", command=self.check_device)
        self.check_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.add_tip(self.check_button, "Checks whether ADB can see the RP5 as a ready USB device. If it fails, enable USB debugging and accept the trust prompt on the RP5.")
        self.preview_button = ttk.Button(buttons, text="Preview", command=self.preview_transfer)
        self.preview_button.grid(row=0, column=1, sticky="ew", padx=6)
        self.add_tip(self.preview_button, "Builds the transfer plan without copying files. Use this to confirm file counts, filters, and paths.")
        self.start_button = ttk.Button(buttons, text="Start Transfer", style="Accent.TButton", command=self.start_transfer)
        self.start_button.grid(row=0, column=2, sticky="ew", padx=(6, 0))
        self.add_tip(self.start_button, "Starts the actual copy using the selected mode, paths, filters, and parallel job count.")
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop_transfer, state="disabled")
        self.stop_button.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.add_tip(self.stop_button, "Requests a graceful stop. Transfers already running finish first, then no new files are started.")

    def build_log_section(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="Log", style="Section.Card.TLabel").grid(row=0, column=0, sticky="w")
        save_button = ttk.Button(top, text="Save Log", command=self.save_log)
        save_button.grid(row=0, column=1, sticky="e")
        self.add_tip(save_button, "Saves the transfer log to a text file for troubleshooting or record keeping.")

        self.log_text = self.make_text(parent, height=12, mono=True)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")
        self.add_tip(self.log_text, "Shows detailed transfer messages, errors, and optional per-file activity. Hidden in Simple Mode to reduce noise.")

    def build_checker_tab(self, parent: ttk.Frame) -> None:
        body = self.make_scroll_body(parent, padding=(0, 10, 0, 0))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)

        if checker is None:
            card = ttk.Frame(body, style="Card.TFrame", padding=18)
            card.grid(row=0, column=0, sticky="nsew")
            ttk.Label(card, text="Sync Checker", style="Section.Card.TLabel").pack(anchor="w")
            ttk.Label(
                card,
                text=f"The checker engine could not load: {CHECKER_LOAD_ERROR}",
                style="Muted.Card.TLabel",
                wraplength=720,
            ).pack(anchor="w", pady=(10, 0))
            return

        settings_card = ttk.Frame(body, style="Card.TFrame", padding=18)
        settings_card.grid(row=0, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        settings_card.columnconfigure(0, weight=1)

        progress_card = ttk.Frame(body, style="Card.TFrame", padding=18)
        progress_card.grid(row=0, column=1, sticky="nsew", pady=(0, 12))
        progress_card.columnconfigure(0, weight=1)

        issues_card = ttk.Frame(body, style="Card.TFrame", padding=14)
        issues_card.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        issues_card.columnconfigure(0, weight=1)
        issues_card.rowconfigure(1, weight=1)

        self.checker_log_card = ttk.Frame(body, style="Card.TFrame", padding=14)
        self.checker_log_card.grid(row=1, column=1, sticky="nsew")
        self.checker_log_card.columnconfigure(0, weight=1)
        self.checker_log_card.rowconfigure(1, weight=1)

        self.build_checker_settings(settings_card)
        self.build_checker_progress(progress_card)
        self.build_checker_issues(issues_card)
        self.build_checker_log(self.checker_log_card)
        self.update_checker_source_panels()
        self.update_checker_buttons()

    def build_checker_settings(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Checker Source", style="Section.Card.TLabel").pack(anchor="w")

        mode_frame = ttk.Frame(parent, style="Card.TFrame")
        mode_frame.pack(fill="x", pady=(10, 12))
        mode_frame.columnconfigure(1, weight=1)
        ttk.Label(mode_frame, text="Source mode", style="Muted.Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        modes = list(checker.SOURCE_MODES)
        if self.checker_source_mode_var.get() not in modes:
            self.checker_source_mode_var.set(checker.MODE_ADB)
        mode_combo = ttk.Combobox(
            mode_frame,
            values=modes,
            textvariable=self.checker_source_mode_var,
            state="readonly",
        )
        mode_combo.grid(row=0, column=1, sticky="ew")
        mode_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_checker_source_panels())
        self.add_tip(mode_combo, "Chooses where the Sync Checker reads the source library from: a Windows folder/card reader, FTP, or ADB over USB.")

        panels = ttk.Frame(parent, style="Card.TFrame")
        panels.pack(fill="x", pady=(0, 14))
        panels.columnconfigure(0, weight=1)

        folder_panel = ttk.Frame(panels, style="Card.TFrame")
        folder_panel.grid(row=0, column=0, sticky="ew")
        folder_panel.columnconfigure(1, weight=1)
        self.add_checker_entry_row(
            folder_panel,
            0,
            "Source folder",
            self.checker_source_folder_var,
            browse=lambda: self.browse_folder(self.checker_source_folder_var, "Choose source folder"),
        )

        ftp_panel = ttk.Frame(panels, style="Card.TFrame")
        ftp_panel.grid(row=0, column=0, sticky="ew")
        ftp_panel.columnconfigure(1, weight=1)
        self.add_checker_entry_row(ftp_panel, 0, "FTP IP:Port", self.checker_ftp_hostport_var)
        self.add_checker_entry_row(ftp_panel, 1, "FTP root", self.checker_ftp_root_var)
        self.add_checker_entry_row(ftp_panel, 2, "FTP username", self.checker_ftp_username_var)
        self.add_checker_entry_row(ftp_panel, 3, "FTP password", self.checker_ftp_password_var, show="*")

        adb_panel = ttk.Frame(panels, style="Card.TFrame")
        adb_panel.grid(row=0, column=0, sticky="ew")
        adb_panel.columnconfigure(1, weight=1)
        self.add_checker_entry_row(
            adb_panel,
            0,
            "ADB",
            self.checker_adb_exe_var,
            browse=lambda: self.browse_file(self.checker_adb_exe_var, "Choose adb.exe"),
        )
        ttk.Label(adb_panel, text="Device", style="Muted.Card.TLabel").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(0, 8),
            padx=(0, 10),
        )
        self.checker_device_combo = ttk.Combobox(
            adb_panel,
            textvariable=self.checker_adb_device_var,
            values=[self.checker_adb_device_var.get()] if self.checker_adb_device_var.get() else [],
        )
        self.checker_device_combo.grid(row=1, column=1, sticky="ew", pady=(0, 8))
        self.add_tip(self.checker_device_combo, "The RP5 device serial. Leave blank for one connected device, or choose the detected serial when multiple Android devices are connected.")
        detect_button = ttk.Button(adb_panel, text="Detect Devices", command=self.detect_checker_adb_devices)
        detect_button.grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=(0, 8))
        self.add_tip(detect_button, "Finds connected ADB devices and storage roots. The detected roots also populate the shared Library Profile picker.")
        self.checker_action_buttons.append(detect_button)
        self.add_checker_entry_row(
            adb_panel,
            2,
            "Root",
            self.checker_adb_root_var,
            help_text="For your removable SD card ROMs, use /storage/4A21-0000/ROMs.",
        )

        self.checker_source_panels = {
            checker.MODE_FOLDER: folder_panel,
            checker.MODE_FTP: ftp_panel,
            checker.MODE_ADB: adb_panel,
        }

        ttk.Label(parent, text="PC Backup", style="Section.Card.TLabel").pack(anchor="w", pady=(4, 0))
        dest_frame = ttk.Frame(parent, style="Card.TFrame")
        dest_frame.pack(fill="x", pady=(10, 14))
        dest_frame.columnconfigure(1, weight=1)
        self.add_checker_entry_row(
            dest_frame,
            0,
            "Destination",
            self.checker_dest_folder_var,
            browse=lambda: self.browse_folder(self.checker_dest_folder_var, "Choose PC backup folder"),
        )

        ttk.Label(parent, text="Compare", style="Section.Card.TLabel").pack(anchor="w", pady=(4, 0))
        compare_frame = ttk.Frame(parent, style="Card.TFrame")
        compare_frame.pack(fill="x", pady=(10, 0))
        compare_frame.columnconfigure(3, weight=1)
        ttk.Radiobutton(compare_frame, text="Fast size check", variable=self.checker_thorough_var, value=False).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 14),
        )
        ttk.Radiobutton(compare_frame, text="Thorough hash check", variable=self.checker_thorough_var, value=True).grid(
            row=0,
            column=1,
            sticky="w",
            padx=(0, 18),
        )
        ttk.Label(compare_frame, text="Filter", style="Muted.Card.TLabel").grid(row=0, column=2, sticky="e", padx=(0, 8))
        filter_combo = ttk.Combobox(
            compare_frame,
            values=list(checker.ISSUE_FILTERS),
            textvariable=self.checker_filter_var,
            state="readonly",
            width=16,
        )
        filter_combo.grid(row=0, column=3, sticky="e")
        filter_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_checker_issue_table())
        self.add_tip(filter_combo, "Filters the issue list after a scan. It does not change what is scanned, only what you are looking at.")

    def build_checker_progress(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Scan / Repair", style="Section.Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(parent, textvariable=self.checker_status_var, style="Muted.Card.TLabel").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(10, 0),
        )
        self.checker_progress = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.checker_progress.grid(row=2, column=0, sticky="ew", pady=(10, 8))
        ttk.Label(parent, textvariable=self.checker_progress_text_var, style="Muted.Card.TLabel", wraplength=360).grid(
            row=3,
            column=0,
            sticky="w",
        )
        ttk.Label(parent, textvariable=self.checker_summary_var, style="Stat.Card.TLabel", wraplength=360).grid(
            row=4,
            column=0,
            sticky="w",
            pady=(12, 0),
        )

        buttons = ttk.Frame(parent, style="Card.TFrame")
        buttons.grid(row=5, column=0, sticky="ew", pady=(18, 0))
        buttons.columnconfigure((0, 1), weight=1)
        scan_button = ttk.Button(buttons, text="Scan / Compare", style="Accent.TButton", command=self.start_checker_scan)
        scan_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.add_tip(scan_button, "Scans the RP5/source folder and your PC backup, then lists missing files, size mismatches, and optional hash mismatches.")
        copy_all_button = ttk.Button(buttons, text="Copy ALL Issues", command=self.copy_all_checker_issues)
        copy_all_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.add_tip(copy_all_button, "Repairs every issue from the latest scan by copying source files into your PC backup. Run Scan / Compare first.")
        copy_selected_button = ttk.Button(buttons, text="Copy Selected", command=self.copy_selected_checker_issues)
        copy_selected_button.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(8, 0))
        self.add_tip(copy_selected_button, "Repairs only the rows selected in the issue table. Useful when you want to fix a few systems or files first.")
        use_transfer_button = ttk.Button(buttons, text="Use Transfer Paths", command=self.copy_transfer_paths_to_checker)
        use_transfer_button.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(8, 0))
        self.add_tip(use_transfer_button, "Copies the current Fast Transfer paths into Sync Checker so both tabs compare the same RP5 folder and PC backup.")
        self.checker_action_buttons.extend([scan_button, copy_all_button, copy_selected_button, use_transfer_button])
        self.checker_scan_button = scan_button
        self.checker_copy_all_button = copy_all_button
        self.checker_copy_selected_button = copy_selected_button

    def build_checker_issues(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="Issues", style="Section.Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(top, textvariable=self.checker_issue_count_var, style="Muted.Card.TLabel").grid(row=0, column=1, sticky="e")

        table_frame = ttk.Frame(parent, style="Card.TFrame")
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("issue", "file", "source_size", "dest_size")
        self.checker_issue_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "issue": "Issue",
            "file": "File",
            "source_size": "Source Size",
            "dest_size": "Dest Size",
        }
        widths = {"issue": 130, "file": 520, "source_size": 110, "dest_size": 110}
        for column in columns:
            self.checker_issue_tree.heading(column, text=headings[column])
            self.checker_issue_tree.column(column, width=widths[column], minwidth=80, anchor="w")
        self.checker_issue_tree.grid(row=0, column=0, sticky="nsew")
        self.add_tip(self.checker_issue_tree, "Shows scan differences. Select rows, then Copy Selected, or use Copy ALL Issues to repair everything listed.")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.checker_issue_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.checker_issue_tree.configure(yscrollcommand=scrollbar.set)

    def build_checker_log(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="Checker Log", style="Section.Card.TLabel").grid(row=0, column=0, sticky="w")
        save_button = ttk.Button(top, text="Save Log", command=self.save_checker_log)
        save_button.grid(row=0, column=1, sticky="e")
        self.add_tip(save_button, "Saves the checker log to a text file for troubleshooting or record keeping.")
        self.checker_log_text = self.make_text(parent, height=12, mono=True)
        self.checker_log_text.grid(row=1, column=0, sticky="nsew")
        self.checker_log_text.configure(state="disabled")
        self.add_tip(self.checker_log_text, "Shows detailed checker activity, warnings, and copy repair results. Hidden in Simple Mode to reduce noise.")

    def add_checker_entry_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        browse: Callable[[], None] | None = None,
        help_text: str | None = None,
        show: str | None = None,
    ) -> int:
        ttk.Label(parent, text=label, style="Muted.Card.TLabel").grid(row=row, column=0, sticky="w", pady=(0, 8), padx=(0, 10))
        entry = ttk.Entry(parent, textvariable=variable, show=show or "")
        entry.grid(row=row, column=1, sticky="ew", pady=(0, 8))
        self.add_tip(entry, help_text or f"{label} used by the Sync Checker. This value is saved and reused for scans and repair copies.")
        if browse:
            button = ttk.Button(parent, text="Browse", command=browse)
            button.grid(row=row, column=2, sticky="ew", padx=(8, 0), pady=(0, 8))
            self.add_tip(button, f"Choose the {label.lower()} from your PC instead of typing it.")
        return row + 1

    def current_pc_path_for_preflight(self) -> str:
        mode = self.mode_var.get()
        if mode == "local-copy":
            return self.dest_var.get().strip() or self.checker_dest_folder_var.get().strip()
        if mode in {"adb-pull", "adb-push"}:
            return self.local_var.get().strip() or self.checker_dest_folder_var.get().strip()
        return self.checker_dest_folder_var.get().strip() or self.local_var.get().strip()

    def start_preflight_scan(self) -> None:
        if self.preflight_busy:
            return
        adb = self.adb_var.get().strip() or "adb"
        serial = self.serial_var.get().strip() or self.checker_adb_device_var.get().strip() or None
        remote = self.remote_var.get().strip() or self.checker_adb_root_var.get().strip()
        pc_path = self.current_pc_path_for_preflight()

        self.preflight_busy = True
        self.preflight_status_var.set("Running preflight scan...")
        self.preflight_device_var.set("Device: checking...")
        self.update_preflight_buttons()

        def run() -> None:
            try:
                profile = self.collect_preflight_profile(adb, serial, remote, pc_path)
            except Exception as exc:  # noqa: BLE001
                details = "".join(traceback.format_exception(exc))
                self.events.put(("preflight_error", {"message": str(exc), "details": details}))
            else:
                self.events.put(("preflight_done", {"profile": profile}))

        self.preflight_thread = threading.Thread(target=run, daemon=True)
        self.preflight_thread.start()

    def install_adb_here(self) -> None:
        if self.adb_install_busy:
            return
        if self.is_busy() or self.is_checker_busy():
            messagebox.showinfo("Work running", "Wait for transfers or checker jobs to finish before installing ADB.")
            return
        ok = messagebox.askyesno(
            "Install ADB",
            "Download the official Android SDK Platform-Tools for Windows from Google and extract adb.exe into this app folder?\n\n"
            "This avoids needing admin rights and will auto-fill the ADB path afterward.",
        )
        if not ok:
            return

        self.adb_install_busy = True
        self.preflight_status_var.set("Downloading Android SDK Platform-Tools from Google...")
        self.update_preflight_buttons()

        def run() -> None:
            archive_path = Path(__file__).with_name("platform-tools-latest-windows.zip")
            target_parent = Path(__file__).parent
            try:
                urllib.request.urlretrieve(PLATFORM_TOOLS_ZIP, archive_path)
                with zipfile.ZipFile(archive_path) as archive:
                    archive.extractall(target_parent)
                adb_path = target_parent / "platform-tools" / "adb.exe"
                if not adb_path.exists():
                    raise RuntimeError("The download finished, but adb.exe was not found in the extracted platform-tools folder.")
            except Exception as exc:  # noqa: BLE001
                details = "".join(traceback.format_exception(exc))
                self.events.put(("adb_install_error", {"message": str(exc), "details": details}))
            else:
                self.events.put(("adb_install_done", {"adb_path": str(adb_path)}))
            finally:
                try:
                    archive_path.unlink()
                except OSError:
                    pass

        self.adb_install_thread = threading.Thread(target=run, daemon=True)
        self.adb_install_thread.start()

    def finish_adb_install(self, adb_path: str) -> None:
        self.adb_install_busy = False
        self.adb_install_thread = None
        self.adb_var.set(adb_path)
        self.checker_adb_exe_var.set(adb_path)
        self.preflight_status_var.set(f"ADB installed and selected: {adb_path}")
        self.append_log(f"ADB installed and selected: {adb_path}")
        self.append_checker_log(f"ADB installed and selected: {adb_path}")
        self.update_preflight_buttons()
        self.start_preflight_scan()

    def finish_adb_install_error(self, message: str, details: str | None = None) -> None:
        self.adb_install_busy = False
        self.adb_install_thread = None
        self.preflight_status_var.set("ADB install failed. Opening the official download page may help.")
        self.update_preflight_buttons()
        if details:
            self.append_log(details)
        if messagebox.askyesno(
            "ADB install failed",
            f"{message}\n\nOpen the official Android Platform-Tools download page instead?",
        ):
            webbrowser.open(PLATFORM_TOOLS_PAGE)

    def show_usb_debugging_help(self) -> None:
        messagebox.showinfo(
            "USB Debugging Help",
            "On the RP5:\n\n"
            "1. Open Settings > About device.\n"
            "2. Tap Build number seven times to enable Developer options.\n"
            "3. Go to Settings > System > Developer options.\n"
            "4. Turn on USB debugging.\n"
            "5. Plug the RP5 into this PC with a data-capable USB cable.\n"
            "6. Unlock the RP5 and accept the USB debugging / RSA trust prompt.\n\n"
            "Then run Preflight or Check Device again. If Windows asks for a USB mode, File Transfer is usually fine.",
        )

    def start_speed_probe(self) -> None:
        if self.speed_probe_busy:
            return
        if self.is_busy() or self.is_checker_busy():
            messagebox.showinfo("Work running", "Wait for transfers or checker jobs to finish before tuning speed.")
            return
        adb = self.adb_var.get().strip() or "adb"
        serial = self.serial_var.get().strip() or self.checker_adb_device_var.get().strip() or None
        remote = self.remote_var.get().strip() or self.checker_adb_root_var.get().strip()
        if not remote:
            messagebox.showerror("Missing Retroid folder", "Choose a Retroid folder before tuning speed.")
            return
        local_anchor = existing_path_anchor(self.current_pc_path_for_preflight())

        self.speed_probe_busy = True
        self.speed_probe_var.set("Speed probe: scanning sample files...")
        self.preflight_status_var.set("Speed probe is testing ADB worker counts with temporary copies.")
        self.update_preflight_buttons()

        def run() -> None:
            try:
                result = self.collect_speed_probe(adb, serial, remote, local_anchor)
            except Exception as exc:  # noqa: BLE001
                details = "".join(traceback.format_exception(exc))
                self.events.put(("speed_probe_error", {"message": str(exc), "details": details}))
            else:
                self.events.put(("speed_probe_done", {"result": result}))

        self.speed_probe_thread = threading.Thread(target=run, daemon=True)
        self.speed_probe_thread.start()

    def collect_speed_probe(
        self,
        adb: str,
        serial: str | None,
        remote: str,
        local_anchor: Path,
    ) -> SpeedProbeResult:
        engine.check_adb(adb, serial)
        remote_items = engine.list_remote_files(adb, serial, remote)
        sample = self.speed_probe_sample(remote_items)
        if not sample:
            raise RuntimeError("No suitable sample files were found in the selected Retroid folder.")

        current_jobs = 4
        try:
            current_jobs = max(1, int(float(self.jobs_var.get())))
        except ValueError:
            pass
        profile_jobs = self.preflight_profile.adb_jobs if self.preflight_profile else 6
        max_jobs = clamp(max(current_jobs, profile_jobs) * 2, 2, 12)
        max_jobs = min(max_jobs, max(1, len(sample)))
        candidates = sorted(
            {
                jobs
                for jobs in (1, 2, 3, 4, 6, 8, 10, 12, current_jobs, profile_jobs)
                if 1 <= jobs <= max_jobs
            }
        )
        if not candidates:
            candidates = [1]

        sample_bytes = sum(item.size or 0 for item in sample)
        trials: list[tuple[int, float, int, int]] = []
        probe_root = local_anchor / ".rp5_speed_probe_tmp"
        if probe_root.exists():
            shutil.rmtree(probe_root, ignore_errors=True)
        probe_root.mkdir(parents=True, exist_ok=True)
        try:
            for jobs in candidates:
                mbps, failures = self.run_speed_probe_trial(adb, serial, sample, probe_root, jobs)
                trials.append((jobs, mbps, len(sample), failures))
                self.events.put(
                    (
                        "speed_probe_progress",
                        {"jobs": jobs, "mbps": mbps, "failures": failures, "sample_files": len(sample)},
                    )
                )
        finally:
            shutil.rmtree(probe_root, ignore_errors=True)

        usable = [trial for trial in trials if trial[3] == 0]
        if not usable:
            raise RuntimeError("All speed-probe trials failed. Check the transfer log for ADB errors.")
        best = max(usable, key=lambda trial: trial[1])
        return SpeedProbeResult(
            best_jobs=best[0],
            best_mbps=best[1],
            trials=tuple(trials),
            sample_files=len(sample),
            sample_bytes=sample_bytes,
        )

    def speed_probe_sample(self, items: list[engine.TransferItem]) -> list[engine.TransferItem]:
        sized = [item for item in items if item.size and item.size > 0]
        if not sized:
            return items[:24]
        small = sorted((item for item in sized if item.size <= 64 * 1024**2), key=lambda item: item.size or 0)
        sample: list[engine.TransferItem] = []
        total = 0
        for item in small:
            sample.append(item)
            total += item.size or 0
            if total >= 48 * 1024**2 or len(sample) >= 80:
                break
        if total < 16 * 1024**2:
            for item in sorted(sized, key=lambda item: item.size or 0):
                if item in sample or (item.size or 0) > 256 * 1024**2:
                    continue
                sample.append(item)
                total += item.size or 0
                if total >= 64 * 1024**2 or len(sample) >= 48:
                    break
        return sample[:96]

    def run_speed_probe_trial(
        self,
        adb: str,
        serial: str | None,
        sample: list[engine.TransferItem],
        probe_root: Path,
        jobs: int,
    ) -> tuple[float, int]:
        trial_root = Path(tempfile.mkdtemp(prefix=f"jobs_{jobs}_", dir=probe_root))
        copied_bytes = 0
        failures = 0
        start = time.monotonic()

        def probe_item(item: engine.TransferItem) -> engine.TransferResult:
            dest = engine.rel_to_local(trial_root, item.rel)
            probe = engine.TransferItem(item.rel, item.source, str(dest), item.size, item.mtime)
            return engine.adb_pull_file(adb, serial, probe, True)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
                futures = [executor.submit(probe_item, item) for item in engine.sort_for_transfer(sample)]
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result.status == "failed":
                        failures += 1
                    else:
                        copied_bytes += result.bytes_done or result.item.size or 0
        finally:
            shutil.rmtree(trial_root, ignore_errors=True)

        elapsed = max(time.monotonic() - start, 0.001)
        mbps = (copied_bytes / elapsed) / (1024 * 1024)
        return mbps, failures

    def finish_speed_probe(self, result: SpeedProbeResult) -> None:
        self.speed_probe_busy = False
        self.speed_probe_thread = None
        self.jobs_var.set(str(result.best_jobs))
        trial_text = ", ".join(f"{jobs}={mbps:.1f} MB/s" for jobs, mbps, _files, failures in result.trials if failures == 0)
        self.speed_probe_var.set(
            f"Speed probe: best {result.best_jobs} jobs at {result.best_mbps:.1f} MB/s"
        )
        self.preflight_status_var.set(
            f"Speed probe used {result.sample_files} files ({engine.human_bytes(result.sample_bytes)} sample). Results: {trial_text}"
        )
        self.append_log(self.preflight_status_var.get())
        self.update_preflight_buttons()

    def finish_speed_probe_error(self, message: str, details: str | None = None) -> None:
        self.speed_probe_busy = False
        self.speed_probe_thread = None
        self.speed_probe_var.set(f"Speed probe failed: {message}")
        if details:
            self.append_log(details)
        self.update_preflight_buttons()
        messagebox.showerror("Speed probe failed", message)

    def collect_preflight_profile(
        self,
        adb: str,
        serial: str | None,
        remote: str,
        pc_path: str,
    ) -> PreflightProfile:
        cpu_count = os.cpu_count() or 4
        adb_jobs, local_jobs, buffer_mb, total_memory, available_memory, cpu_name = recommend_jobs()
        disk_path, free_bytes, total_bytes = disk_usage_for(pc_path)
        notes: list[str] = []
        devices: list[str] = []
        device_ready = False
        selected_serial = serial
        rom_roots: list[engine.RemoteDirEntry] = []
        storage_roots: list[engine.RemoteDirEntry] = []

        try:
            devices = list_ready_adb_devices(adb)
        except FileNotFoundError:
            notes.append("ADB was not found. Browse to adb.exe or add Platform Tools to PATH.")
        except OSError as exc:
            notes.append(f"ADB device list failed: {exc}")

        if not selected_serial and devices:
            selected_serial = devices[0]

        try:
            engine.check_adb(adb, selected_serial)
            device_ready = True
            rom_roots = engine.find_remote_rom_roots(adb, selected_serial)
            storage_roots = engine.find_remote_storage_roots(adb, selected_serial)
        except SystemExit as exc:
            notes.append(str(exc) or "ADB could not see a ready RP5.")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Storage detection failed: {exc}")

        choices = build_storage_choices(rom_roots, storage_roots, remote)
        if not choices and remote:
            choices = (StorageChoice("Current path", posixpath.basename(remote.rstrip("/")) or remote, remote),)
        return PreflightProfile(
            cpu_count=cpu_count,
            cpu_name=cpu_name,
            total_memory_bytes=total_memory,
            available_memory_bytes=available_memory,
            adb_jobs=adb_jobs,
            local_jobs=local_jobs,
            buffer_mb=buffer_mb,
            pc_path=disk_path,
            pc_free_bytes=free_bytes,
            pc_total_bytes=total_bytes,
            device_ready=device_ready,
            devices=tuple(devices),
            selected_serial=selected_serial,
            storage_choices=choices,
            notes=tuple(notes),
        )

    def finish_preflight(self, profile: PreflightProfile) -> None:
        self.preflight_busy = False
        self.preflight_thread = None
        self.preflight_profile = profile
        self.storage_choices = list(profile.storage_choices)
        self.update_preflight_table()
        self.apply_preflight_recommendations()

        if profile.device_ready:
            serial_text = profile.selected_serial or "single connected device"
            self.preflight_device_var.set(f"Device: ready ({serial_text})")
            if profile.selected_serial:
                self.serial_var.set(profile.selected_serial)
                self.checker_adb_device_var.set(profile.selected_serial)
        elif profile.devices:
            self.preflight_device_var.set("Device: not ready. Ready devices found: " + ", ".join(profile.devices))
        else:
            self.preflight_device_var.set("Device: no ready RP5 found")

        if profile.pc_free_bytes is not None and profile.pc_total_bytes is not None:
            self.preflight_pc_var.set(
                f"PC folder: {profile.pc_path} | free {engine.human_bytes(profile.pc_free_bytes)}"
            )
        else:
            self.preflight_pc_var.set(f"PC folder: {profile.pc_path} | free space unknown")

        resource_note = f"{profile.cpu_count} logical CPUs"
        if profile.available_memory_bytes is not None:
            resource_note += f", {engine.human_bytes(profile.available_memory_bytes)} RAM free"
        if profile.notes:
            self.preflight_status_var.set(
                f"Preflight complete ({resource_note}) with notes: " + " | ".join(profile.notes[:2])
            )
        else:
            self.preflight_status_var.set(
                f"Preflight complete ({resource_note}). Device, storage choices, and job recommendations are ready."
            )

        recommended = next((choice for choice in self.storage_choices if choice.recommended), None)
        if recommended:
            self.select_storage_choice(recommended)
            self.apply_selected_storage_choice("both", choice=recommended, quiet=True)

        self.update_preflight_buttons()
        self.run_pending_after_preflight()

    def finish_preflight_error(self, message: str, details: str | None = None) -> None:
        self.preflight_busy = False
        self.preflight_thread = None
        self.preflight_status_var.set(f"Preflight failed: {message}")
        self.preflight_device_var.set("Device: preflight failed")
        if details:
            self.append_log(details)
        self.update_preflight_buttons()
        self.pending_after_preflight = None

    def update_preflight_table(self) -> None:
        if not hasattr(self, "preflight_tree"):
            return
        self.preflight_tree.delete(*self.preflight_tree.get_children())
        for index, choice in enumerate(self.storage_choices):
            marker = "*" if choice.recommended else ""
            self.preflight_tree.insert("", "end", iid=str(index), values=(marker, choice.kind, choice.path))
        if self.storage_choices:
            preferred_index = next((i for i, choice in enumerate(self.storage_choices) if choice.recommended), 0)
            self.preflight_tree.selection_set(str(preferred_index))
            self.preflight_tree.focus(str(preferred_index))

    def selected_storage_choice(self) -> StorageChoice | None:
        if not self.storage_choices:
            return None
        selection = self.preflight_tree.selection() if hasattr(self, "preflight_tree") else ()
        if selection:
            try:
                index = int(selection[0])
            except ValueError:
                index = 0
            if 0 <= index < len(self.storage_choices):
                return self.storage_choices[index]
        return next((choice for choice in self.storage_choices if choice.recommended), self.storage_choices[0])

    def select_storage_choice(self, choice: StorageChoice) -> None:
        if not hasattr(self, "preflight_tree"):
            return
        for index, current in enumerate(self.storage_choices):
            if current.path == choice.path:
                iid = str(index)
                self.preflight_tree.selection_set(iid)
                self.preflight_tree.focus(iid)
                self.preflight_tree.see(iid)
                return

    def apply_selected_storage_choice(
        self,
        target: str,
        choice: StorageChoice | None = None,
        quiet: bool = False,
    ) -> None:
        choice = choice or self.selected_storage_choice()
        if not choice:
            return
        path = choice.path
        if target in {"transfer", "both"}:
            self.remote_var.set(path)
            self.remote_browser_path_var.set(path)
        if target in {"checker", "both"} and checker is not None:
            self.checker_source_mode_var.set(checker.MODE_ADB)
            self.checker_adb_exe_var.set(self.adb_var.get().strip() or self.checker_adb_exe_var.get())
            self.checker_adb_device_var.set(self.serial_var.get().strip() or self.checker_adb_device_var.get())
            self.checker_adb_root_var.set(path)
            self.update_checker_source_panels()
        self.refresh_command_preview()
        if not quiet:
            label = {"transfer": "Fast Transfer", "checker": "Sync Checker", "both": "both tabs"}[target]
            self.preflight_status_var.set(f"Applied {path} to {label}.")

    def browse_selected_storage_choice(self) -> None:
        choice = self.selected_storage_choice()
        if not choice:
            return
        self.remote_browser_path_var.set(choice.path)
        self.load_remote_browser(choice.path)

    def scan_selected_storage_choice(self) -> None:
        choice = self.selected_storage_choice()
        if not choice:
            return
        self.apply_selected_storage_choice("checker", choice=choice, quiet=True)
        self.start_checker_scan()

    def apply_preflight_recommendations(self) -> None:
        profile = self.preflight_profile
        if not profile:
            return
        self.preflight_jobs_var.set(
            f"Recommended jobs: ADB {profile.adb_jobs} | Local {profile.local_jobs} | Buffer {profile.buffer_mb} MB"
        )
        if self.auto_tune_var.get():
            if self.mode_var.get() == "local-copy":
                self.jobs_var.set(str(profile.local_jobs))
            else:
                self.jobs_var.set(str(profile.adb_jobs))
            self.buffer_mb_var.set(str(profile.buffer_mb))

    def update_preflight_buttons(self) -> None:
        if hasattr(self, "profile_strip_buttons"):
            for index, button in enumerate(self.profile_strip_buttons):
                if index == 1:
                    button.configure(state="disabled" if self.preflight_busy or self.adb_install_busy or self.speed_probe_busy else "normal")
                else:
                    button.configure(state="normal")
        if not self.preflight_buttons:
            return
        has_choices = bool(self.storage_choices)
        for index, button in enumerate(self.preflight_buttons):
            if index == 0:
                button.configure(state="disabled" if self.preflight_busy or self.adb_install_busy or self.speed_probe_busy else "normal")
            elif 1 <= index <= 5:
                button.configure(state="disabled" if self.preflight_busy or self.adb_install_busy or self.speed_probe_busy or not has_choices else "normal")
            elif index == 6:
                button.configure(state="disabled" if self.preflight_busy or self.adb_install_busy or self.speed_probe_busy else "normal")
            elif index == 8:
                button.configure(state="disabled" if self.preflight_busy or self.adb_install_busy or self.speed_probe_busy else "normal")
            else:
                button.configure(state="normal")

    def ensure_preflight_before(self, action: str) -> bool:
        if self.preflight_profile is not None:
            return True
        self.pending_after_preflight = action
        if self.preflight_busy:
            self.preflight_status_var.set("Preflight is running. The requested action will continue when it finishes.")
        else:
            self.preflight_status_var.set("Running preflight first. The requested action will continue when it finishes.")
            self.start_preflight_scan()
        return False

    def run_pending_after_preflight(self) -> None:
        action = self.pending_after_preflight
        self.pending_after_preflight = None
        if action == "preview":
            self.root.after(100, self.preview_transfer)
        elif action == "transfer":
            self.root.after(100, self.start_transfer)
        elif action == "checker_scan":
            self.root.after(100, self.start_checker_scan)

    def update_checker_source_panels(self) -> None:
        if checker is None or not self.checker_source_panels:
            return
        mode = self.checker_source_mode_var.get()
        for panel_mode, panel in self.checker_source_panels.items():
            if panel_mode == mode:
                panel.grid()
            else:
                panel.grid_remove()
        self.update_checker_buttons()

    def checker_settings_from_ui(self) -> dict:
        return {
            "source_mode": self.checker_source_mode_var.get(),
            "source_folder": self.checker_source_folder_var.get().strip(),
            "dest_folder": self.checker_dest_folder_var.get().strip(),
            "ftp_hostport": self.checker_ftp_hostport_var.get().strip(),
            "ftp_root": self.checker_ftp_root_var.get().strip() or "/",
            "ftp_username": self.checker_ftp_username_var.get().strip(),
            "ftp_password": self.checker_ftp_password_var.get(),
            "adb_exe": self.checker_adb_exe_var.get().strip(),
            "adb_device": self.checker_adb_device_var.get().strip(),
            "adb_root": self.checker_adb_root_var.get().strip() or "/sdcard",
            "thorough": bool(self.checker_thorough_var.get()),
            "filter": self.checker_filter_var.get(),
        }

    def start_checker_scan(self, auto: bool = False) -> None:
        if checker is None:
            return
        if not auto and not self.ensure_preflight_before("checker_scan"):
            return
        settings = self.checker_settings_from_ui()
        valid, message = checker.validate_settings(settings)
        if not valid:
            messagebox.showerror("Checker settings", message)
            return
        checker.save_config(settings=settings)
        if not auto:
            self.checker_issues = []
            self.checker_visible_issues = []
            self.checker_issue_tree.delete(*self.checker_issue_tree.get_children())
            self.checker_summary_var.set("Scanning...")
            self.checker_issue_count_var.set("Showing 0 issue(s)")
        self.checker_scan_signature = checker.settings_signature(settings)
        self.launch_checker_worker("scan", checker.scan_compare_worker, settings)

    def detect_checker_adb_devices(self) -> None:
        if checker is None:
            return
        self.launch_checker_worker("adb_detect", checker.detect_adb_devices_worker, self.checker_adb_exe_var.get())

    def copy_all_checker_issues(self) -> None:
        self.start_checker_copy(list(self.checker_issues))

    def copy_selected_checker_issues(self) -> None:
        selected_ids = self.checker_issue_tree.selection()
        selected_issues = []
        for item_id in selected_ids:
            try:
                index = int(item_id)
            except ValueError:
                continue
            if 0 <= index < len(self.checker_visible_issues):
                selected_issues.append(self.checker_visible_issues[index])
        self.start_checker_copy(selected_issues)

    def start_checker_copy(self, selected_issues: list[dict]) -> None:
        if checker is None:
            return
        if not selected_issues:
            messagebox.showerror("No issues selected", "Choose at least one issue to copy.")
            return
        settings = self.checker_settings_from_ui()
        valid, message = checker.validate_settings(settings)
        if not valid:
            messagebox.showerror("Checker settings", message)
            return
        if self.checker_scan_signature != checker.settings_signature(settings):
            messagebox.showerror(
                "Scan needed",
                "Settings changed since the last scan. Run Scan / Compare again before copying.",
            )
            return
        checker.save_config(settings=settings)
        self.launch_checker_worker("copy", checker.copy_worker, settings, selected_issues)

    def launch_checker_worker(self, task: str, func: Callable, *args) -> None:
        if checker is None:
            return
        if self.is_checker_busy():
            messagebox.showinfo("Checker running", "Wait for the current checker job to finish first.")
            return
        self.checker_status_var.set("Starting...")
        self.checker_progress_text_var.set("Starting...")
        self.checker_progress["value"] = 0
        self.set_checker_busy(True)
        event_window = CheckerEventWindow(self.events)
        self.checker_worker_thread = threading.Thread(
            target=checker.run_worker,
            args=(event_window, task, func, *args),
            daemon=True,
        )
        self.checker_worker_thread.start()

    def handle_checker_event(self, event: str, value) -> None:
        if checker is None:
            return
        if event == "-LOG-":
            lines = str(value).splitlines() or [""]
            for line in lines:
                self.append_checker_log(line)
            return
        if event == "-PROGRESS-":
            current = value.get("current", 0)
            total = value.get("total", 0)
            label = value.get("label", "")
            eta = value.get("eta", "")
            if total:
                percent = int(min(max(current / total, 0), 1) * 100)
            else:
                percent = 0
            self.checker_progress["value"] = percent
            self.checker_progress_text_var.set(f"{label}{eta}")
            self.checker_status_var.set(label or "Working...")
            return
        if event == "-WORKER_DONE-":
            self.finish_checker_worker(value)

    def finish_checker_worker(self, data: dict) -> None:
        task = data["task"]
        self.checker_worker_thread = None
        self.set_checker_busy(False)

        if not data["ok"]:
            self.checker_status_var.set("Stopped after error.")
            self.checker_progress_text_var.set("Stopped after error")
            self.append_checker_log(f"ERROR during {task}:")
            for line in data["error"].splitlines():
                self.append_checker_log(line)
            messagebox.showerror(f"{task} failed", "The checker stopped. Check the checker log for details.")
            return

        payload = data.get("payload") or {}
        if task == "scan":
            self.checker_counts = payload["counts"]
            self.checker_issues = payload["issues"]
            self.checker_last_scan_settings = payload["settings"]
            self.checker_scan_signature = checker.settings_signature(payload["settings"])
            self.checker_summary_var.set(checker.summary_text(self.checker_counts))
            self.checker_status_var.set("Scan complete.")
            self.update_checker_issue_table()
        elif task == "copy":
            copied = payload.get("copied", 0)
            failed = payload.get("failed", 0)
            self.checker_status_var.set(f"Copy complete. Copied {copied:,}, failed {failed:,}.")
            if copied > 0 and failed == 0:
                self.start_checker_scan(auto=True)
        elif task == "adb_detect":
            devices = payload.get("devices", [])
            ready_devices = [device["serial"] for device in devices if device.get("status") == "device"]
            if ready_devices:
                self.checker_device_combo.configure(values=ready_devices)
                self.checker_adb_device_var.set(ready_devices[0])
                self.serial_var.set(ready_devices[0])
                self.append_checker_log("ADB device(s): " + ", ".join(ready_devices))
                for device in devices:
                    roots = device.get("storage_roots") or []
                    if device.get("status") == "device" and roots:
                        self.append_checker_log(f"Storage roots for {device['serial']}: {', '.join(roots)}")
                        storage_entries = [
                            engine.RemoteDirEntry(name=posixpath.basename(root.rstrip("/")) or root, path=root)
                            for root in roots
                        ]
                        self.storage_choices = list(
                            build_storage_choices([], storage_entries, self.checker_adb_root_var.get().strip())
                        )
                        self.update_preflight_table()
                        self.update_preflight_buttons()
                        self.preflight_status_var.set("ADB detection found storage roots. Choose one in Library Profile.")
            elif devices:
                self.checker_device_combo.configure(values=[])
                statuses = ", ".join(f"{d['serial']} ({d['status']})" for d in devices)
                self.append_checker_log(f"ADB found no ready devices: {statuses}")
            else:
                self.checker_device_combo.configure(values=[])
                self.append_checker_log("ADB found no devices.")
            self.checker_status_var.set("ADB detection complete.")
        self.update_checker_buttons()

    def update_checker_issue_table(self) -> None:
        if checker is None or not hasattr(self, "checker_issue_tree"):
            return
        selected_filter = self.checker_filter_var.get()
        if selected_filter == "All":
            visible = list(self.checker_issues)
        else:
            visible = [issue for issue in self.checker_issues if issue["issue"] == selected_filter]
        self.checker_visible_issues = visible
        self.checker_issue_tree.delete(*self.checker_issue_tree.get_children())
        for index, issue in enumerate(visible):
            self.checker_issue_tree.insert("", "end", iid=str(index), values=checker.issue_row(issue))
        if selected_filter == "All":
            self.checker_issue_count_var.set(f"Showing {len(visible):,} issue(s)")
        else:
            self.checker_issue_count_var.set(f"Showing {len(visible):,} of {len(self.checker_issues):,} issue(s)")
        self.update_checker_buttons()

    def update_checker_buttons(self) -> None:
        if checker is None or not hasattr(self, "checker_scan_button"):
            return
        busy = self.is_checker_busy()
        has_issues = bool(self.checker_issues)
        self.checker_scan_button.configure(state="disabled" if busy else "normal")
        self.checker_copy_all_button.configure(state="disabled" if busy or not has_issues else "normal")
        self.checker_copy_selected_button.configure(state="disabled" if busy or not has_issues else "normal")
        for button in self.checker_action_buttons:
            if button not in {self.checker_scan_button, self.checker_copy_all_button, self.checker_copy_selected_button}:
                button.configure(state="disabled" if busy else "normal")

    def is_checker_busy(self) -> bool:
        return self.checker_busy or bool(self.checker_worker_thread and self.checker_worker_thread.is_alive())

    def copy_transfer_paths_to_checker(self) -> None:
        mode = self.mode_var.get()
        self.checker_adb_exe_var.set(self.adb_var.get())
        self.checker_adb_device_var.set(self.serial_var.get())
        if mode in {"adb-pull", "adb-push"}:
            self.checker_source_mode_var.set(checker.MODE_ADB)
            self.checker_adb_root_var.set(self.remote_var.get())
            self.checker_dest_folder_var.set(self.local_var.get())
        elif mode == "local-copy":
            self.checker_source_mode_var.set(checker.MODE_FOLDER)
            self.checker_source_folder_var.set(self.source_var.get())
            self.checker_dest_folder_var.set(self.dest_var.get())
        self.update_checker_source_panels()
        self.append_checker_log("Checker paths updated from the Fast Transfer tab.")

    def set_checker_busy(self, busy: bool) -> None:
        self.checker_busy = busy
        self.update_checker_buttons()

    def append_checker_log(self, message: str) -> None:
        if not hasattr(self, "checker_log_text"):
            return
        timestamp = time.strftime("%H:%M:%S")
        self.checker_log_text.configure(state="normal")
        self.checker_log_text.insert("end", f"[{timestamp}] {message}\n")
        self.checker_log_text.see("end")
        self.checker_log_text.configure(state="disabled")

    def save_checker_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save checker log",
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.checker_log_text.get("1.0", "end-1c"), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Could not save log", str(exc))

    def add_entry_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        browse: Callable[[], None] | None = None,
        help_text: str | None = None,
    ) -> int:
        ttk.Label(parent, text=label, style="Muted.Card.TLabel").grid(row=row, column=0, sticky="w", pady=(0, 8), padx=(0, 10))
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=(0, 8))
        self.add_tip(entry, help_text or f"{label} for the current transfer mode. Preflight and root detection can fill many of these automatically.")
        if browse:
            button = ttk.Button(parent, text="Browse", command=browse)
            button.grid(row=row, column=2, sticky="ew", pady=(0, 8), padx=(8, 0))
            self.add_tip(button, f"Choose the {label.lower()} using a file or folder picker.")
        return row + 1

    def add_spin_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        minimum: float,
        maximum: float,
        help_text: str,
        increment: float = 1,
    ) -> None:
        ttk.Label(parent, text=label, style="Muted.Card.TLabel").grid(row=row, column=0, sticky="w", pady=(0, 8), padx=(0, 10))
        spin = ttk.Spinbox(parent, from_=minimum, to=maximum, increment=increment, textvariable=variable, width=10, command=self.refresh_command_preview)
        spin.grid(row=row, column=1, sticky="ew", pady=(0, 8))
        spin.bind("<KeyRelease>", lambda _event: self.refresh_command_preview())
        self.add_tip(spin, help_text)

    def make_text(self, parent: tk.Widget, height: int, mono: bool = False) -> tk.Text:
        font = ("Cascadia Mono", 9) if mono else ("Segoe UI", 9)
        text = tk.Text(
            parent,
            height=height,
            wrap="word",
            borderwidth=1,
            relief="solid",
            background="#f8fafc",
            foreground=self.text,
            insertbackground=self.text,
            selectbackground="#bfdbfe",
            font=font,
            padx=8,
            pady=7,
        )
        text.bind("<KeyRelease>", lambda _event: self.refresh_command_preview())
        return text

    def bind_updates(self) -> None:
        variables = [
            self.mode_var,
            self.adb_var,
            self.serial_var,
            self.remote_var,
            self.local_var,
            self.source_var,
            self.dest_var,
            self.jobs_var,
            self.progress_interval_var,
            self.buffer_mb_var,
            self.force_var,
            self.dry_run_var,
            self.verify_var,
            self.no_remote_scan_var,
        ]
        for variable in variables:
            variable.trace_add("write", lambda *_args: self.refresh_command_preview())
        self.mode_var.trace_add("write", lambda *_args: self.refresh_mode())

    def refresh_mode(self) -> None:
        self.rebuild_path_fields()
        is_adb = self.mode_var.get() in {"adb-pull", "adb-push"}
        is_push = self.mode_var.get() == "adb-push"
        is_local = self.mode_var.get() == "local-copy"

        self.check_button.configure(state="normal" if is_adb and not self.is_busy() else "disabled")
        self.verify_check.configure(state="normal" if is_push else "disabled")
        self.no_scan_check.configure(state="normal" if is_push else "disabled")
        if is_adb:
            if not self.remote_browser_frame.winfo_manager():
                self.remote_browser_frame.pack(fill="both", expand=False, pady=(10, 18))
        else:
            self.remote_browser_frame.pack_forget()
        self.set_remote_browser_busy(self.remote_browser_busy)

        buffer_state = "normal" if is_local else "disabled"
        for child in self.root.winfo_children():
            self.set_buffer_spin_state(child, buffer_state)

        if self.preflight_profile and self.auto_tune_var.get():
            self.apply_preflight_recommendations()
        elif is_adb and self.jobs_var.get() == "12":
            self.jobs_var.set("4")
        elif is_local and self.jobs_var.get() == "4":
            self.jobs_var.set("12")
        self.refresh_command_preview()

    def set_buffer_spin_state(self, widget: tk.Widget, state: str) -> None:
        if isinstance(widget, ttk.Spinbox) and str(widget.cget("textvariable")) == str(self.buffer_mb_var):
            widget.configure(state=state)
        for child in widget.winfo_children():
            self.set_buffer_spin_state(child, state)

    def browse_folder(self, variable: tk.StringVar, title: str) -> None:
        path = filedialog.askdirectory(title=title)
        if path:
            variable.set(path)

    def browse_file(self, variable: tk.StringVar, title: str) -> None:
        path = filedialog.askopenfilename(
            title=title,
            filetypes=[("ADB executable", "adb.exe"), ("Executables", "*.exe"), ("All files", "*.*")],
        )
        if path:
            variable.set(path)

    def browse_remote_from_transfer_path(self) -> None:
        path = self.remote_var.get().strip() or self.remote_browser_path_var.get().strip() or "/sdcard"
        self.remote_browser_path_var.set(path)
        self.load_remote_browser(path)

    def jump_remote_browser(self, path: str) -> None:
        self.remote_browser_path_var.set(path)
        self.load_remote_browser(path)

    def open_remote_parent(self) -> None:
        path = engine.remote_parent(self.remote_browser_path_var.get().strip() or "/sdcard")
        self.remote_browser_path_var.set(path)
        self.load_remote_browser(path)

    def selected_remote_entry(self) -> engine.RemoteDirEntry | None:
        selection = self.remote_browser_listbox.curselection()
        if not selection:
            return None
        index = selection[0]
        if index < 0 or index >= len(self.remote_browser_entries):
            return None
        return self.remote_browser_entries[index]

    def open_selected_remote_folder(self) -> None:
        entry = self.selected_remote_entry()
        if not entry:
            return
        self.remote_browser_path_var.set(entry.path)
        self.remote_var.set(entry.path)
        self.load_remote_browser(entry.path)

    def use_remote_browser_folder(self) -> None:
        entry = self.selected_remote_entry()
        path = entry.path if entry else self.remote_browser_path_var.get().strip()
        if not path:
            return
        self.remote_var.set(path)
        self.remote_browser_path_var.set(path)
        self.remote_browser_status_var.set(f"Transfer folder set to {path}")
        self.append_log(f"Retroid folder set to {path}")
        self.refresh_command_preview()

    def load_remote_browser(self, path: str | None = None) -> None:
        if self.is_busy():
            messagebox.showinfo("Transfer running", "Wait for the transfer to finish before browsing the Retroid.")
            return
        if self.remote_browser_busy:
            return
        target_path = path or self.remote_browser_path_var.get().strip() or self.remote_var.get().strip() or "/sdcard"
        adb = self.adb_var.get().strip() or "adb"
        serial = self.serial_var.get().strip() or None

        self.remote_browser_path_var.set(target_path)
        self.remote_browser_status_var.set(f"Loading {target_path}...")
        self.set_remote_browser_busy(True)

        def load() -> None:
            try:
                engine.check_adb(adb, serial)
                entries = engine.list_remote_dirs(adb, serial, target_path)
            except SystemExit as exc:
                self.events.put(("browser_error", {"message": str(exc), "path": target_path}))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("browser_error", {"message": str(exc), "path": target_path}))
            else:
                self.events.put(("browser_dirs", {"path": target_path, "entries": entries}))

        threading.Thread(target=load, daemon=True).start()

    def detect_remote_storage(self) -> None:
        if self.is_busy():
            messagebox.showinfo("Transfer running", "Wait for the transfer to finish before browsing the Retroid.")
            return
        if self.remote_browser_busy:
            return
        adb = self.adb_var.get().strip() or "adb"
        serial = self.serial_var.get().strip() or None
        self.remote_browser_status_var.set("Finding Retroid storage...")
        self.set_remote_browser_busy(True)

        def detect() -> None:
            try:
                engine.check_adb(adb, serial)
                rom_roots = engine.find_remote_rom_roots(adb, serial)
                storage_roots = engine.find_remote_storage_roots(adb, serial)
            except SystemExit as exc:
                self.events.put(("browser_error", {"message": str(exc), "path": "storage detection"}))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("browser_error", {"message": str(exc), "path": "storage detection"}))
            else:
                self.events.put(
                    (
                        "storage_detected",
                        {"rom_roots": rom_roots, "storage_roots": storage_roots},
                    )
                )

        threading.Thread(target=detect, daemon=True).start()

    def set_remote_browser_busy(self, busy: bool) -> None:
        self.remote_browser_busy = busy
        disabled = busy or self.is_busy() or self.mode_var.get() == "local-copy"
        state = "disabled" if disabled else "normal"
        for button in self.remote_browser_buttons:
            button.configure(state=state)
        if hasattr(self, "remote_browser_listbox"):
            self.remote_browser_listbox.configure(state=state)

    def check_device(self) -> None:
        if not self.mode_var.get().startswith("adb"):
            return
        self.append_log("Checking Retroid connection...")
        self.status_var.set("Checking Retroid connection...")
        adb = self.adb_var.get().strip() or "adb"
        serial = self.serial_var.get().strip() or None

        def check() -> None:
            try:
                engine.check_adb(adb, serial)
            except SystemExit as exc:
                self.events.put(("check_error", {"message": str(exc)}))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("check_error", {"message": str(exc)}))
            else:
                self.events.put(("check_ok", {"message": "Retroid is connected and ready."}))

        threading.Thread(target=check, daemon=True).start()

    def preview_transfer(self) -> None:
        if not self.ensure_preflight_before("preview"):
            return
        settings = self.get_settings(force_dry_run=True)
        if settings:
            self.launch_worker(settings)

    def start_transfer(self) -> None:
        if not self.ensure_preflight_before("transfer"):
            return
        settings = self.get_settings(force_dry_run=False)
        if settings:
            self.launch_worker(settings)

    def stop_transfer(self) -> None:
        if self.is_busy():
            self.stop_event.set()
            self.status_var.set("Stopping after current files finish...")
            self.append_log("Stop requested. Running file transfers will finish first.")
            self.stop_button.configure(state="disabled")

    def launch_worker(self, settings: TransferSettings) -> None:
        if self.is_busy():
            messagebox.showinfo("Transfer already running", "Wait for the current transfer to finish first.")
            return
        self.reset_progress()
        self.set_busy(True)
        self.stop_event.clear()
        self.transfer_started_at = time.monotonic()
        self.transfer_finished_at = None
        self.live_interval_ms = max(250, int(settings.progress_interval * 1000))
        self.status_var.set("Starting...")
        self.append_log(f"Starting {MODE_LABELS[settings.mode]}{' dry run' if settings.dry_run else ''}.")
        if settings.mode in {"adb-pull", "adb-push"}:
            self.append_log(
                "Worker note: skipped files do not start ADB, tiny files may finish instantly, and the RP5/ADB server can throttle concurrent copy processes."
            )
        self.append_log(self.command_line_for(settings))
        event_bus = EventBus(self.events)
        runner = TransferWorker(settings, event_bus, self.stop_event)
        self.worker_thread = threading.Thread(target=runner.run, daemon=True)
        self.worker_thread.start()

    def get_settings(self, force_dry_run: bool) -> TransferSettings | None:
        try:
            jobs = int(float(self.jobs_var.get()))
            if jobs < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid jobs", "Parallel jobs must be a positive number.")
            return None

        try:
            progress_interval = float(self.progress_interval_var.get())
            if progress_interval <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid update interval", "Update interval must be a positive number.")
            return None

        try:
            buffer_mb = int(float(self.buffer_mb_var.get()))
            if buffer_mb < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid buffer", "Buffer MB must be a positive number.")
            return None

        mode = self.mode_var.get()
        adb = self.adb_var.get().strip() or "adb"
        remote = self.remote_var.get().strip()
        local = self.local_var.get().strip()
        source = self.source_var.get().strip()
        dest = self.dest_var.get().strip()

        if mode in {"adb-pull", "adb-push"} and not remote:
            messagebox.showerror("Missing Retroid folder", "Enter the Retroid folder path.")
            return None
        if mode == "adb-pull" and not local:
            messagebox.showerror("Missing PC destination", "Choose a PC destination folder.")
            return None
        if mode == "adb-push" and not local:
            messagebox.showerror("Missing PC source", "Choose the PC source folder.")
            return None
        if mode == "local-copy" and (not source or not dest):
            messagebox.showerror("Missing folders", "Choose both a source and destination folder.")
            return None

        return TransferSettings(
            mode=mode,
            adb=adb,
            serial=self.serial_var.get().strip() or None,
            remote=remote,
            local=local,
            source=source,
            dest=dest,
            include=self.patterns_from_text(self.include_text),
            exclude=self.patterns_from_text(self.exclude_text),
            jobs=jobs,
            force=self.force_var.get(),
            dry_run=force_dry_run or self.dry_run_var.get(),
            progress_interval=progress_interval,
            verify=self.verify_var.get(),
            no_remote_scan=self.no_remote_scan_var.get(),
            buffer_mb=buffer_mb,
            log_every_file=self.log_every_file_var.get(),
        )

    def patterns_from_text(self, widget: tk.Text) -> tuple[str, ...]:
        raw = widget.get("1.0", "end").replace(",", "\n")
        return tuple(line.strip() for line in raw.splitlines() if line.strip())

    def process_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                self.handle_event(event, payload)
        except queue_module.Empty:
            pass
        self.root.after(100, self.process_events)

    def handle_event(self, event: str, payload: dict) -> None:
        if event == "checker_event":
            self.handle_checker_event(payload["event"], payload["value"])
        elif event == "phase":
            message = payload["message"]
            self.status_var.set(message)
            self.append_log(message)
        elif event == "log":
            self.append_log(payload["message"])
        elif event == "plan":
            self.total_files = payload["files"]
            self.total_bytes = payload["bytes"]
            self.has_known_sizes = payload["has_known_sizes"]
            self.configure_progressbar()
            self.refresh_live_stats()
        elif event == "result":
            self.apply_result(payload["result"])
        elif event == "workers":
            self.update_worker_stats(payload)
        elif event == "done":
            self.finish_transfer(payload["code"], payload.get("stopped", False))
        elif event == "error":
            self.finish_with_error(payload["message"], payload.get("details"))
        elif event == "check_ok":
            self.status_var.set(payload["message"])
            self.append_log(payload["message"])
            messagebox.showinfo("Retroid ready", payload["message"])
        elif event == "check_error":
            self.status_var.set("Device check failed.")
            self.append_log(payload["message"])
            messagebox.showerror("Device check failed", payload["message"])
        elif event == "browser_dirs":
            self.show_remote_browser_dirs(payload["path"], payload["entries"])
        elif event == "browser_error":
            self.set_remote_browser_busy(False)
            self.remote_browser_status_var.set("Could not load Retroid folder.")
            self.append_log(f"Retroid browser error for {payload['path']}: {payload['message']}")
            messagebox.showerror("Retroid browser", payload["message"])
        elif event == "storage_detected":
            self.show_detected_storage(payload["rom_roots"], payload["storage_roots"])
        elif event == "preflight_done":
            self.finish_preflight(payload["profile"])
        elif event == "preflight_error":
            self.finish_preflight_error(payload["message"], payload.get("details"))
        elif event == "adb_install_done":
            self.finish_adb_install(payload["adb_path"])
        elif event == "adb_install_error":
            self.finish_adb_install_error(payload["message"], payload.get("details"))
        elif event == "speed_probe_done":
            self.finish_speed_probe(payload["result"])
        elif event == "speed_probe_error":
            self.finish_speed_probe_error(payload["message"], payload.get("details"))
        elif event == "speed_probe_progress":
            self.speed_probe_var.set(
                f"Speed probe: {payload['jobs']} jobs -> {payload['mbps']:.1f} MB/s"
            )

    def show_remote_browser_dirs(self, path: str, entries: list[engine.RemoteDirEntry]) -> None:
        self.remote_browser_entries = entries
        self.remote_browser_listbox.configure(state="normal")
        self.remote_browser_listbox.delete(0, "end")
        for entry in entries:
            self.remote_browser_listbox.insert("end", entry.name + "/")
        self.remote_browser_path_var.set(path)
        self.set_remote_browser_busy(False)
        if entries:
            self.remote_browser_status_var.set(f"{len(entries)} folders in {path}. Double-click to open one.")
        else:
            self.remote_browser_status_var.set(f"No child folders in {path}. Use Folder can still select it.")
        self.append_log(f"Retroid browser loaded {path} ({len(entries)} folders).")

    def show_detected_storage(
        self,
        rom_roots: list[engine.RemoteDirEntry],
        storage_roots: list[engine.RemoteDirEntry],
    ) -> None:
        self.set_remote_browser_busy(False)
        self.storage_choices = list(build_storage_choices(rom_roots, storage_roots, self.remote_var.get().strip()))
        self.update_preflight_table()
        self.update_preflight_buttons()
        entries = rom_roots or storage_roots
        self.remote_browser_entries = entries
        self.remote_browser_listbox.configure(state="normal")
        self.remote_browser_listbox.delete(0, "end")
        for entry in entries:
            self.remote_browser_listbox.insert("end", entry.path)

        if rom_roots:
            chosen = rom_roots[0]
            self.remote_var.set(chosen.path)
            self.checker_source_mode_var.set(checker.MODE_ADB if checker is not None else self.checker_source_mode_var.get())
            self.checker_adb_root_var.set(chosen.path)
            self.update_checker_source_panels()
            self.remote_browser_path_var.set(chosen.path)
            self.remote_browser_status_var.set(f"Found ROMs folder: {chosen.path}")
            self.preflight_status_var.set(f"Found ROMs folder: {chosen.path}")
            self.append_log(f"Found ROMs folder and set transfer folder to {chosen.path}")
            self.refresh_command_preview()
            return

        if storage_roots:
            chosen = storage_roots[0]
            self.remote_browser_path_var.set(chosen.path)
            self.remote_browser_status_var.set(
                f"Found {len(storage_roots)} storage roots. Select one, then open or use it."
            )
            self.preflight_status_var.set(f"Found {len(storage_roots)} storage roots. Choose one in Library Profile.")
            self.append_log("Detected Retroid storage roots: " + ", ".join(entry.path for entry in storage_roots))
            return

        self.remote_browser_status_var.set("No Retroid storage roots were found.")
        self.append_log("No Retroid storage roots were found.")

    def configure_progressbar(self) -> None:
        if self.has_known_sizes and self.total_bytes > 0:
            self.progress.configure(maximum=self.total_bytes)
        else:
            self.progress.configure(maximum=max(self.total_files, 1))
        self.progress["value"] = 0

    def apply_result(self, result: engine.TransferResult) -> None:
        size = result.bytes_done if result.bytes_done else result.item.size or 0
        self.done_files += 1
        if result.status == "copied":
            self.copied_files += 1
            self.copied_bytes += size
            if self.log_every_file_var.get():
                self.append_log(f"Copied: {result.item.rel}")
        elif result.status == "skipped":
            self.skipped_files += 1
            self.skipped_bytes += size
            if self.log_every_file_var.get():
                self.append_log(f"Skipped: {result.item.rel}")
        else:
            self.failed_files += 1
            self.append_log(f"Failed: {result.item.rel} - {result.message}")
        self.refresh_live_stats()

    def update_worker_stats(self, payload: dict) -> None:
        active_slots = payload.get("active_slots", 0)
        active_adb = payload.get("active_adb", 0)
        jobs = payload.get("jobs", 0)
        submitted = payload.get("submitted", 0)
        total = payload.get("total", 0)
        self.workers_var.set(
            f"Workers {active_slots}/{jobs} | ADB active {active_adb} | Queue {submitted}/{total}"
        )

    def finish_transfer(self, code: int, stopped: bool) -> None:
        self.transfer_finished_at = time.monotonic()
        self.refresh_live_stats()
        self.worker_thread = None
        self.set_busy(False)
        self.workers_var.set("Workers 0/0 | ADB active 0 | Queue complete")
        if stopped:
            self.status_var.set("Stopped.")
            self.append_log("Stopped. Any files already completed were left in place.")
        elif code == 0:
            self.status_var.set("Finished successfully.")
            self.append_log("Finished successfully.")
        else:
            self.status_var.set("Finished with failures.")
            self.append_log("Finished with failures. Check the log above.")

    def finish_with_error(self, message: str, details: str | None = None) -> None:
        self.transfer_finished_at = time.monotonic()
        self.refresh_live_stats()
        self.worker_thread = None
        self.set_busy(False)
        self.status_var.set("Stopped by an error.")
        self.append_log(f"Error: {message}")
        if details:
            self.append_log(details)
        messagebox.showerror("Transfer stopped", message)

    def refresh_live_stats(self) -> None:
        done_bytes = self.copied_bytes + self.skipped_bytes
        if self.has_known_sizes and self.total_bytes > 0:
            self.progress["value"] = min(done_bytes, self.total_bytes)
            self.bytes_var.set(f"{engine.human_bytes(done_bytes)} / {engine.human_bytes(self.total_bytes)}")
        else:
            self.progress["value"] = self.done_files
            self.bytes_var.set(engine.human_bytes(self.copied_bytes))

        self.files_var.set(f"{self.done_files} / {self.total_files} files")
        self.counts_var.set(
            f"Copied {self.copied_files} | Skipped {self.skipped_files} | Failed {self.failed_files}"
        )
        if self.transfer_started_at:
            end_time = self.transfer_finished_at or time.monotonic()
            elapsed = max(end_time - self.transfer_started_at, 0.001)
        else:
            elapsed = 0.001
        self.speed_var.set(f"{engine.human_bytes(self.copied_bytes / elapsed)}/s")

    def tick_live_stats(self) -> None:
        self.refresh_live_stats()
        self.root.after(self.live_interval_ms, self.tick_live_stats)

    def reset_progress(self) -> None:
        self.total_files = 0
        self.total_bytes = 0
        self.has_known_sizes = True
        self.done_files = 0
        self.copied_files = 0
        self.skipped_files = 0
        self.failed_files = 0
        self.copied_bytes = 0
        self.skipped_bytes = 0
        self.transfer_started_at = None
        self.transfer_finished_at = None
        self.progress["value"] = 0
        self.progress.configure(maximum=1)
        self.files_var.set("0 / 0 files")
        self.bytes_var.set("0 B")
        self.speed_var.set("0 B/s")
        self.counts_var.set("Copied 0 | Skipped 0 | Failed 0")
        self.workers_var.set("Workers 0/0 | ADB active 0 | Queue 0/0")

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.start_button.configure(state=state)
        self.preview_button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.check_button.configure(
            state="disabled" if busy or self.mode_var.get() == "local-copy" else "normal"
        )
        self.set_remote_browser_busy(self.remote_browser_busy)

    def is_busy(self) -> bool:
        return bool(self.worker_thread and self.worker_thread.is_alive())

    def append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save transfer log",
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Could not save log", str(exc))

    def refresh_command_preview(self) -> None:
        if not hasattr(self, "command_text"):
            return
        settings = self.get_preview_settings()
        command = self.command_line_for(settings) if settings else "Fill in the fields to build a command."
        self.command_text.configure(state="normal")
        self.command_text.delete("1.0", "end")
        self.command_text.insert("1.0", command)
        self.command_text.configure(state="disabled")

    def get_preview_settings(self) -> TransferSettings | None:
        try:
            jobs = max(1, int(float(self.jobs_var.get() or "1")))
            progress_interval = max(0.25, float(self.progress_interval_var.get() or "2.0"))
            buffer_mb = max(1, int(float(self.buffer_mb_var.get() or "16")))
        except ValueError:
            return None
        return TransferSettings(
            mode=self.mode_var.get(),
            adb=self.adb_var.get().strip() or "adb",
            serial=self.serial_var.get().strip() or None,
            remote=self.remote_var.get().strip(),
            local=self.local_var.get().strip(),
            source=self.source_var.get().strip(),
            dest=self.dest_var.get().strip(),
            include=self.patterns_from_text(self.include_text),
            exclude=self.patterns_from_text(self.exclude_text),
            jobs=jobs,
            force=self.force_var.get(),
            dry_run=self.dry_run_var.get(),
            progress_interval=progress_interval,
            verify=self.verify_var.get(),
            no_remote_scan=self.no_remote_scan_var.get(),
            buffer_mb=buffer_mb,
            log_every_file=self.log_every_file_var.get(),
        )

    def command_line_for(self, settings: TransferSettings) -> str:
        script = str(Path(__file__).with_name("fast_transfer.py"))
        cmd = [sys.executable or "python", script, settings.mode]

        if settings.mode in {"adb-pull", "adb-push"}:
            cmd.extend(["--adb", settings.adb])
            if settings.serial:
                cmd.extend(["--serial", settings.serial])

        for pattern in settings.include:
            cmd.extend(["--include", pattern])
        for pattern in settings.exclude:
            cmd.extend(["--exclude", pattern])
        cmd.extend(["--jobs", str(settings.jobs)])
        if settings.force:
            cmd.append("--force")
        if settings.dry_run:
            cmd.append("--dry-run")
        cmd.extend(["--progress-interval", str(settings.progress_interval)])

        if settings.mode == "adb-pull":
            cmd.extend(["--remote", settings.remote, "--local", settings.local])
        elif settings.mode == "adb-push":
            cmd.extend(["--local", settings.local, "--remote", settings.remote])
            if settings.verify:
                cmd.append("--verify")
            if settings.no_remote_scan:
                cmd.append("--no-remote-scan")
        else:
            cmd.extend(["--source", settings.source, "--dest", settings.dest, "--buffer-mb", str(settings.buffer_mb)])

        return subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(sh_quote(part) for part in cmd)

    def copy_command(self) -> None:
        command = self.command_text.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self.status_var.set("Command copied.")


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def main() -> None:
    root = tk.Tk()
    FastTransferGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
