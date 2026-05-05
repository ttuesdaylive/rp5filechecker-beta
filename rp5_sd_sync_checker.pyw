import ftplib
import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import PySimpleGUI as sg


sg.theme("DarkBlue3")

APP_TITLE = "RP5 SD Card Sync Checker"
CONFIG_FILE = Path(__file__).parent / "rp5_sync_checker_config.json"

MODE_FOLDER = "Folder/Drive"
MODE_FTP = "FTP (WiFi)"
MODE_ADB = "ADB (USB)"
SOURCE_MODES = [MODE_FOLDER, MODE_FTP, MODE_ADB]

ISSUE_MISSING = "Missing"
ISSUE_SIZE = "Size mismatch"
ISSUE_HASH = "Hash mismatch"
ISSUE_FILTERS = ["All", ISSUE_MISSING, ISSUE_SIZE, ISSUE_HASH]

HASH_CHUNK_SIZE = 4 * 1024 * 1024
FTP_BLOCK_SIZE = 1024 * 1024
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


DEFAULT_CONFIG = {
    "source_mode": MODE_FOLDER,
    "source_folder": "",
    "dest_folder": "",
    "ftp_hostport": "",
    "ftp_root": "/",
    "ftp_username": "",
    "ftp_password": "",
    "adb_exe": "",
    "adb_device": "",
    "adb_root": "/sdcard",
    "thorough": False,
    "filter": "All",
}


@dataclass
class FileRecord:
    rel: str
    size: int
    source_path: str


def load_config():
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                config.update(loaded)
        except Exception:
            pass
    if config.get("source_mode") not in SOURCE_MODES:
        config["source_mode"] = MODE_FOLDER
    if config.get("filter") not in ISSUE_FILTERS:
        config["filter"] = "All"
    return config


config = load_config()


def save_config(values=None, settings=None):
    if settings is not None:
        config.update(settings)
    elif values is not None:
        config.update(
            {
                "source_mode": values.get("SOURCE_MODE", MODE_FOLDER),
                "source_folder": values.get("SOURCE_FOLDER", ""),
                "dest_folder": values.get("DEST_FOLDER", ""),
                "ftp_hostport": values.get("FTP_HOSTPORT", ""),
                "ftp_root": values.get("FTP_ROOT", "/"),
                "ftp_username": values.get("FTP_USERNAME", ""),
                "ftp_password": values.get("FTP_PASSWORD", ""),
                "adb_exe": values.get("ADB_EXE", ""),
                "adb_device": values.get("ADB_DEVICE", ""),
                "adb_root": values.get("ADB_ROOT", "/sdcard"),
                "thorough": bool(values.get("THOROUGH", False)),
                "filter": values.get("FILTER", "All"),
            }
        )
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def timestamp():
    return time.strftime("%H:%M:%S")


def post_log(window, message=""):
    window.write_event_value("-LOG-", str(message))


def post_progress(window, label, current=0, total=0, start_time=None):
    eta_text = ""
    if start_time and current and total and current < total:
        elapsed = max(time.time() - start_time, 0.001)
        remaining = (elapsed / current) * (total - current)
        eta_text = f" | ETA {format_duration(remaining)}"
    window.write_event_value(
        "-PROGRESS-",
        {
            "label": label,
            "current": current,
            "total": total,
            "eta": eta_text,
        },
    )


def worker_done(window, task, ok, payload=None, error=None):
    window.write_event_value(
        "-WORKER_DONE-",
        {"task": task, "ok": ok, "payload": payload, "error": error},
    )


def run_worker(window, task, func, *args):
    try:
        payload = func(window, *args)
        worker_done(window, task, True, payload=payload)
    except Exception:
        worker_done(window, task, False, error=traceback.format_exc())


def bytes_label(size):
    if size is None:
        return "-"
    size = int(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def format_duration(seconds):
    seconds = int(max(seconds, 0))
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def normalize_rel_path(rel):
    rel = rel.replace("\\", "/").strip("/")
    parts = [part for part in rel.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError(f"Unsafe relative path: {rel}")
    return "/".join(parts)


def dest_path_for_rel(dest_root, rel):
    rel = normalize_rel_path(rel)
    if not rel:
        raise ValueError("Empty relative path")
    return Path(dest_root).joinpath(*rel.split("/"))


def hash_local_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def parse_hostport(hostport):
    raw = hostport.strip()
    if not raw:
        raise ValueError("FTP IP/port is blank")
    raw = raw.replace("ftp://", "", 1)
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    if ":" in raw:
        host, port_text = raw.rsplit(":", 1)
        try:
            port = int(port_text)
        except ValueError:
            raise ValueError(f"FTP port is not a number: {port_text}")
    else:
        host, port = raw, 21
    host = host.strip()
    if not host:
        raise ValueError("FTP host is blank")
    return host, port


def ftp_join(parent, child):
    parent = parent or "/"
    if child.startswith("/"):
        return posixpath.normpath(child)
    if parent == "/":
        return posixpath.normpath("/" + child)
    return posixpath.normpath(parent.rstrip("/") + "/" + child)


def ftp_rel(root, remote_path):
    root = posixpath.normpath(root or "/")
    remote_path = posixpath.normpath(remote_path)
    if root == "/":
        rel = remote_path.lstrip("/")
    elif remote_path == root:
        rel = posixpath.basename(remote_path)
    elif remote_path.startswith(root.rstrip("/") + "/"):
        rel = remote_path[len(root.rstrip("/") + "/") :]
    else:
        rel = posixpath.basename(remote_path)
    return normalize_rel_path(rel)


def shell_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def adb_rel(root, remote_path):
    root = posixpath.normpath(root or "/sdcard")
    remote_path = posixpath.normpath(remote_path)
    candidates = [root]
    if root in ("/sdcard", "/storage/emulated/0", "/storage/self/primary"):
        candidates.extend(["/sdcard", "/storage/emulated/0", "/storage/self/primary"])
    for candidate in candidates:
        candidate = posixpath.normpath(candidate)
        if remote_path.startswith(candidate.rstrip("/") + "/"):
            return normalize_rel_path(remote_path[len(candidate.rstrip("/") + "/") :])
    return normalize_rel_path(posixpath.basename(remote_path))


def adb_find_start(root):
    root = posixpath.normpath(root or "/sdcard")
    if root == "/":
        return root
    return root.rstrip("/") + "/."


def run_subprocess(cmd, timeout=None):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


class FolderSource:
    def __init__(self, root):
        self.root = Path(root)

    def scan(self, window):
        records = {}
        scanned = 0
        post_log(window, f"Scanning source folder: {self.root}")
        for current_root, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [name for name in dirnames if not name.startswith("$RECYCLE.BIN")]
            for filename in filenames:
                file_path = Path(current_root) / filename
                try:
                    stat = file_path.stat()
                    rel = normalize_rel_path(file_path.relative_to(self.root).as_posix())
                    records[rel] = FileRecord(rel=rel, size=stat.st_size, source_path=str(file_path))
                    scanned += 1
                    if scanned % 500 == 0:
                        post_progress(window, f"Scanning source... {scanned:,} files", scanned, 0)
                except Exception as e:
                    post_log(window, f"Warning: skipped source file {file_path}: {e}")
        post_log(window, f"Source scan found {len(records):,} file(s).")
        return records

    def hash_file(self, record):
        return hash_local_file(record.source_path)

    def copy_to_temp(self, record, temp_path):
        shutil.copy2(record.source_path, temp_path)

    def close(self):
        pass


class FTPSource:
    def __init__(self, hostport, root, username="", password=""):
        self.host, self.port = parse_hostport(hostport)
        self.root = posixpath.normpath(root.strip() or "/")
        if not self.root.startswith("/"):
            self.root = "/" + self.root
        self.username = username.strip()
        self.password = password
        self.ftp = None

    def connect(self, window=None):
        if self.ftp is not None:
            return
        if window:
            post_log(window, f"Connecting to FTP: {self.host}:{self.port}")
        ftp = ftplib.FTP(timeout=30)
        ftp.encoding = "utf-8"
        ftp.connect(self.host, self.port)
        if self.username:
            ftp.login(self.username, self.password)
        else:
            ftp.login()
        ftp.voidcmd("TYPE I")
        self.ftp = ftp

    def scan(self, window):
        self.connect(window)
        records = {}
        post_log(window, f"Scanning FTP root: {self.root}")
        self._scan_dir(window, self.root, records)
        post_log(window, f"FTP scan found {len(records):,} file(s).")
        return records

    def _scan_dir(self, window, remote_dir, records):
        try:
            for name, facts in self.ftp.mlsd(remote_dir):
                if name in (".", ".."):
                    continue
                full_path = ftp_join(remote_dir, name)
                item_type = facts.get("type", "").lower()
                if item_type == "dir":
                    self._scan_dir(window, full_path, records)
                elif item_type == "file":
                    size = int(facts.get("size", 0))
                    rel = ftp_rel(self.root, full_path)
                    records[rel] = FileRecord(rel=rel, size=size, source_path=full_path)
                    if len(records) % 500 == 0:
                        post_progress(window, f"Scanning FTP... {len(records):,} files", len(records), 0)
            return
        except ftplib.error_perm as e:
            if "MLSD" not in str(e).upper() and "500" not in str(e) and "502" not in str(e):
                post_log(window, f"Warning: MLSD failed for {remote_dir}: {e}")
        except Exception as e:
            post_log(window, f"Warning: MLSD failed for {remote_dir}: {e}")

        self._scan_dir_fallback(window, remote_dir, records)

    def _scan_dir_fallback(self, window, remote_dir, records):
        try:
            names = self.ftp.nlst(remote_dir)
        except Exception as e:
            post_log(window, f"Warning: could not list FTP folder {remote_dir}: {e}")
            return

        for name in names:
            if name in (".", ".."):
                continue
            full_path = posixpath.normpath(name if name.startswith("/") else ftp_join(remote_dir, name))
            if full_path == posixpath.normpath(remote_dir):
                continue
            try:
                size = self.ftp.size(full_path)
                if size is not None:
                    rel = ftp_rel(self.root, full_path)
                    records[rel] = FileRecord(rel=rel, size=int(size), source_path=full_path)
                    if len(records) % 500 == 0:
                        post_progress(window, f"Scanning FTP... {len(records):,} files", len(records), 0)
                    continue
            except Exception:
                pass
            if self._is_dir(full_path):
                self._scan_dir_fallback(window, full_path, records)

    def _is_dir(self, remote_path):
        try:
            current = self.ftp.pwd()
            self.ftp.cwd(remote_path)
            self.ftp.cwd(current)
            return True
        except Exception:
            try:
                self.ftp.cwd("/")
            except Exception:
                pass
            return False

    def hash_file(self, record):
        self.connect()
        h = hashlib.sha256()
        self.ftp.retrbinary(f"RETR {record.source_path}", h.update, blocksize=FTP_BLOCK_SIZE)
        return h.hexdigest()

    def copy_to_temp(self, record, temp_path):
        self.connect()
        with open(temp_path, "wb") as f:
            self.ftp.retrbinary(f"RETR {record.source_path}", f.write, blocksize=FTP_BLOCK_SIZE)

    def close(self):
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
            self.ftp = None


class ADBSource:
    def __init__(self, adb_exe, device, root):
        self.adb_exe = adb_exe.strip() or "adb"
        self.device = device.strip()
        self.root = posixpath.normpath(root.strip() or "/sdcard")

    def _cmd(self, args):
        cmd = [self.adb_exe]
        if self.device:
            cmd.extend(["-s", self.device])
        cmd.extend(args)
        return cmd

    def _run(self, args, timeout=None):
        return run_subprocess(self._cmd(args), timeout=timeout)

    def scan(self, window):
        post_log(window, f"Scanning ADB root: {self.root}")
        find_root = adb_find_start(self.root)
        find_cmd = f"find {shell_quote(find_root)} -type f -exec stat -c '%s|%n' {{}} \\;"
        result = self._run(["shell", find_cmd])
        if result.returncode != 0 and not result.stdout.strip():
            message = result.stderr.strip() or result.stdout.strip() or "ADB file scan failed"
            raise RuntimeError(message)
        if result.stderr.strip():
            post_log(window, f"ADB warning: {result.stderr.strip()}")

        records = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            size_text, remote_path = line.split("|", 1)
            try:
                size = int(size_text)
            except ValueError:
                continue
            rel = adb_rel(self.root, remote_path)
            records[rel] = FileRecord(rel=rel, size=size, source_path=remote_path)
            if len(records) % 500 == 0:
                post_progress(window, f"Scanning ADB... {len(records):,} files", len(records), 0)
        post_log(window, f"ADB scan found {len(records):,} file(s).")
        return records

    def hash_file(self, record):
        quoted = shell_quote(record.source_path)
        for command in (f"sha256sum {quoted}", f"toybox sha256sum {quoted}", f"busybox sha256sum {quoted}"):
            result = self._run(["shell", command])
            if result.returncode == 0 and result.stdout.strip():
                first = result.stdout.strip().split()[0]
                if len(first) >= 64:
                    return first[:64].lower()
        raise RuntimeError(f"ADB could not hash: {record.source_path}")

    def copy_to_temp(self, record, temp_path):
        result = self._run(["pull", record.source_path, str(temp_path)])
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "adb pull failed"
            raise RuntimeError(message)

    def close(self):
        pass


def build_source(settings):
    mode = settings["source_mode"]
    if mode == MODE_FOLDER:
        return FolderSource(settings["source_folder"])
    if mode == MODE_FTP:
        return FTPSource(
            settings["ftp_hostport"],
            settings["ftp_root"],
            settings.get("ftp_username", ""),
            settings.get("ftp_password", ""),
        )
    if mode == MODE_ADB:
        return ADBSource(settings.get("adb_exe", ""), settings.get("adb_device", ""), settings.get("adb_root", "/sdcard"))
    raise ValueError(f"Unknown source mode: {mode}")


def scan_destination(window, dest_folder):
    root = Path(dest_folder)
    records = {}
    scanned = 0
    post_log(window, f"Scanning destination folder: {root}")
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith("$RECYCLE.BIN")]
        for filename in filenames:
            file_path = Path(current_root) / filename
            try:
                stat = file_path.stat()
                rel = normalize_rel_path(file_path.relative_to(root).as_posix())
                records[rel] = FileRecord(rel=rel, size=stat.st_size, source_path=str(file_path))
                scanned += 1
                if scanned % 500 == 0:
                    post_progress(window, f"Scanning destination... {scanned:,} files", scanned, 0)
            except Exception as e:
                post_log(window, f"Warning: skipped destination file {file_path}: {e}")
    post_log(window, f"Destination scan found {len(records):,} file(s).")
    return records


def compare_records(window, source, source_records, dest_records, dest_folder, thorough):
    issues = []
    counts = {
        "ok": 0,
        "missing": 0,
        "size": 0,
        "hash": 0,
        "total_source": len(source_records),
        "total_dest": len(dest_records),
    }
    start = time.time()
    total = len(source_records)
    mode_label = "hashes" if thorough else "sizes"

    for index, rel in enumerate(sorted(source_records), 1):
        src = source_records[rel]
        dst = dest_records.get(rel)
        post_progress(window, f"Comparing {mode_label}: {rel}", index, total, start)

        if dst is None:
            counts["missing"] += 1
            issues.append(
                {
                    "issue": ISSUE_MISSING,
                    "rel": rel,
                    "source_size": src.size,
                    "dest_size": None,
                    "record": src,
                }
            )
            continue

        if src.size != dst.size:
            counts["size"] += 1
            issues.append(
                {
                    "issue": ISSUE_SIZE,
                    "rel": rel,
                    "source_size": src.size,
                    "dest_size": dst.size,
                    "record": src,
                }
            )
            continue

        if thorough:
            source_hash = source.hash_file(src)
            dest_hash = hash_local_file(dest_path_for_rel(dest_folder, rel))
            if source_hash != dest_hash:
                counts["hash"] += 1
                issues.append(
                    {
                        "issue": ISSUE_HASH,
                        "rel": rel,
                        "source_size": src.size,
                        "dest_size": dst.size,
                        "record": src,
                    }
                )
                continue

        counts["ok"] += 1

    return counts, issues


def scan_compare_worker(window, settings):
    thorough = bool(settings.get("thorough", False))
    post_log(window)
    post_log(window, "=" * 60)
    post_log(window, f"Starting scan/compare in {settings['source_mode']} mode")
    post_log(window, f"Compare mode: {'Thorough SHA-256 hash check' if thorough else 'Fast size check'}")
    if settings["source_mode"] == MODE_FTP and thorough:
        post_log(window, "FTP thorough mode reads each matching source file over WiFi to calculate hashes.")
    post_log(window, "=" * 60)
    post_progress(window, "Starting scan...", 0, 100)

    source = build_source(settings)
    try:
        source_records = source.scan(window)
        dest_records = scan_destination(window, settings["dest_folder"])
        counts, issues = compare_records(window, source, source_records, dest_records, settings["dest_folder"], thorough)
    finally:
        source.close()

    post_progress(window, "Scan complete", 100, 100)
    post_log(window)
    post_log(window, "Scan complete.")
    post_log(window, summary_text(counts))
    if issues:
        post_log(window, f"Issues found: {len(issues):,}")
    else:
        post_log(window, "Everything matches.")
    return {"counts": counts, "issues": issues, "settings": settings}


def copy_worker(window, settings, issues_to_copy):
    if not issues_to_copy:
        return {"copied": 0, "failed": 0, "settings": settings}

    post_log(window)
    post_log(window, "=" * 60)
    post_log(window, f"Starting copy run: {len(issues_to_copy):,} file(s)")
    post_log(window, "=" * 60)

    source = build_source(settings)
    dest_root = Path(settings["dest_folder"])
    copied = 0
    failed = 0
    start = time.time()

    try:
        if isinstance(source, FTPSource):
            source.connect(window)

        for index, issue in enumerate(issues_to_copy, 1):
            record = issue["record"]
            dest_path = dest_path_for_rel(dest_root, record.rel)
            temp_path = dest_path.with_name(dest_path.name + ".rp5sync_tmp")
            post_progress(window, f"Copying: {record.rel}", index, len(issues_to_copy), start)

            try:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                if temp_path.exists():
                    temp_path.unlink()
                source.copy_to_temp(record, temp_path)
                copied_size = temp_path.stat().st_size
                if copied_size != record.size:
                    raise RuntimeError(f"copied size {copied_size} did not match source size {record.size}")
                os.replace(temp_path, dest_path)
                final_size = dest_path.stat().st_size
                if final_size != record.size:
                    raise RuntimeError(f"final size {final_size} did not match source size {record.size}")
                copied += 1
                post_log(window, f"Copied and verified: {record.rel}")
            except Exception as e:
                failed += 1
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass
                post_log(window, f"FAILED: {record.rel} -- {e}")
    finally:
        source.close()

    post_progress(window, "Copy run complete", 100, 100)
    post_log(window)
    post_log(window, f"Copy run complete. Copied: {copied:,} | Failed: {failed:,}")
    return {"copied": copied, "failed": failed, "settings": settings}


def detect_adb_devices_worker(window, adb_exe):
    exe = adb_exe.strip() or "adb"
    post_log(window, f"Checking ADB devices using: {exe}")
    result = run_subprocess([exe, "devices"], timeout=15)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "adb devices failed"
        raise RuntimeError(message)

    devices = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append({"serial": parts[0], "status": parts[1]})
    for device in devices:
        if device.get("status") == "device":
            device["storage_roots"] = detect_adb_storage_roots(exe, device["serial"])
    return {"devices": devices}


def detect_adb_storage_roots(adb_exe, serial):
    command = (
        "for p in /sdcard /storage/emulated/0 /storage/self/primary /storage/*; do "
        "[ -d \"$p\" ] && echo \"$p\"; "
        "done 2>/dev/null"
    )
    result = run_subprocess([adb_exe, "-s", serial, "shell", command], timeout=15)
    if result.returncode != 0:
        return []
    roots = []
    seen = set()
    for line in result.stdout.splitlines():
        root = line.strip()
        if not root or "*" in root:
            continue
        normalized = posixpath.normpath(root)
        if normalized not in seen:
            roots.append(normalized)
            seen.add(normalized)
    return roots


def summary_text(counts):
    return (
        f"Source: {counts.get('total_source', 0):,} | "
        f"Destination: {counts.get('total_dest', 0):,} | "
        f"OK: {counts.get('ok', 0):,} | "
        f"Missing: {counts.get('missing', 0):,} | "
        f"Size: {counts.get('size', 0):,} | "
        f"Hash: {counts.get('hash', 0):,}"
    )


def issue_row(issue):
    return [
        issue["issue"],
        issue["rel"],
        bytes_label(issue.get("source_size")),
        bytes_label(issue.get("dest_size")),
    ]


def gather_settings(values):
    settings = {
        "source_mode": values.get("SOURCE_MODE", MODE_FOLDER),
        "source_folder": values.get("SOURCE_FOLDER", "").strip(),
        "dest_folder": values.get("DEST_FOLDER", "").strip(),
        "ftp_hostport": values.get("FTP_HOSTPORT", "").strip(),
        "ftp_root": values.get("FTP_ROOT", "/").strip() or "/",
        "ftp_username": values.get("FTP_USERNAME", "").strip(),
        "ftp_password": values.get("FTP_PASSWORD", ""),
        "adb_exe": values.get("ADB_EXE", "").strip(),
        "adb_device": values.get("ADB_DEVICE", "").strip(),
        "adb_root": values.get("ADB_ROOT", "/sdcard").strip() or "/sdcard",
        "thorough": bool(values.get("THOROUGH", False)),
        "filter": values.get("FILTER", "All"),
    }
    return settings


def validate_settings(settings):
    dest = settings["dest_folder"]
    if not dest:
        return False, "Please select your PC backup destination folder."
    if not Path(dest).exists():
        return False, "Destination folder does not exist."

    mode = settings["source_mode"]
    if mode == MODE_FOLDER:
        source = settings["source_folder"]
        if not source:
            return False, "Please select the source folder or drive."
        if not Path(source).exists():
            return False, "Source folder does not exist."
    elif mode == MODE_FTP:
        if not settings["ftp_hostport"]:
            return False, "Please enter the RP5 FTP IP and port."
        try:
            parse_hostport(settings["ftp_hostport"])
        except Exception as e:
            return False, str(e)
    elif mode == MODE_ADB:
        if not settings["adb_root"]:
            return False, "Please enter the ADB source path."
    else:
        return False, f"Unknown source mode: {mode}"
    return True, "OK"


def settings_signature(settings):
    keys = [
        "source_mode",
        "source_folder",
        "dest_folder",
        "ftp_hostport",
        "ftp_root",
        "ftp_username",
        "ftp_password",
        "adb_exe",
        "adb_device",
        "adb_root",
        "thorough",
    ]
    return json.dumps({key: settings.get(key) for key in keys}, sort_keys=True)


def make_layout():
    mode = config.get("source_mode", MODE_FOLDER)
    thorough = bool(config.get("thorough", False))

    folder_panel = sg.Column(
        [
            [
                sg.Text("Source Folder:", size=(14, 1)),
                sg.Input(config.get("source_folder", ""), key="SOURCE_FOLDER", size=(56, 1)),
                sg.FolderBrowse(),
            ]
        ],
        key="FOLDER_PANEL",
        visible=mode == MODE_FOLDER,
        pad=(0, 0),
    )

    ftp_panel = sg.Column(
        [
            [
                sg.Text("FTP IP:Port:", size=(14, 1)),
                sg.Input(config.get("ftp_hostport", ""), key="FTP_HOSTPORT", size=(20, 1)),
                sg.Text("Root:", size=(5, 1)),
                sg.Input(config.get("ftp_root", "/"), key="FTP_ROOT", size=(18, 1)),
            ],
            [
                sg.Text("FTP Login:", size=(14, 1)),
                sg.Input(config.get("ftp_username", ""), key="FTP_USERNAME", size=(20, 1)),
                sg.Text("Password:", size=(8, 1)),
                sg.Input(config.get("ftp_password", ""), key="FTP_PASSWORD", size=(18, 1), password_char="*"),
            ],
        ],
        key="FTP_PANEL",
        visible=mode == MODE_FTP,
        pad=(0, 0),
    )

    adb_panel = sg.Column(
        [
            [
                sg.Text("ADB exe:", size=(14, 1)),
                sg.Input(config.get("adb_exe", ""), key="ADB_EXE", size=(42, 1)),
                sg.FileBrowse(file_types=(("adb.exe", "adb.exe"), ("Executable", "*.exe"), ("All Files", "*.*"))),
                sg.Button("Detect Devices", key="DETECT_ADB"),
            ],
            [
                sg.Text("Device:", size=(14, 1)),
                sg.Combo(
                    [config.get("adb_device", "")] if config.get("adb_device", "") else [],
                    default_value=config.get("adb_device", ""),
                    key="ADB_DEVICE",
                    size=(30, 1),
                ),
                sg.Text("Root:", size=(5, 1)),
                sg.Input(config.get("adb_root", "/sdcard"), key="ADB_ROOT", size=(20, 1)),
            ],
        ],
        key="ADB_PANEL",
        visible=mode == MODE_ADB,
        pad=(0, 0),
    )

    sync_tab = [
        [
            sg.Text("Source Mode:", size=(14, 1)),
            sg.Combo(SOURCE_MODES, default_value=mode, key="SOURCE_MODE", readonly=True, enable_events=True, size=(18, 1)),
            sg.Radio("Fast", "COMPARE_MODE", key="FAST", default=not thorough),
            sg.Radio("Thorough", "COMPARE_MODE", key="THOROUGH", default=thorough),
        ],
        [folder_panel],
        [ftp_panel],
        [adb_panel],
        [
            sg.Text("PC Backup:", size=(14, 1)),
            sg.Input(config.get("dest_folder", ""), key="DEST_FOLDER", size=(56, 1)),
            sg.FolderBrowse(),
        ],
        [sg.HorizontalSeparator()],
        [
            sg.Button("Scan / Compare", key="SCAN", size=(15, 1)),
            sg.Button("Copy ALL Issues", key="COPY_ALL", size=(15, 1), disabled=True),
            sg.Button("Copy Selected", key="COPY_SELECTED", size=(14, 1), disabled=True),
            sg.Text("Filter:", pad=((18, 4), 0)),
            sg.Combo(ISSUE_FILTERS, default_value=config.get("filter", "All"), key="FILTER", readonly=True, enable_events=True, size=(16, 1)),
        ],
        [sg.Text("No scan yet.", key="SUMMARY", size=(92, 1))],
        [sg.Text("Idle", key="PROGRESS_TEXT", size=(92, 1))],
        [sg.ProgressBar(100, orientation="h", size=(68, 16), key="PROGRESS")],
        [
            sg.Table(
                values=[],
                headings=["Issue", "File", "Source Size", "Dest Size"],
                key="ISSUES",
                num_rows=15,
                col_widths=[16, 64, 13, 13],
                auto_size_columns=False,
                justification="left",
                select_mode=sg.TABLE_SELECT_MODE_EXTENDED,
                enable_events=True,
                expand_x=True,
            )
        ],
        [sg.Text("Showing 0 issue(s)", key="ISSUE_COUNT", size=(92, 1))],
    ]

    help_tab = [
        [sg.Text("Folder/Drive", font=("Arial", 10, "bold"))],
        [sg.Text("Use this when the SD card is mounted through a card reader or real Windows drive letter.")],
        [sg.Text("")],
        [sg.Text("FTP (WiFi)", font=("Arial", 10, "bold"))],
        [sg.Text("Start an FTP server app on the RP5, enter its IP:Port, then scan from the folder it exposes.")],
        [sg.Text("")],
        [sg.Text("ADB (USB)", font=("Arial", 10, "bold"))],
        [sg.Text("Enable USB Debugging on the RP5, connect USB, then use Detect Devices or browse to adb.exe.")],
        [sg.Text("Default ADB root is /sdcard.")],
        [sg.Text("For removable cards, use the /storage/... root printed by Detect Devices.")],
    ]

    layout = [
        [sg.Text(APP_TITLE, font=("Arial", 13, "bold"))],
        [
            sg.TabGroup(
                [[sg.Tab("  Sync Checker  ", sync_tab), sg.Tab("  Connection Help  ", help_tab)]],
                expand_x=True,
            )
        ],
        [sg.Output(size=(112, 13), key="OUTPUT")],
        [sg.Button("Clear Output", key="CLEAR_OUTPUT"), sg.Button("Exit")],
    ]
    return layout


def update_source_panels(window, mode):
    window["FOLDER_PANEL"].update(visible=mode == MODE_FOLDER)
    window["FTP_PANEL"].update(visible=mode == MODE_FTP)
    window["ADB_PANEL"].update(visible=mode == MODE_ADB)


def update_buttons(window, busy, has_issues):
    for key in ("SCAN", "DETECT_ADB"):
        window[key].update(disabled=busy)
    for key in ("COPY_ALL", "COPY_SELECTED"):
        window[key].update(disabled=busy or not has_issues)


def update_issue_table(window, state):
    selected_filter = window["FILTER"].get()
    issues = state["issues"]
    if selected_filter == "All":
        visible = list(issues)
    else:
        visible = [issue for issue in issues if issue["issue"] == selected_filter]
    state["visible_issues"] = visible
    window["ISSUES"].update(values=[issue_row(issue) for issue in visible])
    if selected_filter == "All":
        window["ISSUE_COUNT"].update(f"Showing {len(visible):,} issue(s)")
    else:
        window["ISSUE_COUNT"].update(f"Showing {len(visible):,} of {len(issues):,} issue(s)")


def start_background(window, state, task, func, *args):
    if state["busy"]:
        sg.popup_error("A scan or copy run is already in progress.")
        return False
    state["busy"] = True
    update_buttons(window, True, bool(state["issues"]))
    threading.Thread(target=run_worker, args=(window, task, func, *args), daemon=True).start()
    return True


def start_scan(window, state, settings, auto=False):
    valid, message = validate_settings(settings)
    if not valid:
        sg.popup_error(message)
        return False
    save_config(settings=settings)
    if auto:
        post_log(window, "Auto-rescanning after copy run.")
    state["scan_signature"] = settings_signature(settings)
    return start_background(window, state, "scan", scan_compare_worker, settings)


def start_copy(window, state, settings, selected_issues):
    if not selected_issues:
        sg.popup_error("No issues selected to copy.")
        return False
    valid, message = validate_settings(settings)
    if not valid:
        sg.popup_error(message)
        return False
    if state.get("scan_signature") != settings_signature(settings):
        sg.popup_error("Settings changed since the last scan. Run Scan / Compare again before copying.")
        return False
    save_config(settings=settings)
    return start_background(window, state, "copy", copy_worker, settings, selected_issues)


def main():
    window = sg.Window(APP_TITLE, make_layout(), finalize=True)
    update_source_panels(window, config.get("source_mode", MODE_FOLDER))

    state = {
        "busy": False,
        "issues": [],
        "visible_issues": [],
        "counts": None,
        "scan_signature": None,
        "last_scan_settings": None,
    }

    print(f"[{timestamp()}] Ready.")

    while True:
        event, values = window.read()

        if event == sg.WINDOW_CLOSED or event == "Exit":
            save_config(values=values)
            break

        if event == "CLEAR_OUTPUT":
            window["OUTPUT"].update("")

        if event == "SOURCE_MODE":
            update_source_panels(window, values["SOURCE_MODE"])
            save_config(values=values)

        if event == "FILTER":
            save_config(values=values)
            update_issue_table(window, state)

        if event == "DETECT_ADB":
            save_config(values=values)
            start_background(window, state, "adb_detect", detect_adb_devices_worker, values.get("ADB_EXE", ""))

        if event == "SCAN":
            settings = gather_settings(values)
            state["issues"] = []
            state["visible_issues"] = []
            window["ISSUES"].update(values=[])
            window["SUMMARY"].update("Scanning...")
            window["ISSUE_COUNT"].update("Showing 0 issue(s)")
            start_scan(window, state, settings)

        if event == "COPY_ALL":
            settings = gather_settings(values)
            start_copy(window, state, settings, list(state["issues"]))

        if event == "COPY_SELECTED":
            selected_rows = values.get("ISSUES", [])
            selected_issues = [state["visible_issues"][i] for i in selected_rows if i < len(state["visible_issues"])]
            settings = gather_settings(values)
            start_copy(window, state, settings, selected_issues)

        if event == "-LOG-":
            message = values["-LOG-"]
            lines = message.splitlines() or [""]
            for line in lines:
                print(f"[{timestamp()}] {line}")

        if event == "-PROGRESS-":
            progress = values["-PROGRESS-"]
            current = progress.get("current", 0)
            total = progress.get("total", 0)
            label = progress.get("label", "")
            eta = progress.get("eta", "")
            if total:
                pct = int(min(max(current / total, 0), 1) * 100)
            else:
                pct = 0
            window["PROGRESS"].update_bar(pct)
            if total and total != 100:
                window["PROGRESS_TEXT"].update(f"{label}  ({current:,}/{total:,}){eta}")
            else:
                window["PROGRESS_TEXT"].update(f"{label}{eta}")

        if event == "-WORKER_DONE-":
            data = values["-WORKER_DONE-"]
            state["busy"] = False
            task = data["task"]

            if not data["ok"]:
                print(f"[{timestamp()}] ERROR during {task}:")
                for line in data["error"].splitlines():
                    print(f"[{timestamp()}] {line}")
                sg.popup_error(f"{task} failed. Check the output log for details.")
                window["PROGRESS_TEXT"].update("Stopped after error")
                update_buttons(window, False, bool(state["issues"]))
                continue

            payload = data.get("payload") or {}

            if task == "scan":
                state["counts"] = payload["counts"]
                state["issues"] = payload["issues"]
                state["last_scan_settings"] = payload["settings"]
                state["scan_signature"] = settings_signature(payload["settings"])
                window["SUMMARY"].update(summary_text(state["counts"]))
                update_issue_table(window, state)
                update_buttons(window, False, bool(state["issues"]))

            elif task == "copy":
                update_buttons(window, False, bool(state["issues"]))
                if payload.get("copied", 0) > 0 and payload.get("failed", 0) == 0:
                    start_scan(window, state, payload["settings"], auto=True)

            elif task == "adb_detect":
                devices = payload.get("devices", [])
                ready_devices = [device["serial"] for device in devices if device.get("status") == "device"]
                if ready_devices:
                    window["ADB_DEVICE"].update(values=ready_devices, value=ready_devices[0])
                    print(f"[{timestamp()}] ADB device(s): {', '.join(ready_devices)}")
                    for device in devices:
                        roots = device.get("storage_roots") or []
                        if device.get("status") == "device" and roots:
                            print(f"[{timestamp()}] Storage roots for {device['serial']}: {', '.join(roots)}")
                else:
                    window["ADB_DEVICE"].update(values=[], value="")
                    if devices:
                        statuses = ", ".join(f"{d['serial']} ({d['status']})" for d in devices)
                        print(f"[{timestamp()}] ADB found no ready devices: {statuses}")
                    else:
                        print(f"[{timestamp()}] ADB found no devices.")
                update_buttons(window, False, bool(state["issues"]))

    window.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            sg.popup_error(APP_TITLE, "The app hit an unexpected error:", str(e), traceback.format_exc())
        except Exception:
            raise
