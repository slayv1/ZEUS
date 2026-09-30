#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автономный консольный мониторинг файловой системы на основе watchdog.

Скрипт следит за изменениями (создание, изменение, удаление, перемещение)
в указанной папке и выводит события в консоль в реальном времени.

Примеры запуска:
    python watch_watchdog.py                       # текущая папка, все файлы
    python watch_watchdog.py C:/path/to/folder     # конкретная папка
    python watch_watchdog.py data --pattern "*.json"
    python watch_watchdog.py --no-recursive --pattern "*.py;*.txt" D:/src

Завершение: Ctrl+C (наблюдатель корректно останавливается).
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import sys
import time
from datetime import datetime
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


# Иконки и русские названия событий
_EVENT_LABELS = {
    "created": ("📁", "Создан"),
    "modified": ("✏️", "Изменён"),
    "deleted": ("🗑", "Удалён"),
    "moved": ("➡️", "Перемещён"),
}

# ANSI-цвета (если терминал их поддерживает)
COLOR_ENABLED = (
    hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
) and os.environ.get("NO_COLOR", "") == ""
_COLORS = {
    "created": "\033[32m",
    "modified": "\033[33m",
    "deleted": "\033[31m",
    "moved": "\033[36m",
    "reset": "\033[0m",
    "dim": "\033[2m",
}


def _ts() -> str:
    """Возвращает текущее время в формате ЧЧ:ММ:СС."""
    return datetime.now().strftime("%H:%M:%S")


def _out(text: str, file: Any = None) -> None:
    """Печатает текст, устойчиво к неполной кодировке stdout.

    При перенаправлении вывода в файл/пайп на Windows stdout кодируется
    в cp1251, и эмодзи роняли скрипт с UnicodeEncodeError (та же проблема,
    что была у taobao_core до _safe_print). Нечекорректные символы
    заменяются '?', но наблюдение продолжает работать.
    """
    if file is None:
        file = sys.stdout
    try:
        print(text, file=file, flush=True)
    except UnicodeEncodeError:
        enc = getattr(file, "encoding", None) or "utf-8"
        safe = text.encode(enc, errors="replace").decode(enc, errors="replace")
        try:
            print(safe, file=file, flush=True)
        except Exception:  # noqa: BLE001 — вывод не должен ронять мониторинг
            pass


def _paint(event_type: str, text: str) -> str:
    """Окрашивает текст в цвет события, если цвет включён."""
    if COLOR_ENABLED:
        return f"{_COLORS.get(event_type, '')}{text}{_COLORS['reset']}"
    return text


class PrintHandler(FileSystemEventHandler):
    """Выводит события files-системы в консоль, фильтруя по паттернам."""

    def __init__(self, patterns: list[str]) -> None:
        super().__init__()
        self.patterns = patterns  # пустой список = слушать все файлы

    def _matches(self, path: str | None, is_directory: bool | None = None) -> bool:
        """Проверяет соответствие пути паттернам (папки слушаем всегда).

        Флаг директории берётся из события watchdog: для удалённых папок
        os.path.isdir(path) уже False (путь не существует), и папка
        ошибочно просеивалась бы через паттерны имён файлов.
        """
        if not path:
            return True
        if is_directory is None:
            is_directory = os.path.isdir(path)
        if is_directory:
            return True
        if not self.patterns:
            return True
        name = os.path.basename(path)
        return any(fnmatch.fnmatch(name, pat) for pat in self.patterns)

    def _show(self, event_type: str, path: str, dest: str | None = None,
              is_directory: bool | None = None) -> None:
        if not self._matches(path, is_directory=is_directory):
            return
        icon, label = _EVENT_LABELS.get(event_type, ("", event_type))
        line = f"[{_ts()}] {icon} {label}: {path}"
        if dest:
            line += f" -> {dest}"
        _out(_paint(event_type, line))

    # Интеграция с watchdog
    def on_created(self, event: Any) -> None:
        self._show("created", event.src_path, is_directory=event.is_directory)

    def on_modified(self, event: Any) -> None:
        self._show("modified", event.src_path, is_directory=event.is_directory)

    def on_deleted(self, event: Any) -> None:
        self._show("deleted", event.src_path, is_directory=event.is_directory)

    def on_moved(self, event: Any) -> None:
        self._show("moved", event.src_path, event.dest_path,
                   is_directory=event.is_directory)


def _parse_patterns(raw: str | None) -> list[str]:
    """Разбирает строку паттернов, разделённых ';' или ',' в список."""
    if not raw:
        return []
    return [p.strip() for p in raw.replace(",", ";").split(";") if p.strip()]


def resolve_path(raw: str) -> str:
    """Возвращает абсолютный нормализованный путь или выводит ошибку и выходит."""
    path = os.path.abspath(raw)
    if not os.path.exists(path):
        _out(f"Ошибка: путь не существует — {path}", file=sys.stderr)
        raise SystemExit(2)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Мониторинг изменений файлов через watchdog.",
        epilog="Завершение работы: Ctrl+C.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Папка для наблюдения (по умолчанию: текущая).",
    )
    parser.add_argument(
        "--pattern",
        default="",
        help="Фильтр файлов по glob-паттернам, разделённым ';' (например '*.json;*.py'). "
        "Если не задан — отслеживаются все файлы.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        default=True,
        help="Рекурсивно наблюдать подпапки (по умолчанию включено).",
    )
    parser.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        help="Наблюдать только за указанной папкой (без подпапок).",
    )
    args = parser.parse_args()

    path = resolve_path(args.path)
    patterns = _parse_patterns(args.pattern)

    handler = PrintHandler(patterns)
    observer = Observer()

    observer.schedule(handler, path, recursive=args.recursive)
    observer.start()

    _out(
        _paint("modified", "👁 Наблюдаю за:") +
        f" {path}\n" +
        f"   Паттерны: {', '.join(patterns) if patterns else 'все файлы'}\n" +
        f"   Рекурсивно: {'да' if args.recursive else 'нет'}\n" +
        "   Для остановки нажмите Ctrl+C.",
    )

    try:
        while observer.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join(timeout=3.0)

    _out("\nНаблюдение остановлено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())