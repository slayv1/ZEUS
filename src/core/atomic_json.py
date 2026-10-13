"""Атомарная запись JSON-файлов (ТЗ v1.4 §2.2 / аудит sprint 2).

Прямая запись через ``open(path, "w")`` сначала **усекает** файл: если процесс
умрёт (краш, выключение питания, Kill process) посреди ``json.dump``, на диске
останется пустой или обрезанный JSON. Для пользовательских данных
(``commands.json`` с ярлыками, кэш ответов, индекс файлов, тема) это означает
тихую потерю данных без какой-либо ошибки в логе.

Здесь — единственная правильная реализация на весь проект:

    tmp -> flush + fsync -> os.replace

``os.replace`` на Windows и POSIX атомарен: читатель видит либо старую, либо
новую версию файла, но никогда — частично записанную.
"""
from __future__ import annotations

import json
import os
import threading
import tempfile
from typing import Any

# Запись одного и того же файла из разных потоков (слайдеры, тумблеры,
# фоновые сканеры) не должна порождать гонку на *.tmp: имена tmp уникальны
# (mkstemp), но лок страховит от одновременного replace одного файла.
_lock = threading.Lock()


def write_json_atomic(path: str, data: Any, *, indent: int = 2,
                      sort_keys: bool = False) -> bool:
    """Пишет ``data`` в ``path`` атомарно. Возвращает True при успехе.

    Ошибки не глотаются молча: возвращаем False, а исходный файл остаётся
    нетронутым (в отличие от ``open(path, "w")``, который уже усечён).
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_path: str | None = None
    try:
        with _lock:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent,
                          sort_keys=sort_keys)
                f.flush()
                os.fsync(f.fileno())   # данные на диске ДО переименования
            os.replace(tmp_path, path)
            tmp_path = None
        return True
    except Exception:  # noqa: BLE001 — вызывающий сам решает, критично ли это
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def read_json_safe(path: str, default: Any = None) -> Any:
    """Читает JSON, возвращая ``default`` при отсутствии/порче файла.

    Полезно в паре с :func:`write_json_atomic`: битый JSON больше не роняет
    загрузку и не затирается «спасительным» пустым словарём молча.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
