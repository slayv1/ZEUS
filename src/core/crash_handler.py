"""Глобальный перехватчик исключений (Crash Handler) для Зевса.

Защита от аварийного закрытия: если в любом потоке приложения происходит
непредвиденное исключение, оно:
  1. Записывается в файл лога  data/logs/crash_<дата>_<время>.log
     с полным трейсбеком и временем срабатывания.
  2. Если зарегистрирован нотификатор (голосовой движок) — произносится
     мягкое уведомление «Произошла системная ошибка, сэр».

Перехватываются оба канала исключений Python:
  - sys.excepthook        — исключения основного потока;
  - threading.excepthook  — исключения в фоновых потоках.

В отличие от KeyboardInterrupt/SystemExit (намеренный выход), обычные
ошибки не приводят к мгновенному аварийному закрытию окна.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback

try:
    from core import config
except Exception:  # noqa: BLE001 — на случай импорта вне проекта
    config = None


# Функция уведомления (например, audio.speak). Может быть None.
_notifier = None


def set_crash_notifier(fn) -> None:
    """Регистрирует функцию голосового уведомления (напр. audio.speak)."""
    global _notifier
    _notifier = fn


def _normalize_exc_name(name: str) -> str:
    """Убирает префиксы '<class \'...\'>' из имени исключения."""
    return name.replace("<class '", "").replace("'>", "").replace("class ", "")


def _write_crash_log(exc_type, exc_value, tb) -> None:
    """Записывает трейсбек в data/logs/crash_*.log (best-effort)."""
    try:
        if config is not None:
            logs_dir = os.path.join(config.data_dir(), "logs")
        else:
            logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(logs_dir, exist_ok=True)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(logs_dir, f"crash_{stamp}.log")

        thread_name = threading.current_thread().name
        lines = [
            f"Время: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Поток: {thread_name}",
            f"Тип исключения: {_normalize_exc_name(str(exc_type) or exc_type.__name__)}",
            f"Сообщение: {exc_value}",
            "Трейсбек:",
            "".join(traceback.format_exception(exc_type, exc_value, tb)),
        ]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"[CrashHandler] Системная ошибка записана в {path}")
        return path
    except Exception as e:  # noqa: BLE001 — не должны падать в самом перехватчике
        print(f"[CrashHandler] Не удалось записать лог ошибки: {e}")
        return None


def _notify() -> None:
    """Мягко уведомляет пользователя голосом, если нотификатор доступен."""
    global _notifier
    if _notifier is None:
        return
    try:
        _notifier("Произошла системная ошибка, сэр.")
    except Exception as e:  # noqa: BLE001
        print(f"[CrashHandler] Не удалось озвучить уведомление: {e}")


def _handle(exc_type, exc_value, tb) -> None:
    """Единый обработчик исключений (sys.excepthook и threading.excepthook).

    KeyboardInterrupt / SystemExit — намеренное завершение, пересылаем в
    стандартный обработчик, чтобы не маскировать выход пользователя.
    """
    if issubclass(exc_type, KeyboardInterrupt) or issubclass(exc_type, SystemExit):
        sys.__excepthook__(exc_type, exc_value, tb)
        return

    _write_crash_log(exc_type, exc_value, tb)
    _notify()


def install_crash_handler() -> None:
    """Устанавливает глобальные перехватчики исключений приложения."""
    sys.excepthook = _handle
    try:
        threading.excepthook = _handle
    except Exception:  # noqa: BLE001 — не во всех версиях Python есть атрибут
        pass
    print("[CrashHandler] Глобальный перехватчик исключений установлен")