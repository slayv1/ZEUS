"""Модуль автономного поиска файлов для Зевса.

Реализует функционал поиска .exe файлов по дискам с:
- Многопоточностью для неблокирующего поиска
- Исключением системных папок
- Кэшированием результатов для ускорения повторных поисков
- Постоянным индексом index.json для мгновенного поиска
- Фоновой индексацией
- Реактивным сканированием (Этап 12): локальное сканирование только что
  подключённых носителей с пометкой source="external" и очисткой при
  отключении устройства.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import config
import psutil


# === Пути к файлам ===

def _cache_path() -> str:
    """Возвращает путь к file_cache.json рядом с исполняемым файлом."""
    return config.data_path("file_cache.json")


def _index_path() -> str:
    """Возвращает путь к index.json рядом с исполняемым файлом."""
    return config.data_path("index.json")


# === Исключения: системные папки ===

# Папки, которые нельзя сканировать (системные, защищённые)
_EXCLUDED_DIRS = {
    "Windows",
    "System32",
    "SysWOW64",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "$Recycle.Bin",
    "System Volume Information",
    "Recovery",
    "Config.Msi",
    "MSOCache",
    "Installer",
    "Microsoft",
    "WindowsApps",
    "C:\\$Windows.~BT",
    "C:\\$Windows.~WS",
    "D:\\$Windows.~BT",
    "D:\\$Windows.~WS",
}

# Расширения для поиска
_TARGET_EXTENSION = ".exe"


# === Индексация ===

def load_index() -> dict[str, dict]:
    """Загружает индекс из index.json.

    Новый формат (Этап 12):
        {
            "имя_программы.exe": {
                "path": "полный_путь",
                "source": "internal" | "external"
            },
            ...
        }

    Поддерживается обратная совместимость со старыми форматами:
        - {"files": {имя: путь}}
        - {имя: путь}            (str)
        - {имя: [путь, ...]}    (list)

    Returns:
        dict: {имя: {"path": str, "source": str}, ...}
    """
    path = _index_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Старый формат с обёрткой "files"
            if isinstance(data, dict) and "files" in data:
                data = data["files"]

            if not isinstance(data, dict):
                return {}

            converted: dict[str, dict] = {}
            for name, value in data.items():
                if isinstance(value, dict):
                    # Сохраняем все ключи из словаря (path, source, drive и т.д.)
                    record = dict(value)
                    record.setdefault("source", "internal")
                    converted[name] = record
                elif isinstance(value, str):
                    converted[name] = {"path": value, "source": "internal"}
                elif isinstance(value, list) and value:
                    converted[name] = {"path": value[0], "source": "internal"}
            return converted
    except Exception:
        pass
    return {}


def save_index(index: dict[str, dict]) -> None:
    """Сохраняет индекс в index.json (новый формат с тегом source)."""
    try:
        path = _index_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def build_index(search_paths: list[str], max_workers: int = 4) -> dict[str, str]:
    """Строит индекс файлов путём сканирования дисков в многопоточном режиме.

    Args:
        search_paths: список корневых папок для сканирования
        max_workers: количество потоков

    Returns:
        dict в формате {имя_файла: путь, ...}
        Если найдено несколько вариантов - сохраняется первый найденный.
    """
    index: dict[str, str] = {}
    lock = threading.Lock()

    def scan_directory(root_path: str) -> dict[str, str]:
        """Сканирует одну директорию и возвращает найденные .exe файлы."""
        local_index: dict[str, str] = {}
        try:
            for root, dirs, files in os.walk(root_path):
                # Пропускаем системные папки
                dirs[:] = [d for d in dirs if d not in _EXCLUDED_DIRS and not d.startswith("$")]

                for file in files:
                    if file.lower().endswith(_TARGET_EXTENSION):
                        name_lower = file.lower()
                        # Сохраняем только первый найденный путь для каждого имени
                        if name_lower not in local_index:
                            local_index[name_lower] = os.path.join(root, file)
        except (PermissionError, OSError):
            pass
        return local_index

    # Сканируем в пуле потоков
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scan_directory, path): path for path in search_paths}

        for future in as_completed(futures):
            try:
                result = future.result()
                with lock:
                    for name, path in result.items():
                        # Сохраняем первый найденный путь
                        if name not in index:
                            index[name] = path
            except Exception:
                pass

    return index


def scan_drive(drive: str, max_workers: int = 4) -> dict[str, str]:
    """Сканирует ТОЛЬКО один диск/папку (локальное сканирование, Этап 12).

    Используется для реактивного сканирования только что подключённого
    съёмного носителя, не затрагивая основные диски C:/D:.

    Args:
        drive: путь к диску, например "E:\\"
        max_workers: количество потоков

    Returns:
        dict в формате {имя_файла: путь, ...}
    """
    drive = os.path.normpath(drive)
    if not os.path.exists(drive):
        return {}
    return build_index([drive], max_workers)


def merge_external_entries(drive: str, entries: dict[str, str]) -> int:
    """Добавляет найденные на внешнем носителе программы в общий index.json.

    Каждая запись помечается тегом source="external", чтобы при отключении
    устройства Зевс понимал, что файл больше недоступен.

    Args:
        drive: буква диска (например "E:\\")
        entries: {имя_файла: путь} с этого диска

    Returns:
        количество добавленных/обновлённых записей
    """
    if not entries:
        return 0

    index = load_index()
    drive_norm = os.path.normpath(drive).upper()

    added = 0
    for name, path in entries.items():
        name_lower = name.lower()
        existing = index.get(name_lower)
        # Не перезаписываем internal-записи внешними (приоритет основной системы)
        if existing and existing.get("source") == "internal":
            continue
        index[name_lower] = {"path": path, "source": "external", "drive": drive_norm}
        added += 1

    save_index(index)
    return added


def remove_external_for_drive(drive: str) -> int:
    """Удаляет из index.json все external-записи, относящиеся к отключённому диску.

    Это гарантирует, что Зевс не попытается запустить программу с флешки,
    которая уже вынута.

    Args:
        drive: буква диска (например "E:\\")

    Returns:
        количество удалённых записей
    """
    index = load_index()
    drive_norm = os.path.normpath(drive).upper()

    to_remove = [
        name
        for name, meta in index.items()
        if meta.get("source") == "external"
        and meta.get("drive", "").upper() == drive_norm
    ]
    for name in to_remove:
        del index[name]

    if to_remove:
        save_index(index)
    return len(to_remove)


def list_available_drives() -> list[str]:
    """Возвращает список подключённых дисков (букв) через psutil.

    Returns:
        список строк вида "C:\\", "D:\\", "E:\\" ...
    """
    drives: list[str] = []
    try:
        for part in psutil.disk_partitions(all=False):
            device = part.device
            if device and device.endswith(":\\"):
                drives.append(device)
    except Exception:
        pass
    return drives


# === Поиск файлов ===

class FileScanner:
    """Сканер файлов с поддержкой многопоточности, кэширования и индексации."""

    def __init__(self, search_paths: list[str] | None = None):
        """Инициализация сканера.

        Args:
            search_paths: список дисков/папок для сканирования (по умолчанию C:\\ и D:\\)"""
        if search_paths is None:
            # Определяем доступные диски
            search_paths = []
            for drive in ["C:\\", "D:\\"]:
                if os.path.exists(drive):
                    search_paths.append(drive)

        # Сохраняем пути для сканирования
        self.search_paths = search_paths

        # Загружаем индекс (новый формат: dict[str, dict])
        self._index = load_index()
        self._indexing = False
        self._index_lock = threading.Lock()

    def find_file(self, name: str) -> list[str]:
        """Ищет .exe файл по имени (частичное совпадение) в индексе.

        Args:
            name: имя файла (например, "chrome", "telegram")

        Returns:
            список найденных путей (может быть пустым)
        """
        # Нормализуем имя
        name_lower = name.lower().strip()
        if not name_lower.endswith(".exe"):
            name_lower += ".exe"

        # Точное совпадение в индексе
        cached = self._index.get(name_lower)
        if cached:
            return [cached["path"]]

        # Частичный поиск по индексу
        results = []
        for indexed_name, meta in self._index.items():
            if name_lower.replace(".exe", "") in indexed_name.replace(".exe", ""):
                results.append(meta["path"])
        return results

    def rebuild_index(self, max_workers: int = 4) -> dict[str, Any]:
        """Перестраивает индекс файлов в фоновом режиме.

        Сохраняет существующие external-записи (с флешек), чтобы они не
        потерялись при полной переиндексации основных дисков.

        Returns:
            результат операции {"success": bool, "message": str, "files_count": int}
        """
        with self._index_lock:
            if self._indexing:
                return {
                    "success": False,
                    "message": "Индексация уже выполняется, сэр.",
                    "files_count": 0,
                }

            self._indexing = True

        def _rebuild():
            try:
                # Сохраняем external-записи (с флешек), чтобы не потерять
                preserved_external: dict[str, dict] = {
                    name: meta
                    for name, meta in self._index.items()
                    if meta.get("source") == "external"
                }

                flat = build_index(self.search_paths, max_workers)
                new_index: dict[str, dict] = {}
                for name, path in flat.items():
                    new_index[name] = {"path": path, "source": "internal"}

                # Возвращаем external-записи обратно
                for name, meta in preserved_external.items():
                    # Не перезаписываем, если на основном диске нашлась своя версия
                    if name not in new_index:
                        new_index[name] = meta

                self._index = new_index
                save_index(new_index)
            finally:
                with self._index_lock:
                    self._indexing = False

        thread = threading.Thread(target=_rebuild, daemon=True)
        thread.start()

        return {
            "success": True,
            "message": "Запустил индексацию дисков, сэр. Это займёт 1-2 минуты...",
            "files_count": len(self._index) if isinstance(self._index, dict) else 0,
        }

    def is_indexing(self) -> bool:
        """Проверяет, идёт ли индексация."""
        return self._indexing

    def get_index_stats(self) -> dict[str, Any]:
        """Возвращает статистику индекса."""
        files = self._index
        total = len(files) if isinstance(files, dict) else 0
        external = sum(1 for m in files.values() if m.get("source") == "external") if isinstance(files, dict) else 0

        return {
            "total_files": total,
            "total_paths": total,
            "external_files": external,
            "index_age_hours": 0,
            "is_fresh": True,
        }


# === Глобальный экземпляр сканера ===

_scanner_instance: FileScanner | None = None


def get_scanner() -> FileScanner:
    """Возвращает глобальный экземпляр сканера (синглтон)."""
    global _scanner_instance
    if _scanner_instance is None:
        _scanner_instance = FileScanner()
    return _scanner_instance


# === Утилиты для интеграции с actions.py ===

def search_program(name: str) -> dict[str, Any]:
    """Поиск программы по имени в индексе.

    Args:
        name: имя программы (например, "chrome", "telegram")

    Returns:
        dict с результатом:
        {
            "success": bool,
            "paths": list[str],  # найденные пути
            "message": str,
            "action": "file_search"
        }
    """
    scanner = get_scanner()

    # Если индекс пустой — предлагаем проиндексировать
    if not scanner._index:
        return {
            "success": False,
            "paths": [],
            "message": "Индекс пуст. Сканирую систему... Это займёт 1-2 минуты.",
            "action": "file_search",
            "needs_indexing": True,
        }

    # Ищем файл в индексе
    paths = scanner.find_file(name)

    if paths:
        return {
            "success": True,
            "paths": paths,
            "message": f"Нашёл {len(paths)} вариант(ов) для '{name}':\n" + "\n".join(paths),
            "action": "file_search",
        }
    else:
        return {
            "success": False,
            "paths": [],
            "message": f"Не нашёл программу '{name}', сэр. Попробуйте уточнить название или обновите базу.",
            "action": "file_search",
        }


def confirm_and_save(name: str, path: str) -> dict[str, Any]:
    """Подтверждает найденный путь и сохраняет в commands.json.

    Args:
        name: имя программы
        path: путь к .exe файлу

    Returns:
        dict с результатом сохранения
    """
    # Импортируем здесь чтобы избежать циклического импорта
    from core.actions import save_app_command

    # Нормализуем имя
    name = name.strip().lower()

    # Сохраняем
    result = save_app_command(name, path)
    result["action"] = "file_search_confirm"

    return result


def rebuild_system_index(max_workers: int = 4) -> dict[str, Any]:
    """Принудительно перестраивает индекс всей системы.

    Args:
        max_workers: количество потоков для сканирования

    Returns:
        результат операции
    """
    scanner = get_scanner()
    return scanner.rebuild_index(max_workers)


def get_indexed_program(name: str) -> str | None:
    """Получает путь к программе из индекса по имени.

    Args:
        name: имя программы (например, "chrome", "telegram")

    Returns:
        путь к .exe файлу или None, если не найдено
    """
    scanner = get_scanner()
    paths = scanner.find_file(name)
    return paths[0] if paths else None