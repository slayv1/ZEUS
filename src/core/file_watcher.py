"""Модуль фонового мониторинга файловой системы для Зевса.

Реализован на базе библиотеки watchdog и отслеживает в реальном времени
изменения в файлах/папках проекта (создание, модификация, удаление, перемещение).

Возможности:
  - Фоновый Observer от watchdog: не блокирует UI и основную логику.
  - Событийная модель (on_created / on_modified / on_deleted / on_moved)
    через механизм колбэков.
  - Фильтрация событий по паттерну (по умолчанию *.json в data/).
  - Автоматическое добавление отсутствующих путей в список наблюдения.
  - Синглтон get_watcher() — один экземпляр на всё приложение.
  - Интеграция с config.data_dir() для наблюдения за настройками/кэшем.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from core import config


# Тип колбэка: принимает словарь с описанием события.
# Пример: {"event": "created", "path": "...", "src_path": None}
FileEventCallback = Callable[[dict[str, Any]], None]


def _default_patterns() -> list[str]:
    """Возвращает паттерны файлов, отслеживаемых по умолчанию (JSON-конфиги)."""
    return ["*.json"]


def _norm_path(path: str) -> str:
    """Нормализует путь для сравнения с источником события watchdog."""
    return os.path.normcase(os.path.abspath(path))


class ProjectFileHandler(FileSystemEventHandler):
    """Обработчик событий файловой системы для папок проекта Зевса.

    Группирует события watchdog (event_type, is_directory, src_path, dest_path)
    в единый словарь и вызывает зарегистрированный колбэк.

    Атрибуты:
        callback: вызывается для каждого события с описанием в виде dict.
        patterns: список глоб-паттернов (например "*.json"); пустой список — все файлы.
    """

    def __init__(self, callback: FileEventCallback,
                 patterns: list[str] | None = None) -> None:
        super().__init__()
        self.callback = callback
        self.patterns: list[str] = list(patterns) if patterns else _default_patterns()

    def _matches(self, path: str | None) -> bool:
        """Проверяет, соответствует ли путь хотя бы одному паттерну."""
        import fnmatch
        if not path:
            return True
        # Название папки — всегда отслеживаем (события moved по директориям)
        if os.path.isdir(path):
            return True
        filename = os.path.basename(path)
        return any(fnmatch.fnmatch(filename, pat) for pat in self.patterns)

    def _emit(self, event_type: str, src_path: str,
              dest_path: str | None = None) -> None:
        """Формирует описание события и передаёт колбэку."""
        if not self._matches(src_path):
            return
        desc = {
            "event": event_type,          # created | modified | deleted | moved
            "path": src_path,
            "src_path": src_path,
            "dest_path": dest_path,
            "is_directory": os.path.isdir(src_path) or (dest_path and os.path.isdir(dest_path)),
            "timestamp": time.time(),
        }
        try:
            self.callback(desc)
        except Exception:
            pass  # Колбэк не должен ломать мониторинг

    def on_created(self, event) -> None:  # noqa: N802 (сигнатура watchdog)
        self._emit("created", event.src_path)

    def on_modified(self, event) -> None:  # noqa: N802
        self._emit("modified", event.src_path)

    def on_deleted(self, event) -> None:  # noqa: N802
        self._emit("deleted", event.src_path)

    def on_moved(self, event) -> None:  # noqa: N802
        self._emit("moved", event.src_path, event.dest_path)
class FileWatcher:
    """Фоновый наблюдатель за папками проекта Зевса.

    Управляет жизненным циклом watchdog Observer: запуск, остановка,
    наблюдение за списком путей. Помимо очереди колбэка, ведёт простой
    лог последних событий (ring-буфер) для отладки и UI.

    Пример использования:
        w = get_watcher()
        w.start(callback=lambda ev: print(ev))
        w.stop()
    """

    def __init__(self, patterns: list[str] | None = None,
                 recursive: bool = True,
                 log_size: int = 100) -> None:
        self.observer = Observer()
        self.patterns = patterns or _default_patterns()
        self.recursive = recursive
        self._handler: ProjectFileHandler | None = None
        self._paths: list[str] = []
        self._log: list[dict[str, Any]] = []
        self._log_size = max(log_size, 1)
        # Сопоставление «имя файла -> функция перезагрузки». Вызывается при
        # создании/изменении файла (например commands.json -> actions.reload_commands).
        self._reload_handlers: dict[str, Callable[[], None]] = {}
        # Дебаунсинг перезагрузок: watchdog при записи шлёт пачку событий,
        # таймер схлопывает их в ОДИН вызов (по ТЗ).
        self._debounce_delay = 1.5
        self._debounce_timer = None
        self._debounce_pending: dict = {}
        import threading as _th
        self._debounce_lock = _th.Lock()

    def register_reload(self, filename: str, callback) -> None:
        """Регистрирует callback с дебаунсингом (однократно после паузы)."""
        if callable(callback):
            self._reload_handlers[filename] = callback

    def _schedule_debounced_reload(self, reloader) -> None:
        """Планирует один вызов reloader через 1.5 c после последнего события."""
        import threading as _th
        with self._debounce_lock:
            self._debounce_pending[reloader] = reloader
            if self._debounce_timer is not None and self._debounce_timer.is_alive():
                self._debounce_timer.cancel()

            def _fire():
                with self._debounce_lock:
                    pending = list(self._debounce_pending.values())
                    self._debounce_pending.clear()
                for fn in pending:
                    try:
                        fn()
                    except Exception:
                        pass

            self._debounce_timer = _th.Timer(self._debounce_delay, _fire)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def set_callback(self, callback) -> None:
        """Задаёт пользовательский колбэк событий (например для UI).

        Можно вызывать в любой момент, даже если наблюдатель уже запущен.
        """
        self.callback = callback

    # --- Управление наблюдаемыми путями ---

    def add_path(self, path: str) -> None:
        """Добавляет путь к списку наблюдения.

        Путь добавляется, даже если он ещё не существует: watchdog корректно
        обработает появление папки позже. Но для гарантии рантайма дубли
        исключаются.
        """
        norm = _norm_path(path)
        if norm not in self._paths:
            self._paths.append(norm)

    def add_default_paths(self) -> None:
        """Добавляет папку data/ проекта (настройки, кэш, индекс)."""
        self.add_path(config.data_dir())

    # --- Жизненный цикл ---

    def start(self, callback: FileEventCallback | None = None,
              paths: list[str] | None = None) -> bool:
        """Запускает мониторинг в фоновом потоке.

        Args:
            callback: вызывается на каждое событие. Если не передан — события
                      складываются в лог, доступный через recent_events().
            paths: переопределяет список наблюдаемых путей. Если None — берётся
                   дефолтный (папка data/), добавленный через add_default_paths.

        Returns:
            True, если мониторинг успешно запущен.
        """
        # Колбэк применяется в любом случае (даже если наблюдатель уже запущен
        # контроллером) — чтобы UI мог зарегистрировать свой обработчик.
        if callback is not None:
            self.callback = callback  # type: ignore[assignment]

        if self.observer.is_alive():
            return True  # Уже работает, сэр.

        if paths is not None:
            self._paths = [_norm_path(p) for p in paths]

        observer_paths = self._paths
        if not observer_paths:
            self.add_default_paths()
            observer_paths = self._paths

        self._handler = ProjectFileHandler(self._dispatch, self.patterns)

        started = False
        try:
            for path in observer_paths:
                self.observer.schedule(
                    self._handler, path, recursive=self.recursive
                )
            self.observer.start()
            started = True
        except Exception:
            started = False
        return started

    def stop(self) -> None:
        """Останавливает мониторинг и ожидает остановки потока наблюдателя."""
        try:
            self.observer.stop()
            self.observer.join(timeout=2.0)
        except Exception:
            pass

    def is_running(self) -> bool:
        """Проверяет, активен ли наблюдатель."""
        return self.observer.is_alive()

    # --- События и лог ---

    def _dispatch(self, event: dict[str, Any]) -> None:
        """Внутренний обработчик: пишет в лог, зовёт колбэк и перезагрузки."""
        self._log.append(event)
        if len(self._log) > self._log_size:
            del self._log[: len(self._log) - self._log_size]
        cb = getattr(self, "callback", None)
        if callable(cb):
            try:
                cb(event)
            except Exception:
                pass

        # Перезагрузка по имени файла (например commands.json -> actions.reload_commands).
        ev = event.get("event")
        if ev in ("created", "modified"):
            fname = os.path.basename(event.get("path") or event.get("src_path") or "")
            # Игнорируем временные файлы атомарной записи (auto_scanner: .tmp)
            if fname.endswith(".tmp"):
                return
            reloader = self._reload_handlers.get(fname)
            if reloader is not None:
                self._schedule_debounced_reload(reloader)

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        """Возвращает последние зафиксированные события (для UI/отладки)."""
        return list(self._log[-limit:])


# === Глобальный экземпляр наблюдателя ===

_watcher_instance: FileWatcher | None = None


def get_watcher() -> FileWatcher:
    """Возвращает глобальный экземпляр FileWatcher (синглтон)."""
    global _watcher_instance
    if _watcher_instance is None:
        _watcher_instance = FileWatcher()
    return _watcher_instance


# === Утилиты для интеграции (удобный интерфейс) ===

def start_watching(callback: FileEventCallback | None = None) -> bool:
    """Запускает дефолтный мониторинг папки data/ проекта.

    Args:
        callback: обработчик событий (опционально).

    Returns:
        True, если запущен.
    """
    return get_watcher().start(callback=callback)


def stop_watching() -> None:
    """Останавливает глобальный мониторинг, если он запущен."""
    if _watcher_instance is not None:
        _watcher_instance.stop()