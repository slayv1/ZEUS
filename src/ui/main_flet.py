"""UI Зевса на Flet (навигационная панель с 2 разделами).

Разделы:
   1. «Голосовой помощник» — монитор логов, статус, кнопки управления
   2. «Настройки» — настройки приложения

Стиль: тёмная тема Hydra.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from typing import Any

import flet as ft

# Обеспечиваем доступность модулей src/ при запуске файла напрямую
# (python src/ui/main_flet.py), когда корень src/ не в sys.path.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_ROOT = os.path.dirname(_THIS_DIR)  # src/
for _p in (_SRC_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import config
from core.config import THEMES
from core.file_watcher import start_watching, stop_watching

# ВАЖНО: ZeusController НЕ импортируем на уровне модуля — его цепочка
# (vosk, sounddevice, piper, pyaudio) загружается секунды и откладывала
# старт socket-сервера Flet: клиент всё это время показывал «Working…».
# Импортируем лениво в _boot_worker, уже ПОСЛЕ отрисовки сплэша.


# === Design Tokens (ТЗ v1.2: светлая и тёмная темы) ===
# Ключи по ТЗ: background_main, background_surface, color_primary,
# text_high_emphasis, text_low_emphasis, terminal_background, terminal_text.
# Старые ключи (bg, surface, electric, ...) оставлены как алиасы —
# на них ссылается остальной код UI.
PALETTES = {
    "dark": {
        # --- Токены референса (ТЗ: сдержанный, глубокий, тёмный) ---
        "background_main": "#020408",      # Deep Space (ещё глубже)
        "background_surface": "#080C14",   # Panel (ещё темнее)
        "color_primary": "#1E40AF",        # Primary Blue (глубокий, не неон)
        "color_cyan": "#0369A1",           # Cyan accent (приглушённый)
        "text_high_emphasis": "#E2E8F0",   # Text (мягче, не чистый белый)
        "text_low_emphasis": "#475569",    # Muted (ещё более приглушённый)
        "terminal_background": "#010203",  # Terminal (почти чёрный)
        "terminal_text": "#22C55E",        # Success (мягче зелёный)
        "glow_primary": "#1E3A8A",         # Glow (глубокий, не резкий)
        "glow_cyan": "#075985",            # Cyan glow (приглушённый)
        # --- Алиасы (совместимость с существующим кодом) ---
        "bg": "#020408",
        "surface": "#080C14",
        "surface_2": "#0A0F1A",
        "card": "#080C14",
        "card_border": "#111827",
        "text": "#E2E8F0",
        "text_dim": "#475569",
        "accent": "#1E40AF",
        "accent_dim": "#1E40AF",
        "border": "#111827",
        "hover": "#111827",
        "electric": "#1E40AF",
        "cyan": "#0369A1",
        "sidebar": "#080C14",
    },
    "light": {
        # --- Токены референса (ТЗ §15) ---
        "background_main": "#F8FAFC",
        "background_surface": "#FFFFFF",
        "color_primary": "#1E40AF",
        "color_cyan": "#0369A1",
        "text_high_emphasis": "#1E293B",
        "text_low_emphasis": "#475569",
        "terminal_background": "#FFFFFF",
        "terminal_text": "#166534",
        "glow_primary": "#1E3A8A",
        "glow_cyan": "#075985",
        # --- Алиасы ---
        "bg": "#F8FAFC",
        "surface": "#FFFFFF",
        "surface_2": "#E8EDF6",
        "card": "#FFFFFF",
        "card_border": "#E2E8F0",
        "text": "#1E293B",
        "text_dim": "#475569",
        "accent": "#1E40AF",
        "accent_dim": "#1E40AF",
        "border": "#E2E8F0",
        "hover": "#E8EFFA",
        "electric": "#1E40AF",
        "cyan": "#0369A1",
        "sidebar": "#FFFFFF",
    },
}

# Текущая активная палитра (по умолчанию тёмная)
C = dict(PALETTES["dark"])


# === Секции навигации (ТЗ v1.2: Dashboard / Логи / Настройки) ===
SECTIONS = [
    {"label": "Главная", "icon": ft.Icons.DASHBOARD_ROUNDED},
    {"label": "Системные логи", "icon": ft.Icons.TERMINAL_ROUNDED},
    {"label": "Настройки", "icon": ft.Icons.SETTINGS_ROUNDED},
]


class ZeusUI:
    """UI Зевса на Flet.

    Связывает ZeusController с визуальным интерфейсом.
    Обрабатывает все коллбэки от контроллера.
    """

    def __init__(self, page: ft.Page):
        self.page = page

        # --- Маршализация UI-обновлений через один поток ---
        # Все коллбэки контроллера (аудио, zeus-commands, boot, watchdog)
        # и обработчики событий Flet исполняются в разных потоках.
        # Конкурентные control.update() из нескольких потоков — известная
        # причина зависания отрисовки Flet («Working…»). Поэтому каждое
        # изменение контролов ставится в очередь и исполняется строго
        # последовательно единственным потоком zeus-ui.
        self._ui_queue: queue.Queue = queue.Queue()
        threading.Thread(
            target=self._ui_loop, daemon=True, name="zeus-ui"
        ).start()

        # Контроллер создаётся отложенно в фоновом потоке (см. _boot_worker),
        # чтобы тяжёлая инициализация движков (Vosk, аудио, Ollama, сканеры)
        # не блокировала отрисовку интерфейса на экране «Загрузка…».
        self.controller = None
        self._ready = False
        self._boot_error = None
        self._boot_timeout_sec = 40.0
        self._boot_thread: threading.Thread | None = None

        # Текущий выбранный раздел
        self.selected_index = 0

        # Атрибуты голографического визуализатора (ТЗ v1.2: центральный техно-круг)
        self.holo_image: ft.Image | None = None
        # Анимированный контейнер масштабирования при речи (ТЗ v1.2 §3)
        self.holo_container: ft.Container | None = None
        # Флаг для остановки анимации при закрытии окна (close-handler)
        self._holo_running = False

        # Статистика сессии (для лога голосового помощника)
        self._stats_cmds = 0

        # UI элементы
        self.sidebar: ft.Container | None = None
        self.content_area: ft.Container | None = None
        self.app_bar: ft.AppBar | None = None

        # --- Раздел 0: Голосовой помощник ---
        self.log_text: ft.Text | None = None
        self.voice_status: ft.Text | None = None
        self.sleep_button: ft.IconButton | None = None
        self.listen_button: ft.IconButton | None = None
        self.voice_switch: ft.Switch | None = None
        self.voice_content: ft.Container | None = None

        # --- Раздел 1: Настройки ---
        self.settings_content: ft.Container | None = None

        self._watcher_started = False
        self._hotkey = None

        # Мгновенный «сплэш» загрузки: UI отрисовывается сразу, а не после
        # инициализации движков. Окно не зависает на надписи «Загрузка…».
        self._build_splash()
        self._show_page_text("Загрузка ресурсов...")

        # Фоновая инициализация тяжёлых движков (2.1 — перенос в поток).
        self._boot_thread = threading.Thread(
            target=self._boot_worker, daemon=True, name="zeus-boot"
        )
        self._boot_thread.start()

        # Защита от бесконечного ожидания (2.2): если инициализация затянулась,
        # выводим понятное сообщение в UI/лог, а не висим на «Загрузка…».
        threading.Thread(
            target=self._timeout_guard, daemon=True, name="zeus-boot-timeout"
        ).start()

    def _post_ui(self, fn) -> None:
        """Ставит функцию обновления UI в очередь потока zeus-ui."""
        try:
            self._ui_queue.put(fn)
        except Exception:  # noqa: BLE001
            pass

    def _ui_loop(self) -> None:
        """Единственный поток, выполняющий изменения контролов Flet."""
        while True:
            fn = self._ui_queue.get()
            if fn is None:
                self._ui_queue.task_done()
                break
            try:
                fn()
            except RuntimeError:
                pass  # страница закрыта — игнорируем
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
            finally:
                try:
                    self._ui_queue.task_done()
                except Exception:  # noqa: BLE001
                    pass

    def _build_splash(self):
        """Показывает лёгкую заглушку, пока в фоне грузятся движки."""
        page = self.page
        # Нативная строка заголовка Windows: только имя приложения.
        # Кнопки свернуть, развернуть и закрыть оставляет сама ОС.
        page.title = "Zeus"
        page.theme_mode = ft.ThemeMode.DARK
        page.bgcolor = C["bg"]
        page.padding = 0
        self._splash_text = ft.Text(
            "Загрузка ресурсов…", color=C["text"], size=16
        )
        # ТЗ v1.2 §3: НЕ чистим page.controls — корень страницы создаём
        # ОДИН раз (единый контейнер-обёртка), а сплэш и основной макет просто
        # подменяют его содержимое. Это исключает серый экран при переключении.        
        self._splash = ft.Container(
            content=ft.Column(
                [
                    ft.ProgressRing(color=C["accent"], width=44, height=44),
                    self._splash_text,
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=14,
            ),
            alignment=ft.Alignment.CENTER,
            bgcolor=C["bg"],
            expand=True,
        )
        self._root = ft.Column(
            [self._splash],
            expand=True,
            spacing=0,
        )
        page.add(self._root)
        page.update()

    def _show_page_text(self, text: str):
        """Динамически обновляет текст на экране загрузки (через zeus-ui)."""
        self._post_ui(lambda: self._do_show_page_text(text))

    def _do_show_page_text(self, text: str):
        try:
            if getattr(self, "_splash_text", None) is not None:
                self._splash_text.value = text
                self._splash_text.update()
        except RuntimeError:
            pass

    def _boot_worker(self) -> None:
        """Фоновый поток: инициализация контроллера и сборка UI.

        Всё выполняется в отдельном потоке, чтобы не блокировать главный поток
        Flet. Любая ошибка перехватывается (2.2) и выводится в UI/лог.

        ВАЖНО: само построение UI (_build_ui) и подключение колбэков НЕ
        выполняется здесь напрямую — только через _post_ui в поток zeus-ui.
        Операции с page (add/update/on_window_event) из постороннего потока —
        известная причина плашки «Working…» в Flet.
        """
        import time as _t

        try:
            t0 = _t.time()
            # Контроллер (тяжёлое: Vosk-модель, аудио, Ollama, сканеры).
            # Импорт здесь же: тяжёлые модули грузятся в фоне, не мешая
            # клиенту Flet подключиться и убрать «Working…» как можно раньше.
            if self.controller is None:
                self._show_page_text("Загрузка ресурсов… (движки)")
                from core.controller import ZeusController
                self.controller = ZeusController()

            self._show_page_text("Загрузка интерфейса…")
            # Сборка UI и колбэков — строго в потоке zeus-ui (единственный
            # владелец page). Лёгкое, мгновенное; тяжёлое уже загружено.
            self._post_ui(self._assemble_ui)

            self._ready = True
            self._boot_error = None
            print(f"[ZeusUI] Инициализация завершена за {_t.time() - t0:.1f} с")

            # Убираем сплэш — уже подменён в _build_ui (Их корень _root),
            # поэтому отдельный page.remove() не нужен (исключает серый экран.        
            # (В прежнем коде _remove_splash дергал page.remove на демонтированный
            #   объект — могло дестабилизировать отрисовку))
            self._on_status("Зевс готов к работе")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._ready = True
            self._boot_error = e
            try:
                self._on_status(f"Ошибка загрузки: {e}")
                self._log(f"Ошибка инициализации: {e}")
                self._show_page_text(f"Ошибка загрузки: {e}")
            except RuntimeError:
                pass

    def _assemble_ui(self) -> None:
        """Собирает интерфейс и подключает колбэки.

        Выполняется ТОЛЬКО в потоке zeus-ui (через _post_ui), т.к. напрямую
        манипулирует page. Это исключает гонки и плашку «Working…».
        """
        self._build_ui()
        self._setup_controller()
        self._init_file_watcher()
        self._start_hotkey()

    def _timeout_guard(self) -> None:
        """Стражник: если загрузка затянулась — сообщаем и продолжаем ждать."""
        import time as _t
        _t.sleep(self._boot_timeout_sec)
        if self._ready:
            return
        try:
            self._show_page_text("Загрузка затянулась… проверьте микрофон/интернет")
            self._log("Загрузка движков не завершилась вовремя (тайм-аут)")
            self._on_status("Загрузка затянулась…")
        except RuntimeError:
            pass

    def _init_file_watcher(self):
        """Запускает watchdog-наблюдатель за папкой data/ проекта.

        События создания/изменения/удаления конфигураций логируются в панель
        логов. Наблюдатель работает в фоновом потоке и не блокирует UI.
        """
        started = start_watching(callback=self._on_file_event)
        self._watcher_started = started
        # Гарантированно останавливаем наблюдатель при закрытии окна
        self.page.on_window_event = self._on_window_event

    def _on_file_event(self, event: dict):
        """Коллбэк watchdog: логирует изменение файла в панель логов."""
        ev = event.get("event")
        path = event.get("path") or event.get("dest_path") or "?"
        try:
            from datetime import datetime
            ts = datetime.fromtimestamp(event.get("timestamp", 0)).strftime("%H:%M:%S")
        except Exception:
            ts = ""
        label = {
            "created": "📁 Создан",
            "modified": "✏️ Изменён",
            "deleted": "🗑 Удалён",
            "moved": "➡️ Перемещён",
        }.get(ev, ev)
        self._log(f"{label}: {os.path.basename(path)} [{ts}]")

    def _on_window_event(self, event):
        """Останавливает фоновые ресурсы (watcher, хоткей) при закрытии окна.
        
        Также обрабатывает события максимизации/восстановления для корректного
        обновления layout при изменении размера окна.
        """
        try:
            if event.data == "close":
                if self._watcher_started:
                    stop_watching()
                    self._watcher_started = False
                if self._hotkey is not None:
                    try:
                        self._hotkey.stop()
                    except Exception:
                        pass
                    self._hotkey = None
                # Останавливаем поток анимации голографического визуализатора
                self._holo_running = False
            elif event.data in ("maximize", "unmaximize", "resize"):
                # Принудительно обновляем layout при изменении размера окна
                try:
                    if hasattr(self, "_root") and self._root is not None:
                        self._root.update()
                    self.page.update()
                except Exception:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Глобальная горячая клавиша (Ctrl+Alt+Space)
    # ------------------------------------------------------------------
    def _start_hotkey(self):
        """Регистрирует Ctrl+Alt+Space для принудительного вызова ассистента."""
        if not self.controller.settings.get("app_settings", {}).get("hotkey_enabled", True):
            self._log("Горячая клавиша отключена в настройках")
            return
        try:
            from modules.hotkeys import HotkeyManager
        except Exception:
            self._log("⌨ Модуль горячих клавиш недоступен")
            return
        self._hotkey = HotkeyManager()
        started = self._hotkey.start(self._on_global_hotkey)
        if started:
            self._log("⌨ Горячая клавиша Ctrl+Alt+Space активна")
        else:
            self._log("⌨ Горячая клавиша недоступна (комбинация занята или нет pywin32)")

    def _on_global_hotkey(self):
        """Нажатие Ctrl+Alt+Space — принудительный вызов ассистента.

        Выполнение force_activate() выносится в фоновый daemon-поток
        (_run_bg), т.к. внутри есть аудио-переключения и голосовой вызов —
        синхронное выполнение из колбэка хоткея могло бы блокировать
        поток Flet и спровоцировать плашку «Working…».
        """
        self._log("⌨ Горячая клавиша: вызов ассистента")
        self._run_bg(self.controller.force_activate)

    def _toggle_hotkey(self, e):
        """Включает/выключает горячую клавишу из настроек (live)."""
        value = bool(e.control.value)
        # Запись settings.json — в фоновом потоке (IO не в потоке Flet)
        self._run_bg(
            self.controller.update_settings, "app_settings", "hotkey_enabled", value
        )
        if value:
            if self._hotkey is None:
                self._start_hotkey()
            else:
                self._log("⌨ Горячая клавиша включена")
        else:
            if self._hotkey is not None:
                try:
                    self._hotkey.stop()
                except Exception:
                    pass
                self._hotkey = None
            self._log("⌨ Горячая клавиша выключена")

    # ------------------------------------------------------------------
    # Сборка интерфейса
    # ------------------------------------------------------------------
    def _build_ui(self):
        page = self.page
        # В нативной строке — только имя приложения; кнопки окна рисует Windows.
        page.title = "Zeus"
        page.theme_mode = ft.ThemeMode.DARK
        page.bgcolor = C["bg"]
        # ТЗ v1.2 (п.5.3): начальный размер окна.
        # resizable=True и maximizable=True — чтобы кнопка Maximize
        # корректно разворачивало окно на весь экран.
        page.window.width = 1100
        page.window.height = 720
        page.window.resizable = True
        page.window.maximizable = True
        page.padding = 0

        # Обработчик изменения размера окна — масштабирование визуализатора
        def on_resized(e):
            try:
                if hasattr(self, "holo_image") and self.holo_image:
                    # Адаптивный размер: min(440, 30% высоты окна, 40% ширины окна)
                    new_size = min(440, page.window.height * 0.35, page.window.width * 0.4)
                    new_size = max(200, new_size)  # минимальный размер 200px
                    self.holo_image.width = new_size
                    self.holo_image.height = new_size
                    self.holo_image.update()
            except RuntimeError:
                pass  # окно закрыто

        page.on_resized = on_resized

        # Создаём контент для всех разделов
        self._build_voice_section()
        self._build_logs_section()
        self._build_settings_section()

        # Сайдбар 240px (ТЗ v1.2, п.4.1)
        self.sidebar = self._build_sidebar()

        # Область контента (отступы 30px — ТЗ п.4.2)
        self.content_area = ft.Container(
            content=self._get_section_content(0),
            bgcolor=C["bg"],
            expand=True,
            padding=32,  # ТЗ §8, §13: сетка 32px
        )

        # Заголовок контентной части (вместо AppBar — ТЗ п.4.2)
        self.page_title = ft.Text(
            SECTIONS[0]["label"],
            size=32,
            weight=ft.FontWeight.BOLD,
            color=C["text"],
        )

        # Основной макет: сайдбар + рабочая зона
        # ТЗ v1.2 §3: НЕ очищаем page.controls() (иначе серый экран при
        # первой отрисовке под новый размер). Сплэш просто скрываем, а
        # корень страницы заполняем один раз и больше не трогаем.
        # Основной макет: сайдбар + рабочая зона
        # ТЗ v1.2 §3: НЕ очищаем page.controls() (иначе серый экран при
        # первой отрисовке под новый размер). Корневой контейнер создаётся один раз,
        # а содержимое (сплэш → main_layout) просто подменяем в self._root..
        main_layout = self._compose_main_layout()
        root = getattr(self, "_root", None)
        if root is not None:
            try:
                # _root теперь Column, заменяем controls
                root.controls = [main_layout]
                root.update()
            except Exception:  # noqa: BLE001
                # Если корень ещё не привязан — добавляем напрямую
                try:
                    page.add(main_layout)
                    page.update()
                except Exception:  # noqa: BLE001
                    pass
        else:
            try:
                page.add(main_layout)
                page.update()
            except Exception:  # noqa: BLE001
                pass
        page.update()

        # Добавляем лог
        self._log("Зевс готов к работе, сэр!")
        self._log(f"Микрофон: {'доступен' if self.controller.audio.is_mic_available() else 'недоступен'}")
        if self.controller.audio.is_mic_available():
            self._log(
                f"Wake Word (Vosk): "
                f"{'активен' if self.controller.audio.wake.available else 'недоступен (в Sleep микрофон молчит)'}"
            )
        self._log(f"Ollama: {'доступна' if self.controller.chat.is_available() else 'недоступна'}")

    # ------------------------------------------------------------------
    # Навигация
    # ------------------------------------------------------------------
    def _on_navigation_change(self, e):
        """Обработчик изменения выбранного раздела в навигационной панели."""
        self._select_section(e.control.selected_index)

    def _select_section(self, index: int):
        """Переключает на указанный раздел (ТЗ v1.2, п.5.2)."""
        if not (0 <= index < len(SECTIONS)):
            return

        self.selected_index = index

        # Подсветка активного пункта сайдбара (пересоздаём с цветами темы)
        for i, btn in enumerate(self._nav_buttons):
            section = SECTIONS[i]
            new_btn = self._nav_item(section["icon"], section["label"], i == index)
            new_btn.data = i
            new_btn.on_click = lambda e: self._select_section(int(e.control.data))
            # Заменяем содержимое существующего контейнера сайдбара
            btn.content = new_btn.content
            btn.bgcolor = new_btn.bgcolor
            btn.padding = new_btn.padding
            btn.border_radius = new_btn.border_radius
            try:
                btn.update()
            except RuntimeError:
                pass

        # Обновляем заголовок контента
        if getattr(self, "page_title", None):
            self.page_title.value = SECTIONS[index]["label"]
            self.page_title.color = C["text"]
            self.page_title.update()

        # Обновляем контент
        if self.content_area:
            self.content_area.content = self._get_section_content(index)
            self.content_area.update()

    def _get_section_content(self, index: int) -> ft.Control:
        """Возвращает контент для указанного раздела."""
        if index == 0:
            return self.dashboard_content
        elif index == 1:
            return self.logs_content
        elif index == 2:
            return self.settings_content
        else:
            return ft.Container()

    def _compose_main_layout(self) -> ft.Control:
        """Собирает основной макет: сайдбар + (заголовок + контент)."""
        return ft.Row(
            [
                self.sidebar,
                ft.Column(
                    [
                        self.page_title,
                        self.content_area,
                    ],
                    expand=True,
                    spacing=0,
                ),
            ],
            expand=True,
            spacing=0,
        )

    def _build_sidebar(self):
        """Сайдбар 240px по ТЗ v1.2 (п.4.1).

        Сверху вниз: логотип + ZEUS v1.2, разделитель, навигационное меню
        (активный пункт = color_primary), Spacer, статус-бар,
        переключатель темы Sun/Moon.
        """
        self._nav_buttons = []
        for i, section in enumerate(SECTIONS):
            btn = self._nav_item(section["icon"], section["label"], i == self.selected_index)
            btn.data = i
            btn.on_click = lambda e: self._select_section(int(e.control.data))
            self._nav_buttons.append(btn)

        # Кастомный «эмиссивный» логотип: молния в круге со свечением (ТЗ п.3.1)
        logo = ft.Container(
            content=ft.Image(
                src=config.resource_path("assets/icons/logo_1024.png"),
                width=20,
                height=20,
                fit=ft.BoxFit.CONTAIN,
                repeat=ft.ImageRepeat.NO_REPEAT,
            ),
            width=34,
            height=34,
            alignment=ft.Alignment.CENTER,
            padding=4,
            border_radius=8,
            bgcolor=ft.Colors.with_opacity(0.08, C["color_primary"]),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.1, ft.Colors.WHITE)),
        )
        logo_block = ft.Row(
            [
                logo,
                ft.Row(
                    [
                        ft.Text(
                            "ZEUS", size=22, weight=ft.FontWeight.BOLD, color=C["text"]
                        ),
                        ft.Text("v1.2", size=12, color=C["text_low_emphasis"]),
                    ],
                    spacing=4,
                ),
            ],
            spacing=12,
        )

        # Статус-бар (пульсирующий зелёный маячок + текст — ТЗ §4.1)
        status_bar = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        width=10,
                        height=10,
                        border_radius=5,
                        bgcolor="#27C93F",
                        shadow=ft.BoxShadow(
                            blur_radius=10,
                            color="#27C93F",
                            spread_radius=1,
                        ),
                    ),
                    ft.Column(
                        [
                            ft.Text(
                                "Система активна",
                                size=12,
                                color=C["text_low_emphasis"],
                            ),
                            ft.Text(
                                "Vosk / Piper online",
                                size=10,
                                color=C["text_low_emphasis"],
                            ),
                        ],
                        spacing=1,
                    ),
                ],
                spacing=8,
            ),
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            bgcolor=ft.Colors.with_opacity(0.4, C["hover"]),
            border=ft.Border.all(1, C["border"]),
            border_radius=12,
        )

        # Переключатель темы (ТЗ §8): круглая кнопка 40×40
        self._theme_btn = ft.IconButton(
            icon=(
                ft.Icons.LIGHT_MODE_ROUNDED
                if self.page.theme_mode == ft.ThemeMode.DARK
                else ft.Icons.DARK_MODE_ROUNDED
            ),
            icon_size=20,
            icon_color=C["color_primary"],
            width=40,
            height=40,
            tooltip="Светлая тема" if self.page.theme_mode == ft.ThemeMode.DARK else "Тёмная тема",
            on_click=lambda _: self._toggle_theme(),
        )

        return ft.Container(
            content=ft.Column(
                [
                    logo_block,
                    ft.Divider(height=1, color=C["border"]),
                    *self._nav_buttons,
                    ft.Container(expand=True),
                    # Футер-карточка: статус + кнопка темы (ТЗ §7) — неоновый стиль
                    ft.Container(
                        content=ft.Column(
                            [status_bar, self._theme_btn],
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=4,
                        ),
                        border=ft.Border.all(1, C["card_border"]),
                        border_radius=12,
                        padding=ft.Padding.symmetric(horizontal=8, vertical=8),
                    ),
                ],
                spacing=10,
            ),
            width=280,
            padding=ft.Padding.symmetric(vertical=16, horizontal=12),
            bgcolor=C["sidebar"],
            border=ft.Border.only(right=ft.BorderSide(1, C["border"])),
            shadow=ft.BoxShadow(
                blur_radius=12,
                color=ft.Colors.with_opacity(0.2, "#000000" if self.page.theme_mode != ft.ThemeMode.LIGHT else "#94A3B8"),
                spread_radius=0,
            ),
        )

    def _nav_item(self, icon, label, selected) -> ft.Container:
        """Пункт навигации сайдбара (ТЗ v1.2 финал, §3).

        Неактивные: иконки/текст белые (тёмная) / темные (светлая).
        Активные: подложка color_primary.
        """
        is_dark = self.page.theme_mode != ft.ThemeMode.LIGHT
        if selected:
            # Активный (ТЗ §5): dark #2563EB / light #BFDBFE
            icon_color = ft.Colors.WHITE if is_dark else "#2563EB"
            text_color = ft.Colors.WHITE if is_dark else "#2563EB"
            bg_color = "#2563EB" if is_dark else "#BFDBFE"
        else:
            icon_color = ft.Colors.WHITE if is_dark else "#374151"
            text_color = ft.Colors.WHITE if is_dark else "#374151"
            bg_color = ft.Colors.TRANSPARENT

        return ft.Container(
            height=48,
            padding=ft.Padding.symmetric(horizontal=16, vertical=0),
            border_radius=10,
            bgcolor=bg_color,
            # Hover: лёгкий фон у неактивных пунктов (ТЗ §6, §27)
            on_hover=(
                (lambda e: self._nav_hover(e, False)) if not selected else None
            ),
            content=ft.Row(
                [
                    ft.Icon(icon, size=21, color=icon_color),
                    ft.Text(
                        label,
                        size=14,
                        color=text_color,
                        weight=(
                            ft.FontWeight.W_500 if selected else ft.FontWeight.NORMAL
                        ),
                    ),
                ],
                spacing=12,
            ),
        )

    def _nav_hover(self, e, selected: bool):
        """Hover-подсветка неактивного пункта навигации (ТЗ §27)."""
        c = e.control
        hovered = e.data == "true"
        c.bgcolor = (
            ft.Colors.with_opacity(0.15, "#2563EB") if hovered else ft.Colors.TRANSPARENT
        )
        try:
            c.update()
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Раздел 0: Голосовой помощник
    # ------------------------------------------------------------------
    def _build_voice_section(self):
        # Статус: «Соединение с Zeus: Зевс готов к работе» (ТЗ финал §2)
        self.voice_status = ft.Text(
            "Зевс готов к работе",
            size=15,
            weight=ft.FontWeight.BOLD,
            color=C["electric"],
        )

        # --- Индикатор состояния (неоновый зелёный = ожидание «Зевс», синий = команда) ---
        # Внутренний LED — его цвет меняет _do_state (внешний просто даёт ореол)
        self.status_dot_led = ft.Container(
            width=14,
            height=14,
            border_radius=7,
            bgcolor="#27C93F",  # приглушённый зелёный
            shadow=ft.BoxShadow(blur_radius=8, color="#27C93F", spread_radius=1),
        )
        self.status_dot = ft.Container(
            content=self.status_dot_led,
            width=18,
            height=18,
            alignment=ft.Alignment.CENTER,
            shadow=ft.BoxShadow(blur_radius=6, color="#27C93F", spread_radius=0),
        )
        # Совместимость: подпись под светодиодом больше не в макете,
        # но статус-логика (_do_state) продолжает её обновлять.
        self.status_dot_label = ft.Text(
            "Ожидание «Зевс»",
            size=12,
            color=C["text_dim"],
            visible=False,
        )

        # --- Голографический визуализатор (ТЗ v1.2: центральный техно-круг) ---
        holo_viz = self._build_holo_visualizer()

        # Терминал активности (ТЗ п.4.2): цветные spans, mono, expand
        self.log_text = ft.Text(
            spans=[],
            selectable=True,
            font_family="Consolas, monospace",
        )
        log_container = ft.Container(
            content=ft.Column(
                [self.log_text],
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            bgcolor=C["terminal_background"],
            border_radius=12,
            border=ft.Border.all(1, ft.Colors.with_opacity(0.1, ft.Colors.WHITE)),
            padding=20,
            expand=True,
        )

        # Кнопки управления — круглые Icon Buttons 58×58 (плоский дизайн)
        # Sleep/Power button
        self.sleep_button = ft.IconButton(
            icon=ft.Icons.POWER_SETTINGS_NEW_ROUNDED,
            icon_size=26,
            icon_color=C["electric"],
            width=58,
            height=58,
            tooltip="Отключить ZEUS",
            style=ft.ButtonStyle(
                shape=ft.CircleBorder(),
                side=ft.BorderSide(1, ft.Colors.with_opacity(0.2, ft.Colors.WHITE)),
                bgcolor=ft.Colors.with_opacity(0.08, C["electric"]),
            ),
            on_click=self._toggle_sleep,
        )

        # Быстрый доступ к настройкам микрофона/звука.
        self.listen_button = ft.IconButton(
            # SETTINGS_VOICE есть в Flet 0.85; MIC_SETTINGS отсутствует
            # в наборе Material Icons этой версии.
            icon=ft.Icons.SETTINGS_VOICE_ROUNDED,
            icon_size=26,
            icon_color=C["electric"],
            width=54,
            height=54,
            tooltip="Настройки микрофона",
            style=ft.ButtonStyle(
                shape=ft.CircleBorder(),
                side=ft.BorderSide(1, ft.Colors.with_opacity(0.2, ft.Colors.WHITE)),
                bgcolor=ft.Colors.with_opacity(0.08, C["electric"]),
            ),
            on_click=lambda _: self._select_section(2),
        )
        self.voice_switch = ft.Switch(
            label="Автоматическое прослушивание",
            value=True,
            active_color=C["electric"],
            on_change=self._toggle_auto_listen,
        )
        # Информационная иконка (ТЗ v1.2 §Б): подсказка о режиме
        auto_listen_info = ft.Icon(
            ft.Icons.INFO_OUTLINE,
            size=14,
            color=C["text_dim"],
            tooltip="Постоянное распознавание Wake Word «Зевс» без ручного запуска",
        )

        # Кнопка очистки терминала (ТЗ §16): маленькая IconButton 32×32
        self.clear_log_btn = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            icon_size=16,
            icon_color=C["text_dim"],
            width=32,
            height=32,
            tooltip="Очистить терминал",
            on_click=self._clear_log,
        )

        # Статус-карточка (ТЗ §9): растягивается по ширине при максимизации
        status_card = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [self.status_dot, ft.Text("Соединение с Zeus", size=14, color=C["text"])],
                        spacing=8,
                    ),
                    self.voice_status,
                    ft.Row([self.voice_switch, auto_listen_info], spacing=4),
                ],
                spacing=6,
            ),
            expand=True,
            bgcolor=C["card"],
            border=ft.Border.all(1, ft.Colors.with_opacity(0.1, ft.Colors.WHITE)),
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        )

        # Заголовок терминала: иконка + текст + корзина (ТЗ финал §2)
        terminal_header = ft.Row(
            [
                ft.Icon(ft.Icons.TERMINAL_ROUNDED, size=18, color=C["electric"]),
                ft.Text(
                    "Терминал активности",
                    size=16,
                    weight=ft.FontWeight.BOLD,
                    color=C["text"],
                ),
                ft.Container(expand=True),
                self.clear_log_btn,
            ],
            spacing=8,
        )

        controls = ft.Container(
            content=ft.Column(
                [
                    # Верхняя панель: статус-карточка слева, кнопки справа,
                    # все элементы на одной вертикальной оси (ТЗ финал §2)
                    ft.Row(
                        [status_card, ft.Container(expand=True), self.sleep_button, self.listen_button],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    holo_viz,
                    ft.Container(height=4),
                    terminal_header,
                    log_container,
                ],
                spacing=12,
                expand=True,
                # ТЗ (дополнение) §7: дети Column растягиваются по ширине —
                # терминал занимает всю рабочую область, без «узкого бокса».
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            expand=True,
        )

        self.dashboard_content = controls
        self.voice_content = controls  # алиас для совместимости
        # ТЗ финал §2: бесшовное слияние с фоном рабочей зоны —
        # никаких подложек и рамок вокруг секции главного экрана.
        self.voice_content.bgcolor = None
        self.voice_content.border_radius = None
        self.voice_content.border = None

        # Анимация визуализатора запускается один раз в _setup_controller.

    def _module_card(
        self, icon, title: str, subtitle: str, glow: str
    ) -> ft.Container:
        """Карточка модуля (ТЗ п.4.2): иконка со свечением + заголовок + описание."""
        return ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Icon(icon, size=26, color=glow),
                        width=52,
                        height=52,
                        alignment=ft.Alignment.CENTER,
                        bgcolor=C["hover"],
                        border_radius=12,
                        shadow=ft.BoxShadow(
                            blur_radius=14, color=glow, spread_radius=0
                        ),
                    ),
                    ft.Column(
                        [
                            ft.Text(
                                title,
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=C["text"],
                            ),
                            ft.Text(
                                subtitle,
                                size=12,
                                color=C["text_low_emphasis"],
                            ),
                        ],
                        spacing=2,
                    ),
                ],
                spacing=14,
            ),
            bgcolor=C["background_surface"],
            border_radius=14,
            border=ft.Border.all(1, C["color_primary"]),
            padding=16,
            expand=True,
        )

    # ------------------------------------------------------------------
    # Раздел 1: Системные логи (ТЗ v1.2)
    # ------------------------------------------------------------------
    def _build_logs_section(self):
        """Полный системный терминал: чёрный фон, зелёный mono-текст."""
        self.sys_log_text = ft.Text(
            value="",
            size=12,
            color=C["terminal_text"],
            selectable=True,
            font_family="Consolas, monospace",
        )
        self.logs_content = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.TERMINAL_ROUNDED, size=18, color=C["electric"]),
                            ft.Text(
                                "Терминал активности",
                                size=16,
                                weight=ft.FontWeight.BOLD,
                                color=C["text"],
                            ),
                            ft.Container(expand=True),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_size=18,
                                icon_color=C["text_dim"],
                                tooltip="Очистить терминал",
                                on_click=self._clear_system_log,
                            ),
                        ],
                        spacing=8,
                    ),
                    ft.Container(
                        content=ft.Column(
                            [self.sys_log_text],
                            scroll=ft.ScrollMode.AUTO,
                            expand=True,
                        ),
                        bgcolor=C["terminal_background"],
                        border_radius=12,
                        border=ft.Border.all(1, C["border"]),
                        padding=20,
                        expand=True,
                    ),
                ],
                spacing=8,
                expand=True,
            ),
            bgcolor=C["background_surface"],
            border_radius=16,
            border=ft.Border.all(1, C["border"]),
            padding=20,
            expand=True,
        )

    # ------------------------------------------------------------------
    # Переключатель темы (ТЗ v1.2, п.5.1)
    # ------------------------------------------------------------------
    def _toggle_theme(self):
        """Мгновенно переключает Design Tokens и перестраивает UI."""
        new_key = "light" if self.page.theme_mode == ft.ThemeMode.DARK else "dark"
        global C
        C = dict(PALETTES[new_key])
        self.page.theme_mode = (
            ft.ThemeMode.LIGHT if new_key == "light" else ft.ThemeMode.DARK
        )
        self._rebuild_all()
        self.page.update()

    # ------------------------------------------------------------------
    # Раздел 1: Настройки
    # ------------------------------------------------------------------
    def _build_settings_section(self):
        """Создаёт полноценный раздел настроек с тремя группами карточек."""
        settings = self.controller.get_settings()
        ai = settings.get("ai_settings", {})
        voice = settings.get("voice_settings", {})
        app_s = settings.get("app_settings", {})

        # -- Состояние UI (виджеты, которые будем менять) --
        self._settings_widgets = {}

        # ---- Карточка 1: AI / Модель ----
        model_dd = ft.Dropdown(
            label="Модель Ollama",
            value=ai.get("model", "llama3.1"),
            options=[ft.dropdown.Option("llama3.1")],
            width=280,
            hint_text="Выберите модель",
            text_size=13,
            color=C["text"],
            bgcolor=C["surface"],
            border_color=C["border"],
            on_select=lambda e: self._on_setting_change("ai_settings", "model", e.control.value),
        )
        self._settings_widgets["model_dd"] = model_dd

        temp_slider = ft.Slider(
            label="{value}",
            min=0.0,
            max=2.0,
            divisions=20,
            value=ai.get("temperature", 0.7),
            width=280,
            active_color=C["electric"],
            inactive_color=C["surface_2"],
            on_change=lambda e: self._on_setting_change("ai_settings", "temperature", round(e.control.value, 2)),
        )
        self._settings_widgets["temp_slider"] = temp_slider

        temp_label = ft.Text(
            f"Температура: {ai.get('temperature', 0.7):.2f}",
            size=12,
            color=C["text_dim"],
        )
        self._settings_widgets["temp_label"] = temp_label

        sys_prompt = ft.TextField(
            label="Системный промпт",
            value=ai.get("system_prompt", ""),
            multiline=True,
            min_lines=2,
            max_lines=6,
            width=500,
            text_size=13,
            color=C["text"],
            bgcolor=C["surface"],
            border_color=C["border"],
            on_change=lambda e: self._on_setting_change("ai_settings", "system_prompt", e.control.value),
        )
        self._settings_widgets["sys_prompt"] = sys_prompt

        ai_card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.ROCKET_LAUNCH_OUTLINED, color=C["electric"], size=22),
                    ft.Text("Модель и генерация", size=16, weight=ft.FontWeight.BOLD, color=C["text"]),
                ], spacing=8),
                ft.Divider(height=1, color=C["border"]),
                ft.Row([model_dd], spacing=12),
                ft.Row([temp_label], spacing=12),
                ft.Row([temp_slider], spacing=12),
                ft.Row([sys_prompt], spacing=12),
            ], spacing=12, scroll=ft.ScrollMode.AUTO),
            bgcolor=C["surface"],
            border_radius=16,
            border=ft.Border.all(1, C["border"]),
            padding=20,
            expand=True,
        )

        # ---- Карточка 2: Голос и микрофон ----
        voice_enabled = ft.Switch(
            label="Голосовое управление",
            value=voice.get("enabled", True),
            active_color=C["electric"],
            on_change=lambda e: self._on_setting_change("voice_settings", "enabled", e.control.value),
        )
        self._settings_widgets["voice_enabled"] = voice_enabled

        tts_enabled = ft.Switch(
            label="Озвучивание ответов (TTS)",
            value=voice.get("tts_enabled", True),
            active_color=C["electric"],
            on_change=lambda e: self._on_setting_change("voice_settings", "tts_enabled", e.control.value),
        )
        self._settings_widgets["tts_enabled"] = tts_enabled

        lang_dd = ft.Dropdown(
            label="Язык распознавания",
            value=voice.get("language", "ru-RU"),
            options=[
                ft.dropdown.Option("ru-RU", "Русский"),
                ft.dropdown.Option("en-US", "English"),
                ft.dropdown.Option("kk-KZ", "Қазақ"),
            ],
            width=220,
            text_size=13,
            color=C["text"],
            bgcolor=C["surface"],
            border_color=C["border"],
            on_select=lambda e: self._on_setting_change("voice_settings", "language", e.control.value),
        )
        self._settings_widgets["lang_dd"] = lang_dd

        voice_card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.MIC_OUTLINED, color=C["electric"], size=22),
                    ft.Text("Голос и микрофон", size=16, weight=ft.FontWeight.BOLD, color=C["text"]),
                ], spacing=8),
                ft.Divider(height=1, color=C["border"]),
                ft.Row([voice_enabled], spacing=12),
                ft.Row([tts_enabled], spacing=12),
                ft.Row([lang_dd], spacing=12),
                ft.Row([
                    ft.Text("Микрофон:", size=13, color=C["text_dim"]),
                    ft.Text(
                        self.controller.audio._mic_device_name or "По умолчанию",
                        size=13, color=C["text"],
                    ),
                ], spacing=8),
            ], spacing=12),
            bgcolor=C["surface"],
            border_radius=16,
            border=ft.Border.all(1, C["border"]),
            padding=20,
            expand=True,
        )

        # ---- Карточка 3: Интерфейс и система ----
        theme_dd = ft.Dropdown(
            label="Тема оформления",
            value=app_s.get("theme", "dark"),
            options=[
                ft.dropdown.Option("dark", "Тёмная"),
                ft.dropdown.Option("light", "Светлая"),
            ],
            width=220,
            text_size=13,
            color=C["text"],
            bgcolor=C["surface"],
            border_color=C["border"],
            on_select=lambda e: self._on_setting_change("app_settings", "theme", e.control.value),
        )
        self._settings_widgets["theme_dd"] = theme_dd

        autostart_sw = ft.Switch(
            label="Автозапуск при старте Windows",
            value=app_s.get("autostart", False),
            active_color=C["electric"],
            on_change=lambda e: self._on_setting_change("app_settings", "autostart", e.control.value),
        )
        self._settings_widgets["autostart_sw"] = autostart_sw

        hotkey_sw = ft.Switch(
            label="Горячая клавиша Ctrl+Alt+Space",
            value=app_s.get("hotkey_enabled", True),
            active_color=C["electric"],
            on_change=self._toggle_hotkey,
        )
        self._settings_widgets["hotkey_sw"] = hotkey_sw

        app_card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.APPS_OUTLINED, color=C["electric"], size=22),
                    ft.Text("Интерфейс и система", size=16, weight=ft.FontWeight.BOLD, color=C["text"]),
                ], spacing=8),
                ft.Divider(height=1, color=C["border"]),
                ft.Row([theme_dd], spacing=12),
                ft.Row([autostart_sw], spacing=12),
                ft.Row([hotkey_sw], spacing=12),
            ], spacing=12),
            bgcolor=C["surface"],
            border_radius=16,
            border=ft.Border.all(1, C["border"]),
            padding=20,
            expand=True,
        )

        # ---- Кнопки управления ----
        save_btn = ft.FilledButton(
            content=ft.Text("Сохранить всё"),
            icon=ft.Icons.SAVE,
            on_click=self._on_save_settings,
        )

        reset_btn = ft.OutlinedButton(
            content=ft.Text("Сбросить настройки"),
            icon=ft.Icons.RESTORE,
            on_click=self._on_reset_settings,
        )

        # Оповещение о сохранении
        self._settings_save_notice = ft.Text(
            "",
            size=12,
            color=C["electric"],
            visible=False,
        )

        # ---- Вспомогательные функции загрузки моделей ----
        self._load_models_in_background()

        # Сборка всего раздела
        self.settings_content = ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        "⚙ Настройки",
                        size=22,
                        weight=ft.FontWeight.BOLD,
                        color=C["electric"],
                    ),
                    ft.Divider(height=1, color=C["border"]),
                    # Строка из двух колонок: слева AI, справа голос
                    ft.Row(
                        [ai_card, voice_card],
                        spacing=16,
                        expand=True,
                    ),
                    # Нижний ряд: интерфейс
                    ft.Row(
                        [app_card],
                        expand=True,
                    ),
                    ft.Divider(height=1, color=C["border"]),
                    ft.Row(
                        [save_btn, reset_btn, self._settings_save_notice],
                        spacing=16,
                        alignment=ft.MainAxisAlignment.START,
                    ),
                ],
                spacing=16,
                expand=True,
                scroll=ft.ScrollMode.AUTO,
            ),
            padding=32,
            expand=True,
        )

    def _on_setting_change(self, section: str, key: str, value: Any):
        """Немедленно обновляет настройку в контроллере при изменении.

        Запись settings.json и применение тяжёлых параметров выполняются
        в фоновом потоке: обновление настроек не блокирует поток Flet
        (например, тумблер «Голос включён» вызывает set_active → stop_
        listening с join до 3 с).
        """
        self._run_bg(self.controller.update_settings, section, key, value)

        # Обновляем связанные UI-элементы
        if section == "ai_settings" and key == "temperature":
            lbl = self._settings_widgets.get("temp_label")
            if lbl:
                lbl.value = f"Температура: {float(value):.2f}"
                lbl.update()
        if section == "app_settings" and key == "theme":
            theme_key = "light" if value == "light" else "dark"
            # Обновляем глобальную палитру
            global C
            C = dict(PALETTES[theme_key])
            # Меняем тему Flet
            self.page.theme_mode = ft.ThemeMode.LIGHT if value == "light" else ft.ThemeMode.DARK
            # Перестраиваем UI с новыми цветами
            self._rebuild_all()
            self.page.update()

        # Показываем уведомление
        self._show_save_notice("✅ Настройка применена")

    def _on_save_settings(self, e=None):
        """Сохраняет все настройки в JSON (запись — в фоновом потоке)."""
        def worker():
            saved = config.save_settings(self.controller.get_settings())
            try:
                if saved:
                    self._show_save_notice("✅ Все настройки сохранены")
                else:
                    self._show_save_notice("⚠️ Ошибка сохранения", error=True)
            except RuntimeError:
                pass

        self._run_bg(worker)

    def _on_reset_settings(self, e=None):
        """Сбрасывает настройки до стандартных (IO — в фоновом потоке)."""
        def worker():
            self.controller.reset_settings()
            # Обновляем UI после сброса
            try:
                self._build_settings_section()
                if self.content_area and self.selected_index == 1:
                    self.content_area.content = self.settings_content
                    self.content_area.update()
                    self._show_save_notice("↺ Настройки сброшены до заводских")
            except RuntimeError:
                pass

        self._run_bg(worker)

    def _show_save_notice(self, text: str, error: bool = False):
        """Показывает временное уведомление о сохранении (через zeus-ui)."""
        self._post_ui(lambda: self._do_save_notice(text, error))

    def _do_save_notice(self, text: str, error: bool = False):
        notice = getattr(self, "_settings_save_notice", None)
        if notice is None:
            return
        try:
            notice.value = text
            notice.color = C["electric"] if not error else "#FF5252"
            notice.visible = True
            notice.update()
        except RuntimeError:
            pass

    def _load_models_in_background(self):
        """Загружает список моделей Ollama в фоне."""
        def _load():
            try:
                models = self.controller.get_available_models()
                dd = self._settings_widgets.get("model_dd")
                if dd is None:
                    return

                def _apply_models(dd=dd, models=models):
                    try:
                        dd.options = [ft.dropdown.Option(m) for m in models]
                        # Если текущая модель не в списке — подставляем первую
                        if dd.value not in models and models:
                            dd.value = models[0]
                        dd.update()
                    except RuntimeError:
                        pass

                # Контролы трогаем только в zeus-ui
                self._post_ui(_apply_models)
            except Exception:
                pass

        threading.Thread(target=_load, daemon=True).start()

    def _build_holo_visualizer(self) -> ft.Container:
        """Создаёт контейнер с голографическим визуализатором активности (ТЗ v1.2).

        Единственный центральный визуализатор — assets/animations/hologram.webp.
        Для светлой темы используется hologram_light.webp (тёмные линии на белом фоне).
        В покое фиксированный размер ~250px; при речи ассистента масштаб плавно
        увеличивается (scale 1.0 -> 1.25) через animate_scale.
        """
        is_light = self.page.theme_mode == ft.ThemeMode.LIGHT
        file_name = "hologram_light.webp" if is_light else "hologram.webp"
        webp = os.path.join("assets", "animations", file_name)
        img_path = config.resource_path(webp)

        self.holo_image = ft.Image(
            src=img_path,
            width=250,
            height=250,
            fit=ft.BoxFit.CONTAIN,
            opacity=1.0,
            repeat=ft.ImageRepeat.NO_REPEAT,
        )

        # Анимированный контейнер: scale плавно меняется при речи (ТЗ v1.2 §3)
        self.holo_container = ft.Container(
            content=self.holo_image,
            alignment=ft.Alignment.CENTER,
            scale=1.0,
            animate_scale=ft.Animation(300, ft.AnimationCurve.EASE_OUT),
        )

        # Внешний контейнер без фона, рамки и тени — только центрирование
        return ft.Container(
            content=self.holo_container,
            alignment=ft.Alignment.CENTER,
            expand=True,
            margin=ft.Margin.symmetric(vertical=8),
        )

    def update_assistant_state(self, is_speaking: bool):
        """Масштабирует голограмму при речи ассистента (ТЗ v1.2 §3).

        Триггер — audio_engine.is_speaking (через ctrl.audio.on_speaking_changed).
        При речи контейнер плавно увеличивается (scale 1.0 -> 1.25, ~250->312px),
        в покое возвращается к исходному размеру. Потокобезопасно: через _post_ui.
        """
        if getattr(self, "holo_container", None) is None:
            return

        def _apply():
            try:
                self.holo_container.scale = 1.25 if is_speaking else 1.0
                self.holo_container.update()
            except RuntimeError:
                pass  # страница закрыта

        self._post_ui(_apply)



    # ------------------------------------------------------------------
    # Перестройка UI при смене темы
    # ------------------------------------------------------------------
    def _rebuild_all(self):
        """Горячее обновление темы БЕЗ перезагрузки страницы (ТЗ v1.2 §3).

        Обновляем свойства уже существующих контролов в дереве Flet
        (bgcolor/содержимое), НЕ вызывая page.clean() / controls.clear()
        — это исключает артефакты «серого экрана» при смене темы.
        """
        current_index = self.selected_index

        # Фон страницы и корневого контейнера — обновляем СУЩЕСТВУЮЩИЕ (не
        # пересоздавая). Это гарантирует, что всё окно красится в актуальный фон темы.
        try:
            self.page.bgcolor = C["bg"]
        except Exception:  # noqa: BLE001
            pass
        root = getattr(self, "_root", None)
        if root is not None:
            try:
                root.bgcolor = C["bg"]
            except Exception:  # noqa: BLE001
                pass

        # Пересобираем содержимое секций (новые контролы с новыми цветами)
        self._build_voice_section()
        self._build_logs_section()
        self._build_settings_section()

        # --- Сайдбар: обновляем СУЩЕСТВУЮЩИЙ контейнер в дереве ---
        # Защитная обёртка: если вдруг в пересборке сайдбара вылетит любое
        # исключение (AttributeError и т.п.) — тема всё равно применится на лету,
        # а страница не останется «серой» (page.update() ниже выполнится всегда`.
        _sb_ok = False
        try:
            new_sidebar = self._build_sidebar()
            _sb_ok = True
        except Exception:  # noqa: BLE001
            import traceback
            print("[ZeusUI][toggle_theme] Ошибка пересборки сайдбара (тема всё равно применится)")
            traceback.print_exc()
            _sb_ok = False
        if _sb_ok and self.sidebar is not None:
            try:
                self.sidebar.bgcolor = new_sidebar.bgcolor
                self.sidebar.border = new_sidebar.border
                self.sidebar.padding = new_sidebar.padding
                # Подменяем содержимое (nav/статус/тема — все с новыми цветами`
                self.sidebar.content = new_sidebar.content
                try:
                    self.sidebar.update()
                except RuntimeError:
                    pass
            except Exception:  # noqa: BLE001
                pass

        # --- Заголовок: обновляем свойства существующего Text ---
        if getattr(self, "page_title", None):
            self.page_title.value = SECTIONS[current_index]["label"]
            self.page_title.color = C["text"]
            try:
                self.page_title.update()
            except RuntimeError:
                pass

        # --- Контентная область: подменяем содержимое ---
        if self.content_area is not None:
            self.content_area.bgcolor = C["bg"]
            self.content_area.content = self._get_section_content(current_index)
            self.content_area.padding = 30
            try:
                self.content_area.update()
            except RuntimeError:
                pass

        # Синхронизируем состояния кнопок после обновления
        self._do_state(self.controller.state)

        self.page.update()

    # ------------------------------------------------------------------
    # Настройка контроллера
    # ------------------------------------------------------------------
    def _setup_controller(self):
        ctrl = self.controller

        # Подключаем коллбэки
        ctrl.on_system_command = self._on_system_command
        ctrl.on_chat_start = self._on_chat_start
        ctrl.on_chat_token = self._on_chat_token
        ctrl.on_chat_complete = self._on_chat_complete
        ctrl.on_chat_error = self._on_chat_error
        ctrl.on_status = self._on_status
        ctrl.on_voice_command = self._on_voice_command
        ctrl.on_state = self._on_state

        # Переключение slow/fast GIF голограммы при изменении состояния TTS
        if hasattr(ctrl, "audio"):
            ctrl.audio.on_speaking_changed = self.update_assistant_state


    # ------------------------------------------------------------------
    # Коллбэки UI
    # ------------------------------------------------------------------
    def _on_status(self, text: str):
        """Обновляет статус голосового помощника (через zeus-ui)."""
        self._post_ui(lambda: self._do_status(text))

    def _do_status(self, text: str):
        try:
            if self.voice_status:
                self.voice_status.value = text
                self.voice_status.update()
        except RuntimeError:
            pass

    def _log(self, text: str):
        """Добавляет запись в лог голосового помощника (через zeus-ui)."""
        self._post_ui(lambda: self._do_log(text))

    def _clear_log(self, e=None):
        """Очищает терминал активности (ТЗ v1.2 §Б: кнопка-корзина)."""
        def _do_clear():
            try:
                self.log_text.spans = []
                self.log_text.value = None
                self.log_text.update()
            except Exception:  # noqa: BLE001
                pass
        self._post_ui(_do_clear)

    def _clear_system_log(self, e=None):
        """Очищает полный терминал на странице «Системные логи»."""
        def _do_clear():
            try:
                self.sys_log_text.value = ""
                self.sys_log_text.update()
            except (AttributeError, RuntimeError):
                pass
        self._post_ui(_do_clear)

    def _do_log(self, text: str):
        """Добавляет строку в терминал с цветовой дифференциацией (ТЗ §4.2).

        Временная метка — приглушённая, текст — цвет по типу события:
        ✅ успех (зелёный), ❌ ошибка (красный), 🤔/🎤 LLM/речь (жёлтый/синий),
        остальное — терминальный зелёный mono.
        """
        if self.log_text is None:
            return
        try:
            from datetime import datetime

            timestamp = datetime.now().strftime("%H:%M:%S")
            line = f"[{timestamp}] {text}"

            # Цвет строки по типу события
            if text.startswith(("✅", "[1]", "[2]", "[3]")) or "✅" in text[:4]:
                color = "#4ADE80"
            elif text.startswith("❌") or "⚠️" in text[:4] or "Ошибка" in text:
                color = "#F87171"
            elif text.startswith("🤔"):
                color = "#FACC15"  # Warning (ТЗ §14)
            elif text.startswith("🎤"):
                color = "#60A5FA"
            else:
                color = C["terminal_text"]

            from datetime import datetime as _dt  # noqa: F401
            new_span = ft.TextSpan(
                f"{line}\n",
                ft.TextStyle(color=color, font_family="Consolas, monospace", size=12),
            )
            prev = self.log_text.spans or []
            spans = [*prev, new_span]
            # Ограничиваем историю терминала (память/транспорт Flet)
            if len(spans) > 200:
                spans = spans[-200:]
            self.log_text.spans = spans
            self.log_text.value = None
            self.log_text.update()

            # Дублируем запись в «Системные логи» (ТЗ v1.2)
            sys_log = getattr(self, "sys_log_text", None)
            if sys_log is not None:
                lines = (sys_log.value or "").splitlines()
                lines.append(line)
                # Полный журнал также ограничен: не раздувает память и
                # транспорт Flet во время длительной работы ассистента.
                sys_log.value = "\n".join(lines[-200:]) + "\n"
                try:
                    sys_log.update()
                except RuntimeError:
                    pass
        except RuntimeError:
            pass

    def _on_system_command(self, result: dict[str, Any]):
        """Обрабатывает результат системной команды."""
        success = result.get("success", False)
        message = result.get("message", "")
        action = result.get("action", "")

        self._stats_cmds += 1
        emoji = "✅" if success else "❌"
        self._log(
            f"{emoji} [{self._stats_cmds}] Системная команда ({action}): {message}"
        )
        self._on_status(message)

    def _on_voice_command(self, text: str):
        """Обрабатывает голосовую команду."""
        self._log(f"🎤 Распознано: {text}")

    def _on_state(self, state: str):
        """Реагирует на смену состояния FSM (обновления — через zeus-ui)."""
        self._post_ui(lambda: self._do_state(state))

    def _do_state(self, state: str):
        """Реагирует на смену состояния конечного автомата контроллера.

        Кнопки «Слушать» и «Отключиться» выступают информационными
        индикаторами:
          - зелёный — ожидание ключевого слова «Зевс» (IDLE, активен);
          - синий   — распознавание команды (LISTENING);
          - оранжевый — выполнение (PROCESSING);
          - серый   — спящий режим (микрофон молчит).
        """
        try:
            btn = self.listen_button
            dot = self.status_dot
            dot_label = self.status_dot_label

            if dot is not None:
                # Внешний контейнер-ореол, внутренний LED с цветом
                led = getattr(self, "status_dot_led", None) or dot
                if state == "LISTENING":
                    led.bgcolor = "#1FB6FF"
                    led.shadow = ft.BoxShadow(blur_radius=6, color="#1FB6FF", spread_radius=0)
                    if dot_label is not None:
                        dot_label.value = "Слушаю команду..."
                        dot_label.color = "#1FB6FF"
                elif state == "PROCESSING":
                    led.bgcolor = "#F5A623"
                    led.shadow = ft.BoxShadow(blur_radius=6, color="#F5A623", spread_radius=0)
                    if dot_label is not None:
                        dot_label.value = "Выполняю..."
                        dot_label.color = "#F5A623"
                else:  # IDLE
                    color = "#27C93F" if self.controller.is_active else "#7A7A8A"
                    led.bgcolor = color
                    led.shadow = ft.BoxShadow(blur_radius=6, color=color, spread_radius=0)
                    if dot_label is not None:
                        dot_label.value = (
                            "Ожидание «Зевс»" if self.controller.is_active else "Спящий режим"
                        )
                        dot_label.color = C["text_dim"]
                led.update()
                dot.update()
                if dot_label is not None:
                    dot_label.update()

            if btn is not None:
                # Иконочная кнопка (ТЗ финал): цвет иконки + подложка + подсказка
                if state == "LISTENING":
                    btn.icon_color = "#1FB6FF"
                    btn.tooltip = "Слушаю команду..."
                    self._on_status("Слушаю команду...")
                elif state == "PROCESSING":
                    btn.icon_color = "#F5A623"
                    btn.tooltip = "Выполняю..."
                    self._on_status("Выполняю команду...")
                else:  # IDLE
                    btn.icon_color = (
                        "#27C93F" if self.controller.is_active else C["electric"]
                    )
                    btn.tooltip = "Слушать"
                    if self.controller.is_active:
                        self._on_status("Ожидание «Зевс»...")
                    else:
                        self._on_status("Спящий режим. Скажите «Зевс, включись».")
                btn.update()

            # Кнопка «Отключиться» — зелёная при активном прослушивании, синяя в Sleep
            if self.sleep_button:
                self.sleep_button.icon_color = (
                    "#27C93F" if self.controller.is_active else C["electric"]
                )
                self.sleep_button.tooltip = (
                    "Отключиться" if self.controller.is_active else "Включиться"
                )
                self.sleep_button.update()
        except (RuntimeError, Exception):
            pass

    def _on_chat_start(self, text: str):
        """Обрабатывает начало генерации LLM."""
        self._set_buttons(False)
        self._log(f"🤔 Запрос к LLM: {text}")
        self._on_status("Зевс думает...")

    def _on_chat_token(self, token: str):
        """Обрабатывает новый токен от LLM (текстовый чат отключён — токен игнорируется)."""
        pass

    def _on_chat_complete(self, full_text: str):
        """Обрабатывает завершение генерации LLM."""
        self._set_buttons(True)
        self._log(f"✅ Ответ LLM получен ({len(full_text)} символов)")
        self._on_status("Готов к работе")

    def _on_chat_error(self, error: str):
        """Обрабатывает ошибку LLM."""
        self._set_buttons(True)
        self._log(f"⚠️ Ошибка LLM: {error}")
        self._on_status(f"Ошибка: {error}")

    # ------------------------------------------------------------------
    # Действия пользователя
    # ------------------------------------------------------------------
    def _on_listen_once(self, e=None):
        """Однократное прослушивание микрофона."""
        self._on_status("Слушаю...")
        self._log("🎤 Запись речи...")
        self._set_buttons(False)

        def _listen():
            text = self.controller.listen_once()
            if text:
                self._log(f"🎤 Распознано: {text}")
                # Отправляем как команду
                self.controller.process_text(text, source="voice")
            else:
                self._log("🎤 Речь не распознана")
                self._on_status("Не удалось распознать речь")
                self._set_buttons(True)

        threading.Thread(target=_listen, daemon=True).start()

    def _toggle_sleep(self, e=None):
        """Переключает спящий режим.

        toggle_active → set_active → audio.stop_listening() содержит
        join до 3 с: выполняем его в фоновом потоке, чтобы поток Flet
        оставался отзывчивым (без «Working…»).
        """
        # Целевое состояние вычисляем сразу (для мгновенного UI-отклика),
        # а само переключение (с stop_listening/join до 3 с) — в фоне.
        target = not self.controller.is_active
        self._run_bg(self.controller.set_active, target)

        status_text = (
            "активирован" if target else "отключён (спящий режим)"
        )
        self._log(f"🌙 Спящий режим: {status_text}")

        if self.sleep_button:
            self.sleep_button.icon = (
                ft.Icons.POWER_SETTINGS_NEW_ROUNDED if target else ft.Icons.POWER_OFF
            )
            self.sleep_button.tooltip = "Отключиться" if target else "Включиться"
            self.sleep_button.icon_color = "#27C93F" if target else C["electric"]
            self.sleep_button.bgcolor = C["hover"]
            self._post_ui(self.sleep_button.update)

    def _toggle_auto_listen(self, e=None):
        """Включает/выключает автоматическое прослушивание (в фоне)."""
        if self.voice_switch and self.voice_switch.value:
            self._log("🎤 Автоматическое прослушивание включено")
            self._run_bg(self.controller.start_voice)
        else:
            self._log("🎤 Автоматическое прослушивание выключено")
            self._run_bg(self.controller.stop_voice)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------
    def _run_bg(self, fn, *args, **kwargs):
        """Запускает функцию в фоновом daemon-потоке.

        Используется всеми обработчиками Flet для операций с файловой
        системой, аудио-движком и внешними процессами — главный поток
        интерфейса никогда не блокируется на IO/запусках.
        """
        threading.Thread(
            target=fn, args=args, kwargs=kwargs, daemon=True, name="zeus-ui-bg"
        ).start()
    def _set_buttons(self, enabled: bool):
        """Включает/выключает кнопки (через zeus-ui)."""
        self._post_ui(lambda: self._do_set_buttons(enabled))

    def _do_set_buttons(self, enabled: bool):
        try:
            if self.listen_button and self.controller.audio.is_mic_available():
                self.listen_button.disabled = not enabled
                self.listen_button.update()
        except RuntimeError:
            pass


def main(page: ft.Page):
    ZeusUI(page)


if __name__ == "__main__":
    ft.run(main, view=ft.AppView.FLET_APP)
