import ctypes
import os
import platform
import shutil
import subprocess
from pathlib import Path


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("load", ctypes.c_ulong),
        ("total_physical", ctypes.c_ulonglong),
        ("available_physical", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("available_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("available_virtual", ctypes.c_ulonglong),
        ("available_extended_virtual", ctypes.c_ulonglong),
    ]


def hardware_lines(directory: Path) -> list[str]:
    processor = platform.processor()
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            ) as key:
                processor = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    details = [
        f"Процессор: {processor or platform.machine()}",
        f"Логические потоки: {os.cpu_count() or 1}",
    ]
    if os.name == "nt":
        state = _MemoryStatus()
        state.length = ctypes.sizeof(state)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            details.append(f"ОЗУ: {state.total_physical / 2**30:.1f} ГиБ")
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            data = subprocess.run(
                [nvidia, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=3,
                check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            details.extend(f"NVIDIA: {line.strip()}" for line in data.splitlines() if line.strip())
        except (OSError, subprocess.SubprocessError):
            details.append("Не удалось получить сведения от драйвера NVIDIA.")
    else:
        details.append("Утилита NVIDIA не обнаружена. Доступен режим CPU.")
    try:
        details.append(
            f"Свободно на диске результатов: {shutil.disk_usage(directory).free / 2**30:.1f} ГиБ"
        )
    except OSError:
        details.append("Папка результатов недоступна для оценки свободного места.")
    return details
