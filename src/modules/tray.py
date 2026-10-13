"""System tray integration for Zeus on Windows."""
from __future__ import annotations

import os
import threading
from typing import Callable


def _tray_log(message: str) -> None:
    """Пишет сбой трея в zeus.log. Трей не должен ронять окно."""
    try:
        from core.logging_service import error
        error(f"[Tray] {message}")
    except Exception:
        pass


class TrayManager:
    """Runs a pystray icon without blocking the Flet event loop.

    Поддерживает динамическое аудио-подменю (ТЗ §2.3): список микрофонов
    и динамиков с radio-галочкой у активного устройства. Меню пересоздаётся
    при каждом открытии через callable-генератор pystray.Menu(fn), что
    позволяет отражать горячее подключение устройств без перезапуска трея.
    """

    def __init__(
        self,
        on_open: Callable[[], None],
        on_settings: Callable[[], None],
        on_exit: Callable[[], None],
        icon_path: str | None = None,
        on_pick_input: Callable[[str | None], None] | None = None,
        on_pick_output: Callable[[str | None], None] | None = None,
    ):
        self._on_open = on_open
        self._on_settings = on_settings
        self._on_exit = on_exit
        self._icon_path = icon_path
        self._on_pick_input = on_pick_input
        self._on_pick_output = on_pick_output

        self._icon = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False

        # Данные аудиоустройств для динамического меню
        self._devices_in: list[dict] = []    # [{"id": ..., "name": ...}, ...]
        self._devices_out: list[dict] = []
        self._active_in_id: str | None = None
        self._active_out_id: str | None = None
        self._devices_lock = threading.Lock()

        # Handshake между start() и фоновым циклом: без него start() возвращал
        # True сразу после thread.start(), даже если цикл pystray падал при
        # старте — UI считал трей рабочим и прятал окно по крестику,
        # получая «зомби»-процесс без иконки.
        self._run_started = threading.Event()
        self._run_error: Exception | None = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

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
            # Передаём callable — pystray вызывает его при каждом показе меню,
            # что обеспечивает актуальный список устройств без перезапуска.
            menu = pystray.Menu(self._build_menu)
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

    def update_audio_devices(
        self,
        devices_in: list[dict],
        devices_out: list[dict],
        active_in_id: str | None,
        active_out_id: str | None,
    ) -> None:
        """Обновляет список аудиоустройств в подменю трея.

        Вызывается из контроллера при изменении списка устройств (hot-plug)
        или при первичной инициализации. Принудительно обновляет меню через
        icon.update_menu(), если трей запущен.

        Args:
            devices_in: Список микрофонов [{"id": ..., "name": ...}, ...].
            devices_out: Список динамиков/наушников.
            active_in_id: ID активного устройства ввода (или None).
            active_out_id: ID активного устройства вывода (или None).
        """
        with self._devices_lock:
            self._devices_in = list(devices_in)
            self._devices_out = list(devices_out)
            self._active_in_id = active_in_id
            self._active_out_id = active_out_id

        # Принудительное обновление меню в потоке pystray
        icon = self._icon
        if icon is not None:
            try:
                icon.update_menu()
            except Exception as exc:  # noqa: BLE001
                print(f"[Tray] Не удалось обновить меню: {exc}")

    # ------------------------------------------------------------------
    # Построение динамического меню
    # ------------------------------------------------------------------

    def _build_menu(self):
        """Callable-генератор меню: вызывается pystray при каждом показе.

        Возвращает кортеж MenuItem-объектов. Аудио-подменю генерируется
        динамически на основе текущих _devices_in / _devices_out.
        """
        try:
            import pystray
        except Exception as exc:  # noqa: BLE001
            _tray_log(f"pystray недоступен: {exc}")
            return ()

        with self._devices_lock:
            devices_in = list(self._devices_in)
            devices_out = list(self._devices_out)
            active_in = self._active_in_id
            active_out = self._active_out_id

        items = [
            pystray.MenuItem("Открыть / Развернуть", self._open, default=True),
            pystray.Menu.SEPARATOR,
        ]

        # --- Подменю микрофонов ---
        if self._on_pick_input is not None:
            mic_items = self._build_device_submenu(
                pystray, devices_in, active_in, self._on_pick_input
            )
            items.append(
                pystray.MenuItem("🎤 Микрофон", pystray.Menu(*mic_items))
            )

        # --- Подменю динамиков ---
        if self._on_pick_output is not None:
            spk_items = self._build_device_submenu(
                pystray, devices_out, active_out, self._on_pick_output
            )
            items.append(
                pystray.MenuItem("🔊 Динамики", pystray.Menu(*spk_items))
            )

        if self._on_pick_input is not None or self._on_pick_output is not None:
            items.append(pystray.Menu.SEPARATOR)

        items.extend([
            pystray.MenuItem("Настройки", self._settings),
            pystray.MenuItem("Выход", self._exit),
        ])

        return tuple(items)

    @staticmethod
    def _build_device_submenu(
        pystray,
        devices: list[dict],
        active_id: str | None,
        on_pick: Callable[[str | None], None],
    ) -> list:
        """Строит список MenuItems для одного подменю устройств (вход или выход).

        Каждый пункт — radio-кнопка с галочкой у активного устройства.
        """
        if not devices:
            return [pystray.MenuItem("(нет устройств)", None, enabled=False)]

        items = []
        for dev in devices:
            dev_id = dev.get("id")
            dev_name = dev.get("name", dev.get("label", "?"))
            # Замыкание: захватываем dev_id по значению через аргумент по умолчанию
            def make_action(d_id=dev_id, cb=on_pick):
                def action(icon=None, item=None):
                    try:
                        cb(d_id)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[Tray] Ошибка выбора устройства: {exc}")
                return action

            def make_checked(d_id=dev_id, a_id=active_id):
                return lambda item: d_id == a_id

            items.append(
                pystray.MenuItem(
                    dev_name,
                    make_action(),
                    checked=make_checked(),
                    radio=True,
                )
            )
        return items

    # ------------------------------------------------------------------
    # Внутренние обработчики
    # ------------------------------------------------------------------

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
        except Exception as exc:
            _tray_log(f"не показал иконку: {exc}")
        self._run_started.set()

    def _load_image(self, image_module, draw_module):
        if self._icon_path and os.path.exists(self._icon_path):
            try:
                # Контекст-менеджер закрывает файловый дескриптор .ico
                # (PIL держит файл открытым, пока картинка «ленивая»).
                with image_module.open(self._icon_path) as img:
                    return img.convert("RGBA")
            except Exception as exc:
                _tray_log(f"не открыл иконку: {exc}")

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

