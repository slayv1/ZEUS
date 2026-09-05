"""Точка входа в приложение «Зевс v1.1».

Запускает основной графический интерфейс на Flet из src/ui/main_flet.py.
Логика и контроллеры лежат в src/, конфигурация — в data/, модели — в models/.

Запуск:
    python main.py
"""
import os
import sys

# Добавляем папку src/ в sys.path, чтобы импортировать модули проекта
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import flet as ft
from core import config
from ui import main_flet  # pyright: ignore[reportMissingImports]


def _set_window_icon(page: ft.Page) -> None:
    """Устанавливает брендовую иконку окна (ТЗ: замена стандартной иконки Flet).

    Выполняется через before_main — до отрисовки интерфейса, чтобы иконка
    заголовка окна и панели задач была фирменной с первого кадра.
    """
    try:
        icon_path = config.resource_path("assets/icons/app_icon.ico")
        if os.path.exists(icon_path):
            page.window.icon = icon_path
    except Exception:
        pass  # иконка не критична — приложение работает и без неё


if __name__ == "__main__":
    # В noconsole-сборке PyInstaller sys.stdout/stderr равны None —
    # подменяем на devnull, чтобы любая библиотека не упала на записи.
    import io as _io
    for _idx, _stream in enumerate((sys.stdout, sys.stderr)):
        if _stream is None:
            try:
                _stream = open(os.devnull, "w", encoding="utf-8")
                if _idx == 0:
                    sys.stdout = _stream
                else:
                    sys.stderr = _stream
            except Exception:
                pass

    # Безопасный вывод в консоль (UTF-8): предотвращает UnicodeEncodeError
    # на консолях CP1251 при любых символах в логах/печати.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    # Глобальный перехватчик исключений: логирует непредвиденные падения
    # в data/logs/crash_*.log и мягко уведомляет голосом «Произошла системная
    # ошибка, сэр», вместо аварийного закрытия окна.
    from core.crash_handler import install_crash_handler  # pyright: ignore[reportMissingImports]
    install_crash_handler()

    # Защита от второго экземпляра: две копии дерутся за микрофон и
    # Vosk-модель, что приводит к нативному крашу libvosk.dll.
    import ctypes
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "ZeusAssistant_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("[Zeus] Уже запущен — активирую существующее окно и выхожу.")
        sys.exit(0)

    ft.run(main_flet.main, view=ft.AppView.FLET_APP, before_main=_set_window_icon)
