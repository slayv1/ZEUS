"""System tray integration for Zeus on Windows."""
from __future__ import annotations

import os
import threading
from typing import Callable


class TrayManager:
    """Runs a pystray icon without blocking the Flet event loop."""

    def __init__(
        self,
        on_open: Callable[[], None],
        on_settings: Callable[[], None],
        on_exit: Callable[[], None],
        icon_path: str | None = None,
    ):
        self._on_open = on_open
        self._on_settings = on_settings
        self._on_exit = on_exit
        self._icon_path = icon_path
        self._icon = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False
        # Handshake между start() и фоновым циклом: без него start() возвращал
        # True сразу после thread.start(), даже если цикл pystray падал при
        # старте — UI считал трей рабочим и прятал окно по крестику,
        # получая «зомби»-процесс без иконки.
        self._run_started = threading.Event()
        self._run_error: Exception | None = None

    def start(self) -> bool:
        """Starts the tray loop in a daemon thread.

        Возвращает True только если цикл pystray действительно поднялся
        (иконка создана и видима), иначе False — вызывающий код обязан
        считать трей недоступным.
        """
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as exc:  # noqa: BLE001
            print(f"[Tray] Трей недоступен: {exc}")
            return False

        with self._lock:
            if self._running:
                return True
            image = self._load_image(Image, ImageDraw)
            menu = pystray.Menu(
                pystray.MenuItem(
                    "Открыть / Развернуть", self._open, default=True
                ),
                pystray.MenuItem("Настройки", self._settings),
                pystray.MenuItem("Выход", self._exit),
            )
            self._icon = pystray.Icon("Zeus", image, "Zeus", menu)
            self._run_started.clear()
            self._run_error = None
            self._running = True
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="zeus-tray"
            )
            self._thread.start()

        if not self._run_started.wait(timeout=3.0):
            reason = self._run_error or "тайм-аут запуска цикла"
            print(f"[Tray] Цикл трея не запустился: {reason}")
            self.stop()
            return False
        if self._run_error is not None:
            print(f"[Tray] Цикл трея упал при старте: {self._run_error}")
            self.stop()
            return False
        return True

    def stop(self) -> None:
        """Stops the tray icon and its background loop."""
        with self._lock:
            icon = self._icon
            self._icon = None
            self._running = False
        if icon is not None:
            try:
                icon.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[Tray] Не удалось остановить трей: {exc}")
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # Ограниченный join: даём циклу pystray корректно завершиться,
            # не блокируя UI-поток дольше секунды.
            thread.join(timeout=1.0)

    def _run(self) -> None:
        # Локальная ссылка ОБЯЗАТЕЛЬНА: stop() обнуляет self._icon из другого
        # потока, и повторное чтение self._icon.run() здесь даёт гонку
        # (AttributeError: 'NoneType' object has no attribute 'run').
        icon = self._icon
        try:
            if icon is not None:
                icon.run(setup=self._on_tray_ready)
        except Exception as exc:  # noqa: BLE001
            self._run_error = exc
            # Пробуждаем start(): иначе он ждёт полный тайм-аут.
            self._run_started.set()
            print(f"[Tray] Ошибка цикла трея: {exc}")
        finally:
            with self._lock:
                self._running = False

    def _on_tray_ready(self, icon) -> None:
        """pystray setup-колбэк: вызывается в цикле трея после инициализации."""
        try:
            icon.visible = True
        except Exception:
            pass
        self._run_started.set()

    def _load_image(self, image_module, draw_module):
        if self._icon_path and os.path.exists(self._icon_path):
            try:
                # Контекст-менеджер закрывает файловый дескриптор .ico
                # (PIL держит файл открытым, пока картинка «ленивая»).
                with image_module.open(self._icon_path) as img:
                    return img.convert("RGBA")
            except Exception:
                pass

        image = image_module.new("RGBA", (64, 64), (8, 12, 20, 255))
        draw = draw_module.Draw(image)
        draw.ellipse((8, 8, 56, 56), fill=(30, 64, 175, 255))
        draw.polygon((32, 14, 23, 34, 31, 34, 25, 51, 43, 28, 34, 28), fill="white")
        return image

    def _open(self, icon=None, item=None) -> None:
        # Колбэки вызываются в потоке pystray: необработанное исключение
        # убивает цикл трея (иконка исчезает, а UI продолжает считать её живой).
        try:
            self._on_open()
        except Exception as exc:  # noqa: BLE001
            print(f"[Tray] Ошибка обработчика «Открыть»: {exc}")

    def _settings(self, icon=None, item=None) -> None:
        try:
            self._on_settings()
        except Exception as exc:  # noqa: BLE001
            print(f"[Tray] Ошибка обработчика «Настройки»: {exc}")

    def _exit(self, icon=None, item=None) -> None:
        try:
            self._on_exit()
        except Exception as exc:  # noqa: BLE001
            print(f"[Tray] Ошибка обработчика «Выход»: {exc}")
