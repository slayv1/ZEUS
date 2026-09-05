"""Сканирование + живой мониторинг компьютера с автообновлением индекса.

Объединяет два механизма Зевса:
  1. file_scanner — полный фоновый скан дисков (C:\\, D:\\ и др.) и построение index.json.
  2. watchdog     — постоянное отслеживание изменений файлов в реальном времени.

Когда watchdog фиксирует появление/удаление/перемещение исполняемого файла
(*.exe), индекс обновляется автоматически, без повторного полного скана.

Ключевые компоненты:
  - IndexEditor       — потокобезопасное чтение/редактирование index.json с
                        отложенной записью (debounce) для снижения нагрузки.
  - IndexUpdateHandler — обработчик событий watchdog (created/deleted/moved).
  - IndexWatcher      — запускает полный скан и live-наблюдение за дисками.
  - get_index_watcher() — глобальный синглтон.

CLI:
    python -m src.core.index_watcher          # скан + живой мониторинг
    python -m src.core.index_watcher --once   # только полный скан без watch
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

# bootstrap: делаем доступным пакет core/ при любом способе запуска
# (python -m src.core.index_watcher, или прямой запуск файла, или из приложения)
_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from core import file_scanner
from core.file_scanner import load_index, save_index, _EXCLUDED_DIRS


# === Константы ===

_TARGET_EXT = ".exe"
_SAVE_DELAY = 1.0          # сек: отложенная запись индекса (debounce)
_INTERNAL = "internal"     # источник записи в индексе


def _is_target(path: str | None) -> bool:
    """Истина, если путь — исполняемый файл *.exe (без учёта регистра)."""
    return bool(path) and str(path).lower().endswith(_TARGET_EXT)


def _in_excluded_dir(path: str | None) -> bool:
    """Истина, если путь лежит внутри системной/исключённой папки."""
    if not path:
        return False
    parts = set(p.upper() for p in str(path).split(os.sep) if p)
    return any(ex.upper() in parts for ex in _EXCLUDED_DIRS)


# === Редактор индекса (потокобезопасный, с отложенной записью) ===

class IndexEditor:
    """Держит актуальную копию index.json и обновляет её по событиям watchdog.

    Запись на диск выполняется не на каждое событие, а по таймеру (debounce),
    чтобы десятки событий в секунду не «застреляли» диск.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._index: dict[str, dict] = {}
        self._dirty = False
        self._timer: threading.Timer | None = None
        self.reload()

    def reload(self) -> None:
        """Перечитывает индекс с диска (например, после полного скана)."""
        with self._lock:
            self._index = load_index()
            self._dirty = False

    def entries_count(self) -> int:
        """Возвращает количество записей в текущем индексе."""
        with self._lock:
            return len(self._index)

    # --- Операции ---

    def upsert(self, path: str, source: str = _INTERNAL) -> bool:
        """Добавляет/обновляет запись по пути (только для целевых *.exe)."""
        if not _is_target(path) or _in_excluded_dir(path):
            return False
        name = os.path.basename(path).lower()
        with self._lock:
            existing = self._index.get(name)
            # Не перезаписываем external-запись (с флешки) внутренней — скан решит
            if existing and existing.get("source") != _INTERNAL:
                return False
            self._index[name] = {"path": path, "source": source}
            self._mark_dirty()
        return True

    def remove(self, path: str) -> bool:
        """Удаляет запись, если её путь совпадает с удалённым (source=internal)."""
        if not _is_target(path):
            return False
        removed = False
        norm = os.path.normcase(os.path.normpath(path))
        with self._lock:
            for name in list(self._index):
                meta = self._index[name]
                if (meta.get("source") == _INTERNAL
                        and os.path.normcase(os.path.normpath(meta["path"])) == norm):
                    del self._index[name]
                    removed = True
            if removed:
                self._mark_dirty()
        return removed

    def move(self, src: str, dest: str | None) -> bool:
        """Обновляет путь записи при перемещении файла."""
        if not dest:
            return False
        if not _is_target(src) and not _is_target(dest):
            return False
        # Перемещение внутри исключённой папки → считаем запись потерянной
        if _in_excluded_dir(dest):
            return self.remove(src)
        changed = self.remove(src)
        if self.upsert(dest):
            changed = True
        return changed

    # --- Отложенная запись ---

    def _mark_dirty(self) -> None:
        """Помечает индекс изменённым и планирует сохранение."""
        self._dirty = True
        if self._timer and self._timer.is_alive():
            return
        self._timer = threading.Timer(_SAVE_DELAY, self.save)
        self._timer.daemon = True
        self._timer.start()

    def save(self) -> bool:
        """Сохраняет индекс на диск, если были изменения. Идемпотентно."""
        with self._lock:
            if not self._dirty:
                return False
            index = dict(self._index)
            self._dirty = False
        try:
            save_index(index)
            return True
        except Exception:
            return False

    def flush(self) -> None:
        """Принудительная запись при завершении работы."""
        self.save()
# === Обработчик событий watchdog ===

class IndexUpdateHandler(FileSystemEventHandler):
    """Реагирует на изменения *.exe и обновляет индекс через IndexEditor."""

    def __init__(self, editor: IndexEditor) -> None:
        super().__init__()
        self.editor = editor
        # Счётчики для статистики
        self.stats = {"created": 0, "deleted": 0, "moved": 0}

    def on_created(self, event) -> None:  # noqa: N802
        if self.editor.upsert(event.src_path):
            self.stats["created"] += 1

    def on_modified(self, event) -> None:  # noqa: N802
        # Модификация не меняет позицию файла в индексе — пропускаем ради шума
        pass

    def on_deleted(self, event) -> None:  # noqa: N802
        if self.editor.remove(event.src_path):
            self.stats["deleted"] += 1

    def on_moved(self, event) -> None:  # noqa: N802
        if self.editor.move(event.src_path, event.dest_path):
            self.stats["moved"] += 1


# === Наблюдатель = скан + живой мониторинг ===

class IndexWatcher:
    """Запускает полный скан дисков и постоянный live-мониторинг индекса.

    Порядок работы:
      1. scan_and_watch() запускает file_scanner.rebuild_index() в фоне
         (полный скан компьютера и пересборка index.json).
      2. Сразу после этого watchdog начинает следить за теми же дисками
         и подхватывает события в реальном времени.
    """

    def __init__(self, search_paths: list[str] | None = None) -> None:
        self.scanner = file_scanner.FileScanner(search_paths=search_paths)
        self.editor = IndexEditor()
        self._handler = IndexUpdateHandler(self.editor)
        self.observer = Observer()
        self._watching = False

    @property
    def search_paths(self) -> list[str]:
        return self.scanner.search_paths

    def scan_and_watch(self, max_workers: int = 4, watch: bool = True) -> dict[str, Any]:
        """Полный скан дисков + (опционально) старт live-мониторинга.

        Returns:
            результат rebuild_index от file_scanner.
        """
        result = self.scanner.rebuild_index(max_workers)
        # Перечитываем индекс: скан мог изменить его с момента загрузки редактора
        self.editor.reload()
        if watch:
            self.start_watching()
        return result

    def start_watching(self) -> bool:
        """Запускает наблюдатель на всех путях сканера (рекурсивно)."""
        if self._watching:
            return True
        if not self.search_paths:
            return False
        started = False
        try:
            for path in self.search_paths:
                if os.path.exists(path):
                    self.observer.schedule(self._handler, path, recursive=True)
            self.observer.start()
            self._watching = True
            started = True
        except Exception:
            started = False
        return started

    def stop(self) -> None:
        """Останавливает наблюдатель и сохраняет накопленные изменения индекса."""
        self.editor.flush()
        if self._watching:
            try:
                self.observer.stop()
                self.observer.join(timeout=2.0)
            except Exception:
                pass
            self._watching = False

    def is_watching(self) -> bool:
        return self._watching

    def get_stats(self) -> dict[str, Any]:
        """Возвращает статистику: размер индекса и пойманные события."""
        stats = dict(self._handler.stats)
        stats["index_entries"] = self.editor.entries_count()
        stats["drives"] = list(self.search_paths)
        return stats


# === Глобальный экземпляр ===

_watcher_instance: IndexWatcher | None = None


def get_index_watcher() -> IndexWatcher:
    """Возвращает глобальный IndexWatcher (синглтон)."""
    global _watcher_instance
    if _watcher_instance is None:
        _watcher_instance = IndexWatcher()
    return _watcher_instance


# === CLI ===

def _main_cli() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Полный скан дисков + живой мониторинг индекса (watchdog)."
    )
    parser.add_argument("--once", action="store_true",
                        help="Только полный скан, без постоянного мониторинга.")
    parser.add_argument("--workers", type=int, default=4,
                        help="Потоков для сканирования (по умолчанию 4).")
    parser.add_argument("--watch-only", action="store_true",
                        help="Только live-мониторинг, без повторного скана.")
    args = parser.parse_args()

    watcher = get_index_watcher()
    print("Диски для сканирования:", ", ".join(watcher.search_paths))

    if args.watch_only:
        watcher.start_watching()
    else:
        res = watcher.scan_and_watch(max_workers=args.workers, watch=not args.once)
        print("Скан:", res.get("message"))

    if args.once:
        # rebuild_index работает в фоне — дожидаемся завершения скана
        print("Сканирование дисков... это может занять 1-2 минуты.")
        while watcher.scanner.is_indexing():
            time.sleep(0.5)
        watcher.editor.reload()
        print("Готово. Записей в индексе:", watcher.editor.entries_count())
        return 0

    print("Живой мониторинг запущен. Для остановки нажмите Ctrl+C.")
    try:
        while watcher.is_watching():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()

    print("Статистика:", watcher.get_stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main_cli())