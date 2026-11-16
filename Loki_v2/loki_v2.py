"""
LOKI 2.0
========
Local Windows desktop AI assistant.

This version is self-contained:
- No Loki_v1 import/dependency.
- Dual-model routing:
    * llama3                    -> fast model
    * mistral-small3.2:24b      -> reasoning model
- Python tool gateway with explicit permissions.
- Thread-safe Ollama requests.
- COM initialization for Pycaw worker threads.
- Persistent conversation + long-term memory.
- PC, GPU, storage, process, network and audio tools.
- Approved application launcher.
- Read-first file management.
- Confirmation before destructive/system-changing actions.
- Futuristic dark/blue CustomTkinter UI.

Required packages:
    pip install customtkinter requests psutil comtypes pycaw

Ollama:
    Make sure both configured models are installed and Ollama is running.
"""

from __future__ import annotations

# =============================================================================
# IMPORTS
# =============================================================================

import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import comtypes
import customtkinter as ctk
import psutil
import requests
from tkinter import messagebox

try:
    from pycaw.pycaw import AudioUtilities

    PYCAW_AVAILABLE = True
    PYCAW_ERROR = ""
except Exception as exc:
    AudioUtilities = None
    PYCAW_AVAILABLE = False
    PYCAW_ERROR = str(exc)


# =============================================================================
# CONFIGURATION
# =============================================================================

APP_NAME = "Loki"

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
ROUTER_LOGGER = logging.getLogger("loki.router")


def load_json_config(filename: str) -> dict[str, Any]:
    """Load a required JSON configuration object from config/."""
    path = CONFIG_DIR / filename
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing Loki configuration file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{filename} must contain a JSON object.")
    return data


MODELS_CONFIG = load_json_config("models.json")
ROUTER_CONFIG = load_json_config("router.json")
PROMPTS_CONFIG = load_json_config("prompts.json")
TOOLS_CONFIG = load_json_config("tools.json")
APP_CONFIG = load_json_config("apps.json")

OLLAMA_URL = str(MODELS_CONFIG["ollama_url"])
FAST_MODEL = str(MODELS_CONFIG["models"]["fast"]["name"])
REASONING_MODEL = str(MODELS_CONFIG["models"]["reasoning"]["name"])
CLASSIFIER_MODEL = str(MODELS_CONFIG["models"]["classifier"]["name"])

MEMORY_FILE = BASE_DIR / "memory.json"
LONG_MEMORY_FILE = BASE_DIR / "long_memory.json"
TASK_FILE = BASE_DIR / "tasks.json"

MAX_HISTORY = 70
MAX_LONG_MEMORY = 150
MAX_CONTEXT_MESSAGES = 30
MAX_TOOL_OUTPUT = 12000
MAX_FILE_RESULTS = 50

WINDOW_SMALL = (70, 70)
WINDOW_LARGE = (440, 610)

OLLAMA_TIMEOUT = int(MODELS_CONFIG["timeouts"]["generation_seconds"])
TOOL_MODEL_TIMEOUT = 60

ROUTE_SIMPLE = "simple"
ROUTE_COMPLEX = "reasoning"
ROUTE_TOOL = "tool"

PERMISSION_AUTO = "auto"
PERMISSION_REQUEST = "request"
PERMISSION_CONFIRM = "confirm"


@dataclass
class RouteDecision:
    route: str
    confidence: float
    reason: str
    scores: dict[str, float]
    source: str

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Purposeful, low-noise command-console palette.
COLOR_WINDOW = "#0A0D12"
COLOR_PANEL = "#10151D"
COLOR_SURFACE = "#161D27"
COLOR_SURFACE_HOVER = "#1D2632"
COLOR_BORDER = "#263242"
COLOR_TEXT = "#E7EDF7"
COLOR_MUTED = "#8491A3"
COLOR_ACCENT = "#5BA9FF"
COLOR_ACCENT_HOVER = "#3F8FE8"
COLOR_SUCCESS = "#5BD6A2"


# =============================================================================
# GENERAL UTILITIES
# =============================================================================

def truncate(text: Any, limit: int = MAX_TOOL_OUTPUT) -> str:
    """Return text with a hard maximum length."""
    value = str(text).strip()

    if len(value) <= limit:
        return value

    return value[:limit] + "\n\n[Output truncated by Loki.]"


def run_command(
    command: list[str],
    timeout: int = 8,
) -> tuple[str, int]:
    """Run a Windows command without invoking a shell."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        return result.stdout.strip(), result.returncode
    except (OSError, subprocess.SubprocessError):
        return "", 1


def format_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, seconds = divmod(total, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")

    return " ".join(parts)


def safe_resolve(path: str | Path) -> Path:
    """Expand ~ and resolve a Windows path."""
    return Path(path).expanduser().resolve()


# =============================================================================
# MEMORY
# =============================================================================

class MemoryManager:
    """Thread-safe JSON-backed conversation and long-term memory."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.conversation = self._load_list(MEMORY_FILE)
        self.long_term = self._load_list(LONG_MEMORY_FILE)

    @staticmethod
    def _load_list(path: Path) -> list[str]:
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)

            if isinstance(data, list):
                return [str(item) for item in data]

        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

        return []

    @staticmethod
    def _save_list(path: Path, data: list[str]) -> None:
        try:
            with path.open("w", encoding="utf-8") as file:
                json.dump(
                    data,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )
        except OSError as exc:
            print(f"[Loki] Could not save {path.name}: {exc}")

    def add_message(self, role: str, text: str) -> None:
        with self.lock:
            self.conversation.append(
                f"{role}: {str(text).strip()}"
            )

            if len(self.conversation) > MAX_HISTORY:
                self.conversation = self.conversation[-MAX_HISTORY:]

            self._save_list(
                MEMORY_FILE,
                self.conversation,
            )

    def remember(self, text: str) -> bool:
        value = str(text).strip()

        if not value:
            return False

        with self.lock:
            if any(
                item.casefold() == value.casefold()
                for item in self.long_term
            ):
                return False

            self.long_term.append(value)
            self.long_term = self.long_term[-MAX_LONG_MEMORY:]

            self._save_list(
                LONG_MEMORY_FILE,
                self.long_term,
            )

        return True

    def forget(self, text: str) -> bool:
        needle = str(text).strip().casefold()

        if not needle:
            return False

        with self.lock:
            original = len(self.long_term)

            self.long_term = [
                item
                for item in self.long_term
                if needle not in item.casefold()
            ]

            changed = len(self.long_term) != original

            if changed:
                self._save_list(
                    LONG_MEMORY_FILE,
                    self.long_term,
                )

            return changed

    def clear_conversation(self) -> None:
        with self.lock:
            self.conversation.clear()
            self._save_list(
                MEMORY_FILE,
                self.conversation,
            )

    def build_context(self) -> str:
        with self.lock:
            memories = self.long_term[-MAX_LONG_MEMORY:]
            recent = self.conversation[-MAX_CONTEXT_MESSAGES:]

        memory_text = "\n".join(memories) or "(none)"
        conversation_text = "\n".join(recent) or "(none)"

        return (
            "LONG-TERM MEMORY:\n"
            f"{memory_text}\n\n"
            "RECENT CONVERSATION:\n"
            f"{conversation_text}"
        )


memory = MemoryManager()


# =============================================================================
# SYSTEM / PC INFORMATION
# =============================================================================

def get_cpu_name() -> str:
    output, code = run_command(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_Processor).Name",
        ]
    )

    if code == 0 and output:
        return output.splitlines()[0].strip()

    return platform.processor() or "Unknown CPU"


def get_cpu_details() -> str:
    frequency = psutil.cpu_freq()

    lines = [
        "CPU",
        f"Model: {get_cpu_name()}",
        (
            f"Cores: {psutil.cpu_count(logical=False) or 'Unknown'} "
            f"physical / {psutil.cpu_count(logical=True) or 'Unknown'} logical"
        ),
    ]

    if frequency:
        lines.append(f"Current frequency: {frequency.current:.0f} MHz")

    return "\n".join(lines)


def get_gpu_info() -> str:
    """Read NVIDIA GPU data using nvidia-smi."""
    output, code = run_command(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,memory.free,"
            "utilization.gpu,temperature.gpu,driver_version",
            "--format=csv,noheader,nounits",
        ],
        timeout=10,
    )

    if code != 0 or not output:
        return "NVIDIA GPU information unavailable."

    lines = []

    for row in output.splitlines():
        parts = [part.strip() for part in row.split(",")]

        if len(parts) < 7:
            continue

        lines.append(
            f"{parts[0]} | "
            f"VRAM {parts[2]} / {parts[1]} MB | "
            f"GPU {parts[4]}% | "
            f"Temp {parts[5]}°C | "
            f"Driver {parts[6]}"
        )

    return "\n".join(lines) or output


def get_system_info() -> str:
    memory_info = psutil.virtual_memory()
    uptime = time.time() - psutil.boot_time()

    return (
        "SYSTEM\n"
        f"OS: {platform.system()} {platform.release()}\n"
        f"Windows build: {platform.version()}\n"
        f"CPU: {get_cpu_name()}\n"
        f"Physical / logical cores: "
        f"{psutil.cpu_count(logical=False)} / "
        f"{psutil.cpu_count(logical=True)}\n"
        f"RAM: {memory_info.total / (1024 ** 3):.1f} GB\n"
        f"GPU: {get_gpu_info()}\n"
        f"Uptime: {format_uptime(uptime)}"
    )


def get_resource_usage() -> str:
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()

    return (
        "CURRENT RESOURCE USAGE\n"
        f"CPU: {cpu:.1f}%\n"
        f"RAM: {ram.percent:.1f}% "
        f"({ram.used / (1024 ** 3):.1f} / "
        f"{ram.total / (1024 ** 3):.1f} GB)"
    )


def get_disk_info() -> str:
    lines = ["STORAGE"]

    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue

        lines.append(
            f"{partition.mountpoint} | "
            f"{partition.fstype or 'Unknown'} | "
            f"Total {usage.total / (1024 ** 3):.1f} GB | "
            f"Used {usage.used / (1024 ** 3):.1f} GB | "
            f"Free {usage.free / (1024 ** 3):.1f} GB | "
            f"{usage.percent:.1f}% used"
        )

    if len(lines) == 1:
        lines.append("No accessible mounted drives.")

    return "\n".join(lines)


def get_battery_info() -> str:
    battery = psutil.sensors_battery()

    if battery is None:
        return "No battery detected. This may be a desktop PC."

    status = "charging" if battery.power_plugged else "not charging"

    return (
        "BATTERY\n"
        f"Level: {battery.percent:.0f}%\n"
        f"Status: {status}"
    )


def get_network_info() -> str:
    hostname = socket.gethostname()

    try:
        local_ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        local_ip = "Unavailable"

    lines = [
        "NETWORK",
        f"Hostname: {hostname}",
        f"Local IP: {local_ip}",
    ]

    for interface, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if address.family == socket.AF_INET:
                lines.append(
                    f"{interface}: {address.address}"
                )

    return "\n".join(lines)


def get_network_status() -> str:
    lines = ["NETWORK ADAPTERS"]

    for name, data in psutil.net_if_stats().items():
        state = "up" if data.isup else "down"
        speed = f" | {data.speed} Mbps" if data.speed else ""
        lines.append(f"{name}: {state}{speed}")

    return "\n".join(lines)


def check_internet() -> str:
    try:
        with socket.create_connection(
            ("1.1.1.1", 443),
            timeout=3,
        ):
            pass

        return "Internet connectivity: OK"
    except OSError:
        return "Internet connectivity: unavailable"


def get_processes() -> str:
    rows = []

    for process in psutil.process_iter(
        ["pid", "name", "memory_info", "cpu_percent"]
    ):
        try:
            info = process.info
            memory_info = info["memory_info"]

            memory_mb = (
                memory_info.rss / (1024 ** 2)
                if memory_info
                else 0
            )

            rows.append(
                (
                    memory_mb,
                    info["pid"],
                    info["name"] or "Unknown",
                    info["cpu_percent"] or 0,
                )
            )

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    rows.sort(reverse=True)

    lines = ["TOP PROCESSES BY RAM"]

    for memory_mb, pid, name, cpu in rows[:12]:
        lines.append(
            f"{name} | PID {pid} | "
            f"RAM {memory_mb:.0f} MB | CPU {cpu:.1f}%"
        )

    return "\n".join(lines)


def process_running(name: str) -> str:
    needle = str(name).strip().casefold()

    if not needle:
        return "No process name supplied."

    matches = []

    for process in psutil.process_iter(["pid", "name"]):
        try:
            process_name = (
                process.info["name"] or ""
            )

            if needle in process_name.casefold():
                matches.append(
                    f"{process_name} (PID {process.info['pid']})"
                )

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not matches:
        return f"No running process matched '{name}'."

    return "\n".join(matches[:20])


# =============================================================================
# APPLICATION REGISTRY
# =============================================================================

APP_REGISTRY: dict[str, dict[str, Any]] = APP_CONFIG.get("apps", {})
APP_ALIASES: dict[str, str] = {
    str(alias).casefold(): str(target).casefold()
    for alias, target in APP_CONFIG.get("aliases", {}).items()
}


def resolve_app_name(name: str) -> str | None:
    key = str(name).strip().casefold()

    if key in APP_REGISTRY:
        return key

    return APP_ALIASES.get(key)


def open_app(app_name: str) -> str:
    key = resolve_app_name(app_name)

    if not key:
        approved = ", ".join(sorted(APP_REGISTRY))
        return (
            f"'{app_name}' is not in Loki's approved app registry.\n"
            f"Approved apps: {approved}"
        )

    entry = APP_REGISTRY[key]

    try:
        if entry["type"] == "command":
            command = entry["command"]
            command = command if isinstance(command, list) else [command]
            subprocess.Popen([os.path.expandvars(str(part)) for part in command])
            return f"Opened {key}."

        if entry["type"] in {"paths", "path"}:
            for candidate in entry["paths"]:
                candidate = Path(os.path.expandvars(str(candidate))).expanduser()

                if not candidate.exists():
                    continue

                command = [str(candidate)]
                args = entry.get("args", entry.get("arguments", ""))
                if isinstance(args, str):
                    args = [args] if args else []
                command.extend(str(arg) for arg in args)

                subprocess.Popen(command)
                return f"Opened {key}."

            return (
                f"I couldn't find the installed executable for {key}."
            )

        return f"Invalid registry entry for {key}."

    except OSError as exc:
        return f"Could not open {key}: {exc}"


def close_process(name: str) -> str:
    needle = str(name).strip().casefold()

    if not needle:
        return "No process name supplied."

    matches = []

    for process in psutil.process_iter(["pid", "name"]):
        try:
            process_name = process.info["name"] or ""

            if needle in process_name.casefold():
                matches.append(process)

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not matches:
        return f"No running process matched '{name}'."

    terminated = 0

    for process in matches[:10]:
        try:
            process.terminate()
            terminated += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return f"Requested termination of {terminated} process(es)."


# =============================================================================
# AUDIO
# =============================================================================

def get_audio_devices() -> str:
    if not PYCAW_AVAILABLE:
        return (
            "Audio support is unavailable.\n"
            f"Reason: {PYCAW_ERROR}"
        )

    try:
        from pycaw.constants import DEVICE_STATE, EDataFlow

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)

            devices = AudioUtilities.GetAllDevices(
                data_flow=EDataFlow.eRender.value,
                device_state=DEVICE_STATE.ACTIVE.value,
            )

        default_device = AudioUtilities.GetSpeakers()
        default_id = getattr(default_device, "id", None)

        lines = ["AUDIO OUTPUT DEVICES"]

        for device in devices:
            name = getattr(
                device,
                "FriendlyName",
                getattr(device, "name", "Unknown device"),
            )

            marker = (
                "*"
                if getattr(device, "id", None) == default_id
                else "-"
            )

            lines.append(f"{marker} {name}")

        return "\n".join(lines)

    except Exception as exc:
        return f"Could not list audio devices: {exc}"


def get_audio_info() -> str:
    if not PYCAW_AVAILABLE:
        return (
            "Audio support is unavailable.\n"
            f"Reason: {PYCAW_ERROR}"
        )

    try:
        device = AudioUtilities.GetSpeakers()
        volume = device.EndpointVolume

        name = getattr(
            device,
            "FriendlyName",
            getattr(device, "name", "Default audio device"),
        )

        muted = bool(volume.GetMute())

        try:
            level = float(device.volume_percent)
        except (AttributeError, TypeError, ValueError):
            level = (
                volume.GetMasterVolumeLevelScalar()
                * 100
            )

        return (
            "AUDIO\n"
            f"Output: {name}\n"
            f"Volume: {level:.0f}%\n"
            f"Muted: {'yes' if muted else 'no'}"
        )

    except Exception as exc:
        return f"Could not read audio status: {exc}"


def set_volume(value: str) -> str:
    if not PYCAW_AVAILABLE:
        return "Audio support is unavailable."

    try:
        level = int(str(value).strip())
    except ValueError:
        return "Volume must be a number from 0 to 100."

    if not 0 <= level <= 100:
        return "Volume must be between 0 and 100."

    try:
        device = AudioUtilities.GetSpeakers()
        device.EndpointVolume.SetMasterVolumeLevelScalar(
            level / 100.0,
            None,
        )

        return f"Volume set to {level}%."

    except Exception as exc:
        return f"Could not change volume: {exc}"


def mute_audio(muted: bool) -> str:
    if not PYCAW_AVAILABLE:
        return "Audio support is unavailable."

    try:
        device = AudioUtilities.GetSpeakers()
        device.EndpointVolume.SetMute(
            1 if muted else 0,
            None,
        )

        return "Audio muted." if muted else "Audio unmuted."

    except Exception as exc:
        return f"Could not change mute state: {exc}"


def switch_audio_device(device_name: str) -> str:
    """
    Switch the default render device.

    Some pycaw versions do not expose SetDefaultDevice. The function
    therefore checks for it explicitly instead of failing unexpectedly.
    """
    if not PYCAW_AVAILABLE:
        return "Audio support is unavailable."

    try:
        from pycaw.constants import DEVICE_STATE, EDataFlow

        target = str(device_name).strip().casefold()

        if not target:
            return "No audio device name supplied."

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)

            devices = AudioUtilities.GetAllDevices(
                data_flow=EDataFlow.eRender.value,
                device_state=DEVICE_STATE.ACTIVE.value,
            )

        matches = [
            device
            for device in devices
            if target in getattr(
                device,
                "FriendlyName",
                "",
            ).casefold()
        ]

        if not matches:
            available = ", ".join(
                getattr(device, "FriendlyName", "Unknown")
                for device in devices
            )

            return (
                f"I couldn't find an output device matching "
                f"'{device_name}'.\n"
                f"Available devices: {available or 'none'}"
            )

        setter = getattr(
            AudioUtilities,
            "SetDefaultDevice",
            None,
        )

        if setter is None:
            return (
                "This installed pycaw version can list audio devices "
                "but does not expose default-device switching."
            )

        selected = matches[0]
        setter(selected.id)

        return (
            "Default audio output switched to "
            f"{getattr(selected, 'FriendlyName', device_name)}."
        )

    except Exception as exc:
        return f"Could not switch audio device: {exc}"


# =============================================================================
# FILE MANAGEMENT
# =============================================================================

SYSTEM_DIRECTORY_NAMES = {
    "$recycle.bin",
    "system volume information",
    "windows",
    "programdata",
    "recovery",
}


def list_folder(path: str) -> str:
    try:
        folder = safe_resolve(path)

        if not folder.exists():
            return f"Folder not found: {path}"

        if not folder.is_dir():
            return f"Not a folder: {path}"

        entries = sorted(
            folder.iterdir(),
            key=lambda item: (
                not item.is_dir(),
                item.name.casefold(),
            ),
        )

        lines = [f"CONTENTS OF {folder}"]

        for item in entries[:MAX_FILE_RESULTS]:
            kind = "[DIR]" if item.is_dir() else "[FILE]"
            lines.append(f"{kind} {item.name}")

        if not entries:
            lines.append("(empty)")

        if len(entries) > MAX_FILE_RESULTS:
            lines.append(
                f"... and {len(entries) - MAX_FILE_RESULTS} more."
            )

        return "\n".join(lines)

    except (PermissionError, OSError) as exc:
        return f"Could not read folder: {exc}"


def get_file_info(path: str) -> str:
    try:
        item = safe_resolve(path)

        if not item.exists():
            return f"Item not found: {path}"

        stat = item.stat()
        kind = "Folder" if item.is_dir() else "File"

        return (
            f"{kind}: {item}\n"
            f"Size: {stat.st_size / (1024 ** 2):.2f} MB\n"
            f"Modified: {datetime.fromtimestamp(stat.st_mtime)}"
        )

    except (PermissionError, OSError) as exc:
        return f"Could not inspect item: {exc}"


def find_file(
    filename: str,
    root: str = "",
) -> str:
    target = str(filename).strip().casefold()

    if not target:
        return "No filename supplied."

    if root.strip():
        roots = [safe_resolve(root)]
    else:
        roots = [
            Path(partition.mountpoint)
            for partition in psutil.disk_partitions(all=False)
        ]

    results: list[str] = []

    for search_root in roots:
        if not search_root.exists():
            continue

        try:
            for current_root, directories, files in os.walk(
                search_root,
                topdown=True,
            ):
                directories[:] = [
                    directory
                    for directory in directories
                    if (
                        directory.casefold()
                        not in SYSTEM_DIRECTORY_NAMES
                        and not directory.startswith(".")
                    )
                ]

                for filename_found in files:
                    if target in filename_found.casefold():
                        results.append(
                            str(
                                Path(current_root)
                                / filename_found
                            )
                        )

                        if len(results) >= MAX_FILE_RESULTS:
                            return (
                                f"FOUND {len(results)} RESULTS "
                                "(limit reached)\n"
                                + "\n".join(results)
                            )

        except (PermissionError, OSError):
            continue

    if not results:
        return (
            f"No files matching '{filename}' were found."
        )

    return (
        f"FOUND {len(results)} RESULTS\n"
        + "\n".join(results)
    )


def open_file(path: str) -> str:
    try:
        item = safe_resolve(path)

        if not item.exists():
            return f"Item not found: {path}"

        os.startfile(str(item))
        return f"Opened {item}"

    except (AttributeError, OSError) as exc:
        return f"Could not open item: {exc}"


def rename_file(
    path: str,
    new_name: str,
) -> str:
    try:
        item = safe_resolve(path)
        new_name = str(new_name).strip()

        if not item.exists():
            return f"Item not found: {path}"

        if not new_name:
            return "No new name supplied."

        if Path(new_name).name != new_name:
            return "The new name must be a name, not a path."

        destination = item.with_name(new_name)

        if destination.exists():
            return (
                f"An item already exists at {destination}."
            )

        item.rename(destination)
        return f"Renamed to {destination.name}."

    except (PermissionError, OSError) as exc:
        return f"Could not rename item: {exc}"


def move_file(
    path: str,
    destination: str,
) -> str:
    try:
        item = safe_resolve(path)
        destination_folder = safe_resolve(destination)

        if not item.exists():
            return f"Item not found: {path}"

        if not destination_folder.is_dir():
            return (
                f"Destination folder not found: "
                f"{destination}"
            )

        destination_path = destination_folder / item.name

        if destination_path.exists():
            return (
                f"An item already exists at "
                f"{destination_path}."
            )

        shutil.move(
            str(item),
            str(destination_path),
        )

        return f"Moved to {destination_path}."

    except (PermissionError, OSError, shutil.Error) as exc:
        return f"Could not move item: {exc}"


def delete_file(path: str) -> str:
    """Delete a path. The permission layer must confirm this first."""
    try:
        item = safe_resolve(path)

        if not item.exists():
            return f"Item not found: {path}"

        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

        return f"Deleted {item}."

    except (PermissionError, OSError, shutil.Error) as exc:
        return f"Could not delete item: {exc}"


# =============================================================================
# REMINDERS
# =============================================================================

class Scheduler:
    """Small persistent reminder scheduler."""

    def __init__(
        self,
        notify_callback: Callable[[str, str], None],
    ) -> None:
        self.notify_callback = notify_callback
        self.lock = threading.RLock()
        self.running = True
        self.tasks = self._load()

        self.thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="LokiScheduler",
        )
        self.thread.start()

    @staticmethod
    def _load() -> list[dict[str, Any]]:
        try:
            with TASK_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)

            return data if isinstance(data, list) else []

        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save(self) -> None:
        try:
            with TASK_FILE.open("w", encoding="utf-8") as file:
                json.dump(
                    self.tasks,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )

        except OSError as exc:
            print(f"[Loki] Could not save tasks: {exc}")

    def add(
        self,
        message: str,
        seconds: str,
    ) -> str:
        message = str(message).strip()

        if not message:
            return "Reminder message cannot be empty."

        try:
            delay = int(str(seconds).strip())
        except ValueError:
            return "Reminder delay must be a number of seconds."

        if delay < 1:
            return "Reminder delay must be at least 1 second."

        run_at = time.time() + delay
        task_id = int(time.time() * 1000)

        with self.lock:
            self.tasks.append(
                {
                    "id": task_id,
                    "message": message,
                    "run_at": run_at,
                }
            )
            self._save()

        return (
            "Reminder scheduled for "
            f"{datetime.fromtimestamp(run_at).strftime('%H:%M:%S')}."
        )

    def list_tasks(self) -> str:
        with self.lock:
            if not self.tasks:
                return "No scheduled reminders."

            lines = ["SCHEDULED REMINDERS"]

            for task in self.tasks:
                try:
                    timestamp = float(task["run_at"])
                    message = str(task["message"])

                    lines.append(
                        f"{task['id']}: "
                        f"{datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')}"
                        f" - {message}"
                    )

                except (KeyError, TypeError, ValueError):
                    continue

            return "\n".join(lines)

    def cancel(self, task_id: str) -> str:
        with self.lock:
            original = len(self.tasks)

            self.tasks = [
                task
                for task in self.tasks
                if str(task.get("id")) != str(task_id).strip()
            ]

            if len(self.tasks) == original:
                return "Reminder not found."

            self._save()

        return "Reminder cancelled."

    def _loop(self) -> None:
        while self.running:
            due = []

            with self.lock:
                now = time.time()
                remaining = []

                for task in self.tasks:
                    try:
                        if float(task["run_at"]) <= now:
                            due.append(task)
                        else:
                            remaining.append(task)
                    except (KeyError, TypeError, ValueError):
                        continue

                if len(remaining) != len(self.tasks):
                    self.tasks = remaining
                    self._save()

            for task in due:
                self.notify_callback(
                    "Loki Reminder",
                    str(task.get("message", "Reminder")),
                )

            time.sleep(1)

    def stop(self) -> None:
        self.running = False


# =============================================================================
# TOOL REGISTRY
# =============================================================================

TOOLS: dict[str, dict[str, Any]] = {
    # Read-only system information.
    "get_system_info": {
        "function": get_system_info,
        "permission": PERMISSION_AUTO,
        "description": "Read PC specifications and operating system information.",
        "arguments": "none",
    },
    "get_cpu_details": {
        "function": get_cpu_details,
        "permission": PERMISSION_AUTO,
        "description": "Read CPU model, cores and frequency.",
        "arguments": "none",
    },
    "get_resource_usage": {
        "function": get_resource_usage,
        "permission": PERMISSION_AUTO,
        "description": "Read current CPU and RAM usage.",
        "arguments": "none",
    },
    "get_gpu_status": {
        "function": get_gpu_info,
        "permission": PERMISSION_AUTO,
        "description": "Read NVIDIA GPU, VRAM, usage, temperature and driver.",
        "arguments": "none",
    },
    "get_disk_info": {
        "function": get_disk_info,
        "permission": PERMISSION_AUTO,
        "description": "Read storage information from mounted drives.",
        "arguments": "none",
    },
    "get_battery_info": {
        "function": get_battery_info,
        "permission": PERMISSION_AUTO,
        "description": "Read battery level and charging state.",
        "arguments": "none",
    },
    "get_network_info": {
        "function": get_network_info,
        "permission": PERMISSION_AUTO,
        "description": "Read local network information.",
        "arguments": "none",
    },
    "get_network_status": {
        "function": get_network_status,
        "permission": PERMISSION_AUTO,
        "description": "Read network adapter status and link speeds.",
        "arguments": "none",
    },
    "check_internet": {
        "function": check_internet,
        "permission": PERMISSION_AUTO,
        "description": "Check basic internet connectivity.",
        "arguments": "none",
    },
    "get_processes": {
        "function": get_processes,
        "permission": PERMISSION_AUTO,
        "description": "List the top running processes by RAM usage.",
        "arguments": "none",
    },
    "process_running": {
        "function": process_running,
        "permission": PERMISSION_REQUEST,
        "description": "Check whether a named process is running.",
        "arguments": "process name",
    },

    # Applications.
    "open_app": {
        "function": open_app,
        "permission": PERMISSION_REQUEST,
        "description": "Open an application from Loki's approved registry.",
        "arguments": "application name",
    },
    "close_process": {
        "function": close_process,
        "permission": PERMISSION_CONFIRM,
        "description": "Terminate a matching process after confirmation.",
        "arguments": "process name",
    },

    # Audio.
    "get_audio_devices": {
        "function": get_audio_devices,
        "permission": PERMISSION_AUTO,
        "description": "List active Windows audio output devices.",
        "arguments": "none",
    },
    "get_audio_info": {
        "function": get_audio_info,
        "permission": PERMISSION_AUTO,
        "description": "Read current Windows audio output, volume and mute state.",
        "arguments": "none",
    },
    "set_volume": {
        "function": set_volume,
        "permission": PERMISSION_REQUEST,
        "description": "Set Windows master volume from 0 to 100.",
        "arguments": "number 0-100",
    },
    "switch_audio_device": {
        "function": switch_audio_device,
        "permission": PERMISSION_REQUEST,
        "description": "Switch the Windows default audio output.",
        "arguments": "device name",
    },
    "mute_audio": {
        "function": lambda: mute_audio(True),
        "permission": PERMISSION_REQUEST,
        "description": "Mute Windows master audio.",
        "arguments": "none",
    },
    "unmute_audio": {
        "function": lambda: mute_audio(False),
        "permission": PERMISSION_REQUEST,
        "description": "Unmute Windows master audio.",
        "arguments": "none",
    },

    # Files.
    "list_folder": {
        "function": list_folder,
        "permission": PERMISSION_AUTO,
        "description": "List a folder. Read-only.",
        "arguments": "folder path",
    },
    "get_file_info": {
        "function": get_file_info,
        "permission": PERMISSION_AUTO,
        "description": "Read file or folder metadata.",
        "arguments": "path",
    },
    "find_file": {
        "function": find_file,
        "permission": PERMISSION_AUTO,
        "description": "Find files by name.",
        "arguments": "filename ||| optional root path",
    },
    "open_file": {
        "function": open_file,
        "permission": PERMISSION_REQUEST,
        "description": "Open a file or folder in Windows.",
        "arguments": "path",
    },
    "rename_file": {
        "function": rename_file,
        "permission": PERMISSION_CONFIRM,
        "description": "Rename a file or folder.",
        "arguments": "path ||| new name",
    },
    "move_file": {
        "function": move_file,
        "permission": PERMISSION_CONFIRM,
        "description": "Move a file or folder.",
        "arguments": "source path ||| destination folder",
    },
    "delete_file": {
        "function": delete_file,
        "permission": PERMISSION_CONFIRM,
        "description": "Delete a file or folder.",
        "arguments": "path",
    },

    # Power actions.
    "shutdown_pc": {
        "function": lambda: shutdown_pc(),
        "permission": PERMISSION_CONFIRM,
        "description": "Shut down Windows.",
        "arguments": "none",
    },
    "restart_pc": {
        "function": lambda: restart_pc(),
        "permission": PERMISSION_CONFIRM,
        "description": "Restart Windows.",
        "arguments": "none",
    },

    # Reminders.
    "add_reminder": {
        "function": None,
        "permission": PERMISSION_REQUEST,
        "description": "Create an in-app reminder.",
        "arguments": "message ||| seconds",
    },
    "list_reminders": {
        "function": None,
        "permission": PERMISSION_AUTO,
        "description": "List active reminders.",
        "arguments": "none",
    },
    "cancel_reminder": {
        "function": None,
        "permission": PERMISSION_REQUEST,
        "description": "Cancel a reminder by ID.",
        "arguments": "reminder ID",
    },
}


def shutdown_pc() -> str:
    try:
        subprocess.Popen(
            ["shutdown", "/s", "/t", "0"]
        )
        return "Shutting down the PC."
    except OSError as exc:
        return f"Could not shut down the PC: {exc}"


def restart_pc() -> str:
    try:
        subprocess.Popen(
            ["shutdown", "/r", "/t", "0"]
        )
        return "Restarting the PC."
    except OSError as exc:
        return f"Could not restart the PC: {exc}"


# =============================================================================
# TOOL PARSING + SAFETY
# =============================================================================

def parse_action(reply: str) -> tuple[str | None, str]:
    """
    Parse a model tool response even if the model adds accidental text.

    Expected:
        ACTION: tool_name
        ARG: value

    Multiple arguments:
        ARG: value1 ||| value2
    """
    if not isinstance(reply, str):
        return None, ""

    action_match = re.search(
        r"\bACTION\s*:\s*([A-Za-z0-9_]+)",
        reply,
        re.IGNORECASE,
    )

    if not action_match:
        return None, ""

    action = action_match.group(1).strip()

    arg_match = re.search(
        r"\bARG\s*:\s*(.*?)(?:\r?\n|$)",
        reply,
        re.IGNORECASE,
    )

    arg = arg_match.group(1).strip() if arg_match else ""

    return action, arg


def split_args(arg: str) -> list[str]:
    if not arg:
        return []

    return [
        part.strip()
        for part in arg.split("|||")
    ]


# These phrases are an additional Python-side guard for action tools.
# The model cannot create permission by itself.
EXPLICIT_ACTION_PHRASES: dict[str, tuple[str, ...]] = {
    "open_app": (
        "open ",
        "launch ",
        "start ",
    ),
    "close_process": (
        "close ",
        "quit ",
        "stop ",
        "terminate ",
        "kill ",
    ),
    "process_running": (
        "is ",
        "check if ",
        "check whether ",
        "is process",
    ),
    "set_volume": (
        "set volume",
        "volume to",
        "turn volume",
        "change volume",
    ),
    "switch_audio_device": (
        "switch audio",
        "switch to",
        "change audio",
        "use my ",
    ),
    "mute_audio": (
        "mute",
    ),
    "unmute_audio": (
        "unmute",
    ),
    "open_file": (
        "open file",
        "open folder",
    ),
    "rename_file": (
        "rename ",
    ),
    "move_file": (
        "move ",
    ),
    "delete_file": (
        "delete ",
        "remove ",
    ),
    "shutdown_pc": (
        "shut down",
        "shutdown",
        "turn off my pc",
        "turn off the computer",
    ),
    "restart_pc": (
        "restart my pc",
        "restart the computer",
        "reboot my pc",
    ),
    "add_reminder": (
        "remind me",
        "set a reminder",
    ),
    "cancel_reminder": (
        "cancel reminder",
    ),
}


def explicitly_requested(
    user_text: str,
    action: str,
) -> bool:
    text = user_text.casefold().strip()

    phrases = EXPLICIT_ACTION_PHRASES.get(
        action,
        (),
    )

    return any(
        phrase in text
        for phrase in phrases
    )


def safety_check(
    action: str,
    arg: str,
) -> tuple[bool, str]:
    """Validate a proposed tool request before execution."""

    config = TOOLS_CONFIG.get("tools", {}).get(action)
    if action not in TOOLS or not isinstance(config, dict):
        return False, "Tool is not in Loki's approved registry."
    if not config.get("enabled", False):
        return False, "That tool is disabled in Loki's configuration."
    if config.get("permission") not in {
        PERMISSION_AUTO,
        PERMISSION_REQUEST,
        PERMISSION_CONFIRM,
    }:
        return False, "The tool has an invalid permission setting."

    args = split_args(arg)

    argument_required = {
        "open_app",
        "process_running",
        "close_process",
        "set_volume",
        "switch_audio_device",
        "list_folder",
        "get_file_info",
        "find_file",
        "open_file",
        "rename_file",
        "move_file",
        "delete_file",
        "add_reminder",
        "cancel_reminder",
    }

    if action in argument_required and not args:
        return False, "A required argument was not supplied."

    if action == "open_app":
        if not resolve_app_name(args[0]):
            return (
                False,
                "That application is not in Loki's approved app registry.",
            )

    if action == "set_volume":
        try:
            level = int(args[0])
        except ValueError:
            return False, "Volume must be a number from 0 to 100."

        if not 0 <= level <= 100:
            return False, "Volume must be between 0 and 100."

    if action in {
        "rename_file",
        "move_file",
    } and len(args) < 2:
        return False, "Two arguments are required."

    if action == "add_reminder" and len(args) < 2:
        return False, "Reminder message and delay are required."

    return True, ""


def confirmation_required(action: str) -> bool:
    config = TOOLS_CONFIG.get("tools", {}).get(action, {})
    return config.get("permission", PERMISSION_CONFIRM) == PERMISSION_CONFIRM


# =============================================================================
# OLLAMA + MODEL ROUTING
# =============================================================================

def build_prompt(user_text: str) -> str:
    """Compatibility wrapper that routes once and builds the matching prompt."""
    decision = choose_route(user_text)
    return build_model_prompt(user_text, decision.route)


def build_model_prompt(user_text: str, route: str) -> str:
    """Build a model prompt from the route-specific configured system prompt."""
    prompt_config = PROMPTS_CONFIG.get(route, PROMPTS_CONFIG.get(ROUTE_SIMPLE, {}))
    system_prompt = str(prompt_config.get("system", "You are Loki, a helpful local assistant."))
    return (
        f"{system_prompt}\n\n"
        f"{memory.build_context()}\n\n"
        f"CURRENT USER MESSAGE:\n{user_text}\n\n"
        "Respond naturally."
    )


def build_tool_prompt(user_text: str) -> str:
    """Ask the fast model to select at most one approved tool."""

    tool_descriptions = "\n".join(
        (
            f"- {name}: {data['description']} | "
            f"args: {data['arguments']} | "
            f"permission: {TOOLS_CONFIG['tools'][name]['permission']}"
        )
        for name, data in TOOLS.items()
        if TOOLS_CONFIG.get("tools", {}).get(name, {}).get("enabled", False)
    )

    return f"""
You are Loki's tool-selection system.

Your only job is to decide whether ONE approved Python tool is
required for the user's request.

APPROVED TOOLS:
{tool_descriptions}

RULES:
1. Use a tool only for live PC information or an actual requested action.
2. Never invent a tool.
3. Select at most ONE tool.
4. If no tool is required, reply exactly:
NO_TOOL
5. If a tool is required, reply ONLY:
ACTION: tool_name
ARG: value
6. For no argument:
ACTION: tool_name
ARG:
7. For multiple arguments:
ARG: value1 ||| value2
8. Do not put prose before or after the tool response.

USER REQUEST:
{user_text}
""".strip()


def ask_ollama(
    prompt: str,
    model: str,
    system: str | None = None,
    stream: bool = False,
    timeout: int = OLLAMA_TIMEOUT,
    stream_callback: Callable[[str], None] | None = None,
) -> str:
    """Send an Ollama request and optionally parse its streamed response."""
    try:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
        }
        if system:
            payload["system"] = system

        response = requests.post(
            OLLAMA_URL,
            json=payload,
            stream=stream,
            timeout=timeout,
        )

        response.raise_for_status()
        if not stream:
            result = response.json().get("response", "")
            return result.strip() if isinstance(result, str) else ""

        chunks: list[str] = []
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            chunk = item.get("response", "")
            if isinstance(chunk, str) and chunk:
                chunks.append(chunk)
                if stream_callback:
                    stream_callback(chunk)
            if item.get("done"):
                break
        return "".join(chunks).strip()

    except requests.exceptions.ConnectionError:
        return (
            "I couldn't connect to Ollama. "
            "Make sure Ollama is running."
        )

    except requests.exceptions.Timeout:
        return "Ollama took too long to respond."

    except requests.exceptions.HTTPError as exc:
        return f"Ollama returned an HTTP error: {exc}"

    except requests.exceptions.RequestException as exc:
        return f"Ollama request failed: {exc}"

    except (ValueError, json.JSONDecodeError):
        return "Ollama returned an invalid response."


def ask_fast(prompt: str) -> str:
    return ask_ollama(
        prompt,
        FAST_MODEL,
    )


def ask_reasoning(prompt: str) -> str:
    return ask_ollama(
        prompt,
        REASONING_MODEL,
    )


def _score_route(user_text: str, route: str) -> float:
    text = user_text.casefold()
    indicators = ROUTER_CONFIG.get("routes", {}).get(route, {}).get("indicators", {})
    return sum(
        float(weight)
        for phrase, weight in indicators.items()
        if str(phrase).casefold() in text
    )


def _heuristic_route(user_text: str) -> RouteDecision:
    settings = ROUTER_CONFIG.get("settings", {})
    scores = {
        ROUTE_SIMPLE: 0.0,
        ROUTE_COMPLEX: _score_route(user_text, ROUTE_COMPLEX),
        ROUTE_TOOL: _score_route(user_text, ROUTE_TOOL),
    }
    if len(user_text.strip()) >= int(settings.get("long_request_threshold", 300)):
        scores[ROUTE_COMPLEX] += 4.0
    if len(user_text.strip()) <= int(settings.get("very_short_request_threshold", 70)):
        scores[ROUTE_SIMPLE] += 1.5

    if scores[ROUTE_TOOL] >= float(settings.get("strong_tool_score", 10)):
        route = ROUTE_TOOL
    else:
        route = max(scores, key=scores.get)
    total = sum(scores.values())
    confidence = scores[route] / total if total else 0.55
    reason = {
        ROUTE_TOOL: "Strong computer-action or live-PC-information indicators.",
        ROUTE_COMPLEX: "Coding, project, reasoning or multi-step indicators.",
        ROUTE_SIMPLE: "No strong tool or reasoning indicators.",
    }[route]
    return RouteDecision(route, round(confidence, 3), reason, scores, "weighted-rules")


def _extract_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _ai_route(user_text: str) -> RouteDecision | None:
    classifier = PROMPTS_CONFIG.get("classifier", {})
    try:
        raw = ask_ollama(
            f"Classify the following request.\n\nUSER REQUEST:\n{user_text}\n\nReturn ONLY JSON.",
            CLASSIFIER_MODEL,
            system=classifier.get("system"),
            timeout=int(MODELS_CONFIG["timeouts"].get("classifier_seconds", 45)),
        )
        data = _extract_json(raw)
        if data is None:
            return None
        route = str(data.get("route", "")).casefold().strip()
        confidence = float(data.get("confidence", 0))
        if route not in {ROUTE_SIMPLE, ROUTE_COMPLEX, ROUTE_TOOL}:
            return None
        return RouteDecision(
            route=route,
            confidence=max(0.0, min(1.0, confidence)),
            reason=str(data.get("reason", "AI classifier decision.")),
            scores={},
            source="ai-classifier",
        )
    except (TypeError, ValueError, KeyError) as exc:
        ROUTER_LOGGER.warning("Invalid AI classifier response: %s", exc)
        return None
    except Exception as exc:
        ROUTER_LOGGER.warning("AI classifier unavailable: %s", exc)
        return None


def choose_route(user_text: str) -> RouteDecision:
    """Choose simple, reasoning, or tool using editable weights and fallback classification."""
    heuristic = _heuristic_route(user_text)
    settings = ROUTER_CONFIG.get("settings", {})
    strong_tool_score = float(settings.get("strong_tool_score", 10))
    if heuristic.route == ROUTE_TOOL and heuristic.scores[ROUTE_TOOL] >= strong_tool_score:
        return heuristic

    total = sum(heuristic.scores.values())
    ranked_scores = sorted(
        (score for score in heuristic.scores.values() if score > 0),
        reverse=True,
    )
    score_margin = ranked_scores[0] - ranked_scores[1] if len(ranked_scores) > 1 else float("inf")
    use_classifier = bool(settings.get("use_ai_classifier_for_ambiguous_requests", True)) and (
        total == 0
        or heuristic.confidence < float(settings.get("classifier_min_confidence", 0.7))
        or (
            len(ranked_scores) > 1
            and score_margin <= float(settings.get("ambiguous_score_gap", 3))
        )
    )
    if use_classifier:
        classified = _ai_route(user_text)
        if classified is not None:
            return classified
    return heuristic


def model_for_route(route: str) -> str:
    return REASONING_MODEL if route == ROUTE_COMPLEX else FAST_MODEL


# =============================================================================
# TOOL EXECUTION
# =============================================================================

def execute_tool(
    action: str,
    arg: str,
    user_text: str,
) -> str:
    """Central safety gate and tool executor."""

    if action not in TOOLS:
        return "I don't have access to that tool."

    tool = TOOLS[action]
    permission = TOOLS_CONFIG.get("tools", {}).get(action, {}).get("permission")
    if not TOOLS_CONFIG.get("tools", {}).get(action, {}).get("enabled", False):
        return "That tool is disabled in Loki's configuration."

    # Action tools require an explicit user request.
    if TOOLS_CONFIG.get("settings", {}).get("require_explicit_user_request_for_actions", True) and permission in (
        PERMISSION_REQUEST,
        PERMISSION_CONFIRM,
    ):
        if not explicitly_requested(
            user_text,
            action,
        ):
            return (
                "I didn't execute that action because "
                "you did not explicitly request it."
            )

    # Destructive/system-changing tools require confirmation.
    if (
        TOOLS_CONFIG.get("settings", {}).get("require_confirmation_for_destructive_actions", True)
        and confirmation_required(action)
    ):
        confirmation_message = (
            f"Loki wants to perform:\n\n"
            f"{action}"
        )

        if arg:
            confirmation_message += (
                f"\n\nArguments:\n{arg}"
            )

        confirmation_message += "\n\nContinue?"

        if not ask_confirmation(
            "Loki Permission Required",
            confirmation_message,
        ):
            return "Action cancelled."

    args = split_args(arg)

    try:
        if action == "open_app":
            return open_app(args[0])

        if action == "process_running":
            return process_running(args[0])

        if action == "close_process":
            return close_process(args[0])

        if action == "set_volume":
            return set_volume(args[0])

        if action == "switch_audio_device":
            return switch_audio_device(args[0])

        if action == "open_file":
            return open_file(args[0])

        if action == "list_folder":
            return list_folder(args[0])

        if action == "get_file_info":
            return get_file_info(args[0])

        if action == "find_file":
            return find_file(
                args[0],
                args[1] if len(args) > 1 else "",
            )

        if action == "rename_file":
            return rename_file(
                args[0],
                args[1],
            )

        if action == "move_file":
            return move_file(
                args[0],
                args[1],
            )

        if action == "delete_file":
            return delete_file(args[0])

        if action == "add_reminder":
            return scheduler.add(
                args[0],
                args[1],
            )

        if action == "list_reminders":
            return scheduler.list_tasks()

        if action == "cancel_reminder":
            return scheduler.cancel(args[0])

        function = tool["function"]

        if function is None:
            return "That tool is not configured correctly."

        result = function()
        return truncate(result)

    except IndexError:
        return "The tool did not receive all required arguments."

    except ValueError:
        return "One of the tool arguments has an invalid value."

    except Exception as exc:
        return f"Tool execution failed: {exc}"


def process_tool_request(user_text: str) -> str:
    """
    Ask the fast model to choose one tool, then validate and execute it.

    If the model does not produce a valid action, no Python action is run.
    """
    tool_reply = ask_ollama(
        build_tool_prompt(user_text),
        FAST_MODEL,
        timeout=TOOL_MODEL_TIMEOUT,
    )

    action, arg = parse_action(tool_reply)

    if not action:
        # If the model returned NO_TOOL, fall back to a normal response.
        if tool_reply.strip().upper() == "NO_TOOL":
            return ask_ollama(
                build_model_prompt(user_text, ROUTE_SIMPLE),
                FAST_MODEL,
            )

        return tool_reply

    allowed, reason = safety_check(
        action,
        arg,
    )

    if not allowed:
        return f"I couldn't perform that action: {reason}"

    return execute_tool(
        action,
        arg,
        user_text,
    )


def process_request(user_text: str) -> tuple[str, RouteDecision, str]:
    """Route once, then run the selected tool or model."""
    decision = choose_route(user_text)
    if decision.route == ROUTE_TOOL:
        model = "tool router"
        reply = process_tool_request(user_text)
    else:
        model = model_for_route(decision.route)
        prompt = build_model_prompt(user_text, decision.route)
        reply = ask_ollama(prompt, model=model, stream=False)
    print(
        f"[LOKI ROUTER] Route={decision.route} | "
        f"Confidence={decision.confidence:.2f} | "
        f"Source={decision.source} | Reason={decision.reason} | "
        f"Scores={decision.scores}"
    )
    return reply, decision, model


# =============================================================================
# UI STATE
# =============================================================================

app = ctk.CTk()

app.overrideredirect(True)
app.geometry(
    f"{WINDOW_SMALL[0]}x{WINDOW_SMALL[1]}"
)
app.configure(fg_color=COLOR_WINDOW)

is_chat_open = False
is_thinking = False
is_closing = False

drag_x = 0
drag_y = 0

current_request_id = 0
request_lock = threading.Lock()
thinking_message_start: str | None = None


# =============================================================================
# UI HELPERS
# =============================================================================

def ui_call(callback: Callable[[], None]) -> None:
    """Schedule a callback safely on Tk's main thread."""
    try:
        if app.winfo_exists():
            app.after(0, callback)
    except Exception:
        pass


def update_chat(text: str) -> None:
    """Append a compact, visually distinct chat entry."""
    tag = "assistant"
    if text.startswith("You:"):
        tag = "user"
    elif text.startswith("Loki: Thinking"):
        tag = "thinking"
    elif "error" in text.casefold() or "couldn't" in text.casefold():
        tag = "error"

    chatbox.configure(state="normal")
    chatbox._textbox.insert("end", str(text) + "\n\n", tag)
    chatbox.configure(state="disabled")
    chatbox.see("end")


def remove_thinking_message() -> None:
    """Remove the transient activity entry before the reply is shown."""
    global thinking_message_start

    if thinking_message_start is None:
        return

    chatbox.configure(state="normal")
    chatbox._textbox.delete(thinking_message_start, "end")
    chatbox.configure(state="disabled")
    thinking_message_start = None


def set_input_enabled(enabled: bool) -> None:
    state = "normal" if enabled else "disabled"

    entry.configure(state=state)
    send_button.configure(state=state)


def set_status(text: str) -> None:
    status_label.configure(text=text)
    thinking = "THINKING" in text or "PROCESSING" in text
    status_chip.configure(
        text="●  WORKING" if thinking else "●  READY",
        text_color=COLOR_ACCENT if thinking else COLOR_SUCCESS,
    )


def ask_confirmation(
    title: str,
    message: str,
) -> bool:
    """
    Tk messageboxes must run on the Tk main thread.

    A worker thread waits on an Event until the UI completes the dialog.
    """
    result = {"approved": False}
    finished = threading.Event()

    def show_dialog() -> None:
        try:
            result["approved"] = messagebox.askyesno(
                title,
                message,
                parent=app,
            )
        finally:
            finished.set()

    ui_call(show_dialog)
    finished.wait()

    return bool(result["approved"])


def show_notification(
    title_text: str,
    message: str,
) -> None:
    """Display a small in-app reminder notification."""
    def show() -> None:
        if is_closing:
            return

        toast = ctk.CTkToplevel(app)
        toast.title(title_text)
        toast.geometry("330x125")
        toast.resizable(False, False)
        toast.attributes("-topmost", True)

        ctk.CTkLabel(
            toast,
            text=title_text,
            font=("Segoe UI", 14, "bold"),
        ).pack(
            anchor="w",
            padx=15,
            pady=(15, 5),
        )

        ctk.CTkLabel(
            toast,
            text=message,
            justify="left",
            wraplength=295,
        ).pack(
            anchor="w",
            padx=15,
        )

        toast.after(
            7000,
            lambda: (
                toast.destroy()
                if toast.winfo_exists()
                else None
            ),
        )

    ui_call(show)


# Scheduler is created after the UI callback functions exist.
scheduler = Scheduler(show_notification)


# =============================================================================
# WINDOW DRAGGING / ANIMATION
# =============================================================================

def start_move(event: Any) -> None:
    global drag_x, drag_y

    drag_x = event.x
    drag_y = event.y


def do_move(event: Any) -> None:
    x = event.x_root - drag_x
    y = event.y_root - drag_y

    app.geometry(f"+{x}+{y}")


def animate_resize(
    start_size: tuple[int, int],
    end_size: tuple[int, int],
    steps: int = 14,
) -> None:
    x = app.winfo_x()
    y = app.winfo_y()

    def step(index: int = 0) -> None:
        if index > steps:
            return

        progress = index / steps

        width = int(
            start_size[0]
            + (end_size[0] - start_size[0])
            * progress
        )

        height = int(
            start_size[1]
            + (end_size[1] - start_size[1])
            * progress
        )

        try:
            app.geometry(
                f"{width}x{height}+{x}+{y}"
            )
        except Exception:
            return

        app.after(
            10,
            lambda: step(index + 1),
        )

    step()


# =============================================================================
# UI BUILD
# =============================================================================

bubble = ctk.CTkButton(
    app,
    text="L",
    font=("Segoe UI Semibold", 24),
    fg_color=COLOR_ACCENT,
    hover_color=COLOR_ACCENT_HOVER,
    text_color=COLOR_WINDOW,
    corner_radius=35,
    command=lambda: open_chat(),
)

bubble.pack(
    fill="both",
    expand=True,
)

chat_frame = ctk.CTkFrame(
    app,
    fg_color=COLOR_WINDOW,
    corner_radius=0,
)

header = ctk.CTkFrame(
    chat_frame,
    height=64,
    fg_color=COLOR_PANEL,
    corner_radius=0,
)

header.pack(
    fill="x",
    side="top",
)

header.pack_propagate(False)

brand_mark = ctk.CTkLabel(
    header,
    text="L",
    width=30,
    height=30,
    corner_radius=15,
    fg_color=COLOR_ACCENT,
    text_color=COLOR_WINDOW,
    font=("Segoe UI Semibold", 16),
)

brand_mark.pack(
    side="left",
    padx=(16, 9),
    pady=16,
)

title_stack = ctk.CTkFrame(header, fg_color="transparent")
title_stack.pack(
    side="left",
    pady=10,
)

title_label = ctk.CTkLabel(
    title_stack,
    text="LOKI",
    text_color=COLOR_TEXT,
    font=("Segoe UI Semibold", 15),
)
title_label.pack(anchor="w")

subtitle_label = ctk.CTkLabel(
    title_stack,
    text="LOCAL DESKTOP ASSISTANT",
    text_color=COLOR_MUTED,
    font=("Segoe UI", 9),
)
subtitle_label.pack(anchor="w", pady=(1, 0))

status_chip = ctk.CTkLabel(
    header,
    text="●  READY",
    text_color=COLOR_SUCCESS,
    fg_color=COLOR_SURFACE,
    corner_radius=10,
    font=("Segoe UI Semibold", 9),
    padx=9,
    pady=4,
)
status_chip.pack(side="right", padx=(0, 8))

status_label = ctk.CTkLabel(
    header,
    text="SIMPLE · llama3",
    text_color=COLOR_MUTED,
    font=("Segoe UI", 9),
)
status_label.pack(side="bottom", anchor="e", padx=14, pady=(0, 7))

close_button = ctk.CTkButton(
    header,
    text="✕",
    width=28,
    height=28,
    corner_radius=14,
    fg_color="transparent",
    hover_color=COLOR_SURFACE_HOVER,
    text_color=COLOR_MUTED,
    command=lambda: close_chat(),
)

close_button.pack(
    side="right",
    padx=(0, 12),
)

chatbox = ctk.CTkTextbox(
    chat_frame,
    wrap="word",
    fg_color=COLOR_PANEL,
    border_width=1,
    border_color=COLOR_BORDER,
    text_color=COLOR_TEXT,
    font=("Segoe UI", 13),
    corner_radius=14,
)

chatbox.pack(
    fill="both",
    expand=True,
    padx=12,
    pady=(12, 10),
)

chatbox.configure(
    state="disabled"
)
chatbox._textbox.configure(
    bg=COLOR_PANEL,
    insertbackground=COLOR_ACCENT,
    padx=14,
    pady=14,
    spacing1=2,
    spacing3=12,
)
chatbox._textbox.tag_configure(
    "assistant",
    foreground=COLOR_TEXT,
    lmargin1=0,
    lmargin2=0,
    rmargin=18,
)
chatbox._textbox.tag_configure(
    "user",
    foreground="#B9D9FF",
    lmargin1=42,
    lmargin2=42,
    rmargin=0,
    justify="right",
)
chatbox._textbox.tag_configure(
    "thinking",
    foreground=COLOR_MUTED,
    font=("Segoe UI", 11, "italic"),
)
chatbox._textbox.tag_configure(
    "error",
    foreground="#FFB4A8",
)

input_frame = ctk.CTkFrame(
    chat_frame,
    fg_color=COLOR_PANEL,
    corner_radius=14,
    border_width=1,
    border_color=COLOR_BORDER,
)

input_frame.pack(
    fill="x",
    padx=12,
    pady=(0, 10),
)

entry = ctk.CTkEntry(
    input_frame,
    placeholder_text="Ask Loki anything…",
    height=44,
    fg_color="transparent",
    border_width=0,
    text_color=COLOR_TEXT,
    placeholder_text_color=COLOR_MUTED,
    font=("Segoe UI", 13),
)

entry.pack(
    side="left",
    fill="x",
    expand=True,
    padx=(13, 4),
    pady=2,
)

send_button = ctk.CTkButton(
    input_frame,
    text="↑",
    width=36,
    height=36,
    corner_radius=18,
    fg_color=COLOR_ACCENT,
    hover_color=COLOR_ACCENT_HOVER,
    text_color=COLOR_WINDOW,
    font=("Segoe UI Semibold", 17),
    command=lambda: send_message(),
)

send_button.pack(side="right", padx=5, pady=4)

footer = ctk.CTkLabel(
    chat_frame,
    text=(
        f"FAST  {FAST_MODEL}    /    REASONING  {REASONING_MODEL}"
    ),
    text_color=COLOR_MUTED,
    font=("Segoe UI Semibold", 8),
)

footer.pack(
    side="bottom",
    pady=(0, 9),
)


# =============================================================================
# CHAT PROCESSING
# =============================================================================

def finish_request(
    request_id: int,
    user_text: str,
    reply: str,
    decision: RouteDecision,
    model: str,
) -> None:
    """
    Return results to Tk's main thread and ignore stale results.
    """
    global is_thinking

    def update() -> None:
        global is_thinking

        with request_lock:
            if request_id != current_request_id:
                return

        remove_thinking_message()
        update_chat(f"Loki: {reply}")
        set_status(f"{decision.route.upper()} · {model}")
        set_input_enabled(True)
        entry.focus_set()
        is_thinking = False

    ui_call(update)


def get_response(
    user_text: str,
    request_id: int,
) -> None:
    """
    Worker-thread request handler.

    COM is initialized here because Pycaw can be called from this thread.
    """
    com_initialized = False

    try:
        comtypes.CoInitialize()
        com_initialized = True

        reply, decision, model = process_request(
            user_text
        )

        if not reply:
            reply = (
                "I received an empty response from "
                "the selected model."
            )

        memory.add_message(
            "User",
            user_text,
        )

        memory.add_message(
            "AI",
            reply,
        )

        finish_request(
            request_id,
            user_text,
            truncate(reply),
            decision,
            model,
        )

    except Exception as exc:
        print(f"[Loki] Worker error: {exc}")

        finish_request(
            request_id,
            user_text,
            f"I encountered an error: {exc}",
            RouteDecision("error", 0.0, "Request failed.", {}, "runtime"),
            "unavailable",
        )

    finally:
        if com_initialized:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


def send_message() -> None:
    global is_thinking, current_request_id, thinking_message_start

    if is_thinking:
        return

    text = entry.get().strip()

    if not text:
        return

    entry.delete(
        0,
        "end",
    )

    update_chat(
        f"You: {text}"
    )

    chatbox.configure(state="normal")
    thinking_message_start = chatbox._textbox.index("end-1c")
    chatbox._textbox.insert(
        "end",
        "Loki is working…\n\n",
        "thinking",
    )
    chatbox.configure(state="disabled")
    chatbox.see("end")

    is_thinking = True

    set_input_enabled(False)
    set_status("PROCESSING · LOCAL")

    with request_lock:
        current_request_id += 1
        request_id = current_request_id

    threading.Thread(
        target=get_response,
        args=(text, request_id),
        daemon=True,
        name="LokiRequest",
    ).start()


def on_enter(event: Any) -> str:
    send_message()
    return "break"


entry.bind(
    "<Return>",
    on_enter,
)


# =============================================================================
# WINDOW OPEN / CLOSE
# =============================================================================

def open_chat() -> None:
    global is_chat_open

    if is_chat_open or is_closing:
        return

    is_chat_open = True

    bubble.pack_forget()

    chat_frame.pack(
        fill="both",
        expand=True,
    )

    animate_resize(
        WINDOW_SMALL,
        WINDOW_LARGE,
    )

    app.after(
        120,
        entry.focus_set,
    )


def close_chat() -> None:
    global is_chat_open

    if not is_chat_open:
        return

    is_chat_open = False

    chat_frame.pack_forget()

    bubble.pack(
        fill="both",
        expand=True,
    )

    animate_resize(
        WINDOW_LARGE,
        WINDOW_SMALL,
    )


bubble.bind(
    "<ButtonPress-1>",
    start_move,
)

bubble.bind(
    "<B1-Motion>",
    do_move,
)

header.bind(
    "<ButtonPress-1>",
    start_move,
)

header.bind(
    "<B1-Motion>",
    do_move,
)


# =============================================================================
# APPLICATION SHUTDOWN
# =============================================================================

def on_close() -> None:
    global is_closing

    if is_closing:
        return

    is_closing = True

    try:
        scheduler.stop()
    except Exception:
        pass

    try:
        app.destroy()
    except Exception:
        pass


app.protocol(
    "WM_DELETE_WINDOW",
    on_close,
)


# =============================================================================
# STARTUP
# =============================================================================

update_chat(
    "Loki: Online.\n"
    "Local AI systems ready.\n"
    f"Fast model: {FAST_MODEL}\n"
    f"Reasoning model: {REASONING_MODEL}\n"
    "Tools are permission-controlled."
)

app.mainloop()
