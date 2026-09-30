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
from modules.tray import TrayManager

# === ZEUS v1.4: Новые UI-компоненты (ТЗ v1.4 §10) ===
from ui.zeus_state import ZeusState, ZeusStateController
from ui.theme import get_theme_manager
from ui.hologram import HologramWidget
from ui.buttons import ListenButton, PowerButton
from ui.main_window import ZeusMainWindow
from ui.settings import SettingsPanel

# === ZEUS v1.4 Neural Core (ТЗ Neural Core Interface) ===
# Единый Design System + декомпозиция: Sidebar / CoreCard / NeuralCore /
# SystemPanel+QuickActions+Recent / Terminal / Memory+Plugins.
# Backend (controller/actions/chat/audio) не трогаем — только UI-слой.
try:
    from ui.design_system import PALETTES as _DS_PALETTES
    from ui import sidebar as _sidebar
    from ui import core_card as _core_card
    from ui import neural_core as _neural_core
    from ui import system_panel as _system_panel
    from ui import terminal as _terminal
    from ui import memory_plugins as _memory_plugins
except ImportError:  # запуск из src/ (python src/ui/main_flet.py)
    from design_system import PALETTES as _DS_PALETTES  # type: ignore[no-redef]
    import sidebar as _sidebar  # type: ignore[no-redef]
    import core_card as _core_card  # type: ignore[no-redef]
    import neural_core as _neural_core  # type: ignore[no-redef]
    import system_panel as _system_panel  # type: ignore[no-redef]
    import terminal as _terminal  # type: ignore[no-redef]
    import memory_plugins as _memory_plugins  # type: ignore[no-redef]

try:
    from ui.audio_devices_panel import AudioDevicesPanel
except ImportError:  # noqa: BLE001 — запуск из src/
    from audio_devices_panel import AudioDevicesPanel  # type: ignore[no-redef]

# ВАЖНО: ZeusController НЕ импортируем на уровне модуля — его цепочка
# (vosk, sounddevice, piper, pyaudio) загружается секунды и откладывала
# старт socket-сервера Flet: клиент всё это время показывал «Working…».
# Импортируем лениво в _boot_worker, уже ПОСЛЕ отрисовки сплэша.


# === Design Tokens: единый источник — ui.design_system (ТЗ Neural Core §12) ===
# Локальный PALETTES оставлен как алиас для обратной совместимости тестов
# и старого кода (ссылается на канонические токены DS).
try:
    from ui.design_system import PALETTES as _CANON_PALETTES
except ImportError:  # запуск из src/
    from design_system import PALETTES as _CANON_PALETTES  # type: ignore[no-redef]
PALETTES = _CANON_PALETTES

# Текущая активная палитра (по умолчанию тёмная)
C = PALETTES["dark"]


# === Секции навигации (ТЗ Neural Core §2 + обратная совместимость) ===
# Порядок 0..2 сохранён для старых тестов/трея (Главная/логи/настройки),
# новые пункты Память/Плагины добавлены в конец.
SECTIONS = [
    {"label": "Главная", "icon": ft.Icons.DASHBOARD_ROUNDED},
    {"label": "Системные логи", "icon": ft.Icons.TERMINAL_ROUNDED},
    {"label": "Память", "icon": ft.Icons.PSYCHOLOGY_ROUNDED},
    {"label": "Плагины", "icon": ft.Icons.EXTENSION_ROUNDED},
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

        # Подписка на записи logging_service ([AUDIO]-диагностика → терминал)
        self._core_log_unsubscribe = None

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

        # === ZEUS v1.4: Новые компоненты (ТЗ v1.4 §8/§10) ===
        self.state_controller = ZeusStateController(ZeusState.IDLE)
        self._theme_manager = get_theme_manager()
        self.main_window: ZeusMainWindow | None = None
        self.settings_panel: SettingsPanel | None = None
        self._hologram_widget: HologramWidget | None = None
        self._listen_btn: ListenButton | None = None
        self._power_btn: PowerButton | None = None

        self._watcher_started = False
        self._hotkey = None
        self._tray = None
        self._exit_requested = False

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
        self._start_tray()
        self._subscribe_core_logs()

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
        # Перехватываем крестик: закрытие должно скрывать окно, а не завершать
        # процесс, чтобы горячие клавиши и голосовой режим продолжали работать.
        self.page.window.prevent_close = True
        self._attach_window_event_handler()

    def _start_tray(self) -> None:
        """Запускает иконку трея после готовности Flet-интерфейса."""
        try:
            self._tray = TrayManager(
                on_open=lambda: self._post_ui(self._show_window),
                on_settings=lambda: self._post_ui(
                    lambda: self._select_section(2)
                ),
                on_exit=lambda: self._post_ui(self._exit_application),
                icon_path=config.resource_path("assets/icons/app_icon.ico"),
                on_pick_input=self._on_tray_pick_input,
                on_pick_output=self._on_tray_pick_output,
            )
            if self._tray.start():
                self._log("☰ Иконка Zeus добавлена в системный трей")
                # Сразу заполняем подменю актуальными устройствами
                self._refresh_tray_audio_devices()
                # Подключаем hot-plug callback к контроллеру.
                # Имя атрибута — self.controller (единое для класса; ранние
                # обращения к self._controller падали AttributeError и трей
                # «терялся»: иконка создавалась, но ссылка занулялась).
                if self.controller is not None:
                    self.controller.on_tray_audio_update = (
                        self._refresh_tray_audio_devices
                    )
            else:
                self._tray = None
        except Exception as exc:  # noqa: BLE001
            self._tray = None
            self._log(f"☰ Системный трей недоступен: {exc}")


    def _refresh_tray_audio_devices(self) -> None:
        """Обновляет список аудиоустройств в подменю системного трея.

        Вызывается при запуске трея и при каждом hot-plug событии.
        Безопасна для вызова из фонового потока DeviceWatcher.
        """
        tray = self._tray
        ctrl = self.controller
        if tray is None or ctrl is None:
            return
        try:
            devices_in = ctrl.get_available_input_devices()
            devices_out = ctrl.get_available_output_devices()
            voice = ctrl.settings.get("voice_settings", {})
            active_in = voice.get("input_device_id")
            active_out = voice.get("output_device_id")
            tray.update_audio_devices(devices_in, devices_out, active_in, active_out)
        except Exception as exc:  # noqa: BLE001
            print(f"[UI] Ошибка обновления аудиоустройств в трее: {exc}")

    def _on_tray_pick_input(self, device_id: str | None) -> None:
        """Обработчик выбора микрофона из контекстного меню трея."""
        ctrl = self.controller
        if ctrl is None:
            return
        try:
            ctrl.set_input_device(device_id)
            self._refresh_tray_audio_devices()
        except Exception as exc:  # noqa: BLE001
            print(f"[UI] Ошибка переключения микрофона: {exc}")

    def _on_tray_pick_output(self, device_id: str | None) -> None:
        """Обработчик выбора динамиков/наушников из контекстного меню трея."""
        ctrl = self.controller
        if ctrl is None:
            return
        try:
            ctrl.set_output_device(device_id)
            self._refresh_tray_audio_devices()
        except Exception as exc:  # noqa: BLE001
            print(f"[UI] Ошибка переключения устройства вывода: {exc}")

    def _close_window(self) -> None:
        """Программно закрывает окно (совместимо с async-API Flet).

        Требование ТЗ (чек-лист №4): ``prevent_close`` сбрасывается ПЕРЕД
        close(), иначе Flet перехватит программное закрытие и снова пришлёт
        событие «close» (риск бесконечного цикла).

        Нюанс: в Flet 0.85+ ``Window.close()`` — корутина, поэтому прямой
        вызов «в лоб» создаст объект-корутину, который никогда не выполнится
        (окно не закроется). Корутина запускается через ``page.run_task()``;
        для синхронных версий API вызывается напрямую.
        """
        import inspect

        window = self.page.window
        try:
            window.prevent_close = False
        except Exception:
            pass
        close = getattr(window, "close", None)
        if close is None:
            return
        if inspect.iscoroutinefunction(close):
            self.page.run_task(close)
        else:
            close()

    def _exit_application(self) -> None:
        """Полное завершение работы приложения из трея (пункт «Выход»)."""
        self._exit_requested = True
        self._shutdown_background_services()
        try:
            self._close_window()
        except Exception:
            try:
                self.page.window.visible = False
            except Exception:
                pass

    def _shutdown_background_services(self) -> None:
        """Останавливает трей, watcher, хоткеи и анимацию (ТЗ §2.3).

        Останавливаются ВСЕ фоновые службы: иконка трея, файловый
        наблюдатель, глобальные горячие клавиши и цикл анимации.
        Метод идемпотентен — повторный вызов безопасен.
        """
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        if self._watcher_started:
            stop_watching()
            self._watcher_started = False
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:  # noqa: BLE001
                pass
            self._hotkey = None
        # Наблюдатель hot-plug аудиоустройств (v1.4)
        try:
            self.controller.stop_device_watcher()
        except Exception:  # noqa: BLE001
            pass
        # Отписка от [AUDIO]-логов (мост в терминал)
        unsub = getattr(self, "_core_log_unsubscribe", None)
        if callable(unsub):
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
            self._core_log_unsubscribe = None
        self._holo_running = False

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

    # ------------------------------------------------------------------
    # Мост [AUDIO]-диагностики в терминал интерфейса (Спринт 1 v1.4)
    # ------------------------------------------------------------------

    def _subscribe_core_logs(self) -> None:
        """Подписывается на записи logging_service.

        logging_service пишет только в файл; подписка позволяет показывать
        аудио-диагностику модулей (hot-plug: подключение/отключение устройств,
        причины Bluetooth/USB/jack, смена системного маршрута) прямо в
        терминале активности. Колбэк исполняется в потоке записи — UI-часть
        уходит в очередь через _post_ui.
        """
        if self._core_log_unsubscribe is not None:
            return
        try:
            from core.logging_service import subscribe
            self._core_log_unsubscribe = subscribe(self._on_core_log)
        except Exception as exc:  # noqa: BLE001
            print(f"[UI] Не удалось подписаться на [AUDIO]-логи: {exc}")

    def _on_core_log(self, message: str, level: int = 0) -> None:
        """Колбэк подписки: [AUDIO]-строки модулей → терминал интерфейса."""
        if not str(message).startswith(("[AUDIO]", "[AudioDevices]", "[AudioEngine]")):
            return
        try:
            self._post_ui(lambda: self._do_log(str(message)))
        except Exception:  # noqa: BLE001 — диагностика не должна ронять запись
            pass

    def _attach_window_event_handler(self) -> None:
        """Привязывает обработчик событий окна к API установленной версии Flet.

        КЛЮЧЕВОЙ ФИКС: в Flet >= 0.85 события окна живут в
        ``page.window.on_event`` (объект WindowEvent с полем ``type``),
        а атрибут ``page.on_window_event`` из старых версий удалён.
        Ранее мы присваивали обработчик несуществующему атрибуту —
        событие закрытия не доходило до _on_window_event, крестик
        блокировался prevent_close, и окно было невозможно закрыть.
        Для совместимости со старыми версиями привязываем оба канала.
        """
        window = getattr(self.page, "window", None)
        if window is not None and hasattr(window, "on_event"):
            window.on_event = self._on_window_event
        if hasattr(self.page, "on_window_event"):
            # Старые версии Flet (< 0.85)
            self.page.on_window_event = self._on_window_event

    @staticmethod
    def _window_event_name(event) -> str:
        """Нормализует событие окна к строковому имени ('close', 'resize', …).

        Flet >= 0.85: WindowEvent.type — enum WindowEventType (значение 'close').
        Старые версии: событие с строковым полем .data.
        """
        data = getattr(event, "data", None)
        if isinstance(data, str) and data:
            return data
        etype = getattr(event, "type", None)
        if etype is not None:
            return str(getattr(etype, "value", etype))
        return ""

    def _on_window_event(self, event) -> None:
        """Управляет поведением окна: крестик прячет в трей, а выход — закрывает.

        Также обрабатывает события максимизации/восстановления для корректного
        обновления layout при изменении размера окна.
        """
        try:
            name = self._window_event_name(event)
            if name == "close":
                # Проверяем, был ли запрошен принудительный выход (например,
                # пункт «Выход» в контекстном меню трея).
                if getattr(self, "_exit_requested", False):
                    self._shutdown_background_services()
                    self._close_window()
                elif self._tray is None:
                    # Трей недоступен — прятать окно нельзя: вернуть его на
                    # экран и завершить процесс будет нечем (зомби-процесс).
                    self._exit_requested = True
                    self._close_window()
                else:
                    # Обычный крестик (X) — просто скрываем окно в трей,
                    # фоновые процессы и трей продолжают работать.
                    self.page.window.visible = False
                    self.page.update()
                    self._log("Окно свернуто в системный трей")
            elif name in ("maximize", "unmaximize", "resize"):
                # Принудительно обновляем layout при изменении размера окна
                try:
                    if hasattr(self, "_root") and self._root is not None:
                        self._root.update()
                    self.page.update()
                except Exception:
                    pass
        except Exception as e:
            print(f"Ошибка в _on_window_event: {e}")

    # ------------------------------------------------------------------
    # Глобальная горячая клавиша (Ctrl+Alt+Space)
    # ------------------------------------------------------------------
    def _start_hotkey(self):
        """Регистрирует системный toggle и быстрый вызов ассистента."""
        app_settings = self.controller.settings.get("app_settings", {})
        if not app_settings.get(
            "global_hotkey_enabled", app_settings.get("hotkey_enabled", True)
        ):
            self._log("Горячая клавиша отключена в настройках")
            return
        try:
            from modules.hotkeys import GlobalHotkeyManager
        except Exception:
            self._log("⌨ Модуль горячих клавиш недоступен")
            return
        self._hotkey = GlobalHotkeyManager()
        started = self._hotkey.start(self._on_global_hotkey)
        interface_started = started and self._hotkey.register(
            "alt+x", self._on_interface_hotkey
        )
        if started and interface_started:
            self._log("⌨ Ctrl+Alt+Space: окно, Alt+X: вызов ассистента")
        else:
            self._hotkey.stop()
            self._hotkey = None
            self._log("⌨ Глобальные клавиши недоступны или заняты")

    def _on_global_hotkey(self):
        """Нажатие Ctrl+Alt+Space — только toggle видимости окна."""
        self._post_ui(self.handle_hotkey_trigger)

    def _on_interface_hotkey(self):
        """Нажатие Alt+X — показать интерфейс и активировать ассистента."""
        self._post_ui(self._show_window)
        self._log("⌨ Alt+X: вызов интерфейса и голосового ввода")
        self._run_bg(self.controller.force_activate)

    def _show_window(self) -> None:
        """Показывает окно поверх остальных окон и переводит его в фокус."""
        try:
            window = self.page.window
            # Защита перехвата крестика (ТЗ: «X» должен отрабатывать на
            # скрытие в трей после «Открыть»): Flet-клиент может сбросить
            # prevent_close/on_window_event за время цикла «скрыть в трей ->
            # показать» — восстанавливаем состояние ДО показа окна.
            # Примечание: при prevent_close=True нативный пункт «Закрыть»
            # в системном меню (Alt+Space) серый — это штатное поведение
            # клиента Flet (SC_CLOSE деактивируется), сам крестик при этом
            # перехватывается и приходит в _on_window_event.
            window.prevent_close = True
            self._attach_window_event_handler()
            window.visible = True
            window.minimized = False
            bring_to_front = getattr(window, "bring_to_front", None)
            if bring_to_front is None:
                bring_to_front = getattr(window, "to_front", None)
            if bring_to_front is not None:
                self.page.run_task(bring_to_front)
            window.focused = True
            self.page.update()
        except RuntimeError:
            pass

    def handle_hotkey_trigger(self) -> None:
        """Переключает видимость окна по глобальной горячей клавише."""
        try:
            window = self.page.window
            if window.visible:
                window.visible = False
            else:
                window.visible = True
                window.minimized = False
                bring_to_front = getattr(window, "bring_to_front", None)
                if bring_to_front is None:
                    bring_to_front = getattr(window, "to_front", None)
                if bring_to_front is not None:
                    self.page.run_task(bring_to_front)
                window.focused = True
            self.page.update()
        except RuntimeError:
            pass

    def _apply_start_minimized(self) -> None:
        """Скрывает окно после запуска, оставляя фоновые сервисы активными."""
        app_settings = self.controller.settings.get("app_settings", {})
        if not app_settings.get("start_minimized", False):
            return
        if self._tray is None:
            self._log(
                "Окно не скрыто: системный трей недоступен, интерфейс оставлен видимым"
            )
            return
        try:
            self.page.window.visible = False
            self.page.update()
            self._log("Окно запущено в свернутом виде")
        except RuntimeError:
            pass

    def _toggle_hotkey(self, e):
        """Включает/выключает горячую клавишу из настроек (live).

        .. deprecated:: Тумблер «Глобальная клавиша вызова Ctrl+Alt+Space»
            удалён из настроек; метод оставлен временно для совместимости
            и более нигде не вызывается.
        """
        value = bool(e.control.value)
        # Запись settings.json — в фоновом потоке (IO не в потоке Flet)
        self._run_bg(
            self.controller.update_settings,
            "app_settings",
            "global_hotkey_enabled",
            value,
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
        saved_settings = self.controller.get_settings() if self.controller else {}
        saved_app_settings = saved_settings.get("app_settings", {})
        saved_theme = saved_app_settings.get(
            "theme", saved_settings.get("theme", "dark")
        )
        global C
        C = PALETTES["light" if saved_theme == "light" else "dark"]
        # В нативной строке — только имя приложения; кнопки окна рисует Windows.
        page.title = "Zeus"
        page.theme_mode = (
            ft.ThemeMode.LIGHT if saved_theme == "light" else ft.ThemeMode.DARK
        )
        page.bgcolor = C["bg"]
        # ТЗ Neural Core: окно под 3-колоночный макет (сайдбар 250 + центр + 350).
        page.window.width = 1380
        page.window.height = 860
        page.window.min_width = 1180
        page.window.min_width = 900
        page.window.min_height = 650
        page.window.resizable = True
        page.window.maximizable = True
        page.window.prevent_close = True
        page.padding = 0

        # Адаптивный ресайз: подстраиваем голограмму и layout под размер окна.
        # Голограмма уменьшена: GIF в пределах [220, 360] внутри сферы 360px.
        def on_resized(e):
            w = page.window.width or 1380
            h = page.window.height or 860
            # Голограмма: компактная сфера ~26% ширины / ~42% высоты окна
            new_size = max(220, min(360, int(min(w * 0.26, h * 0.42))))
            if getattr(self, "holo_image", None):
                self.holo_image.width = new_size
                self.holo_image.height = new_size
                try:
                    self.holo_image.update()
                except RuntimeError:
                    pass
            # Терминал: на маленьких экранах — 150px, на больших — до 260px
            terminal_h = max(150, min(260, int(h * 0.28)))
            if getattr(self, "_log_container", None):
                self._log_container.height = terminal_h
                try:
                    self._log_container.update()
                except RuntimeError:
                    pass
            # Перестраиваем main_row при пересечении порога 1100px
            dashboard = getattr(self, "dashboard_content", None)
            left = getattr(self, "_left_panel", None)
            center_ctrl = getattr(self, "_center", None)
            right = getattr(self, "_right_panel", None)
            if dashboard and left and center_ctrl and right:
                new_row = self._build_adaptive_main_row(left, center_ctrl, right, w)
                try:
                    col = dashboard.content
                    if isinstance(col, ft.Column) and len(col.controls) > 2:
                        col.controls[2] = new_row
                        dashboard.update()
                except (RuntimeError, IndexError, AttributeError):
                    pass

        page.on_resized = on_resized

        # Создаём контент для всех разделов
        # Порядок важен: сначала классика (голос/логи/настройки), затем
        # Neural Core поверх тех же данных (system snapshot/история/терминал).
        self._build_voice_section()
        self._build_logs_section()
        self._build_settings_section()

        # === ZEUS v1.4: Новое главное окно с голограммой (ТЗ v1.4 §2/§10) ===
        self._build_v14_main_window()

        # Сайдбар 240px (ТЗ v1.2, п.4.1)
        self.sidebar = self._build_sidebar()
        # Баг-фикс: принудительная подсветка активного пункта при загрузке
        self._select_section(0)

        # Область контента (отступы 30px — ТЗ п.4.2)
        self.content_area = ft.Container(
            content=self._get_section_content(0),
            bgcolor=C["bg"],
            expand=True,
            padding=32,  # ТЗ §8, §13: сетка 32px
        )

        # Заголовок контентной части (вместо AppBar — ТЗ п.4.2)
        # ТЗ v1.4 §2: добавлена кнопка переключения Classic / New UI
        self.page_title = ft.Text(
            SECTIONS[0]["label"],
            size=32,
            weight=ft.FontWeight.BOLD,
            color=C["text"],
        )

        # Ряд заголовка с кнопкой переключения вида (ТЗ v1.4 §2)
        self._title_row = ft.Row(
            [
                self.page_title,
                ft.Container(expand=True),
                self._v14_view_toggle,
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
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

    def _build_adaptive_main_row(
        self,
        left_panel: ft.Column,
        center: ft.Column,
        right_panel: ft.Column,
        page_width: int = 1380,
    ) -> ft.Row | ft.Column:
        """Адаптивный layout: 3 колонки на широких экранах, 1 колонка на узких.

        При ширине < 1100px переключается в одноколоночный режим с scroll —
        на маленьких экранах панели идут вертикально, чтобы не накладываться.
        """
        if page_width >= 1100:
            # Широкий экран: 3 колонки в ряд
            return ft.Row(
                [
                    left_panel,
                    ft.Container(
                        content=center,
                        expand=True,
                        alignment=ft.Alignment.CENTER,
                        padding=ft.Padding.symmetric(horizontal=8),
                    ),
                    right_panel,
                ],
                spacing=14,
                vertical_alignment=ft.CrossAxisAlignment.START,
                expand=True,
            )
        else:
            # Узкий экран: одна колонка с адаптивными ширинами
            left_panel.expand = True
            right_panel.expand = True
            return ft.Column(
                [
                    left_panel,
                    ft.Container(height=10),
                    ft.Container(
                        content=center,
                        alignment=ft.Alignment.CENTER,
                        padding=ft.Padding.symmetric(vertical=16),
                    ),
                    ft.Container(height=10),
                    right_panel,
                ],
                spacing=0,
                expand=True,
                scroll=ft.ScrollMode.AUTO,
            )

    # ------------------------------------------------------------------
    # ZEUS v1.4: Новое главное окно (ТЗ v1.4 §2/§10)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # ZEUS v1.4: Новое главное окно (ТЗ v1.4 §2/§10)
    # ------------------------------------------------------------------
    def _build_v14_main_window(self) -> None:
        """Создаёт новое главное окно ZEUS v1.4 с голограммой и floating buttons.

        ТЗ v1.4 §2: структура с голограммой, статусом, чатом и кнопками.
        """
        p = self._theme_manager.get_palette()

        self.main_window = ZeusMainWindow(
            state_controller=self.state_controller,
            on_listen=self._on_v14_listen,
            on_power=self._on_v14_power,
        )

        # Подключаем смену темы к новому окну
        self._theme_manager.on_change(self._on_v14_theme_change)

    def _on_v14_listen(self, e: ft.ControlEvent) -> None:
        """Обработчик кнопки Listen в новом окне (ТЗ v1.4 §4)."""
        if self.controller and self.controller.audio.is_mic_available():
            self._on_listen_once()

    def _on_v14_power(self, e: ft.ControlEvent) -> None:
        """Обработчик кнопки Power в новом окне (ТЗ v1.4 §4)."""
        self._toggle_sleep(e)

    def _on_v14_theme_change(self, theme: str, palette: dict[str, str]) -> None:
        """Обработчик смены темы для нового окна (ТЗ v1.4 §7)."""
        if self.main_window:
            self.main_window._on_theme_change(theme, palette)

    def _toggle_v14_view(self, e: ft.ControlEvent) -> None:
        """Переключает между Classic и ZEUS v1.4 видом (ТЗ v1.4 §2)."""
        if not self.main_window:
            return

        self._v14_active = not self._v14_active

        def _apply():
            try:
                if self._v14_active:
                    self.content_area.content = self.main_window
                    self._v14_view_toggle.icon_color = C.get("accent", "#00E5FF")
                    self._v14_view_toggle.tooltip = "Переключить вид: Classic"
                else:
                    self.content_area.content = self._classic_content
                    self._v14_view_toggle.icon_color = C.get("text_dim", "#94A3B8")
                    self._v14_view_toggle.tooltip = "Переключить вид: ZEUS v1.4"
                self.content_area.update()
                self._v14_view_toggle.update()
            except RuntimeError:
                pass

        self._post_ui(_apply)

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
            is_selected = (i == index)
            # Баг-фикс: используем единый метод build_nav_item
            new_btn = _sidebar.build_nav_item(
                section["icon"], section["label"], is_selected, C
            )
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
            # Раздел «Настройки»: лениво подгружаем список аудиоустройств
            # (COM-запрос выполняется в фоне, UI не блокируется).
            if index == 4 and getattr(self, "_audio_devices_panel", None) is not None:
                self._audio_devices_panel.refresh_async()
            self.content_area.update()

    def _get_section_content(self, index: int) -> ft.Control:
        """Возвращает контент для выбранного раздела (ТЗ Neural Core)."""
        if index == 0:
            return self.dashboard_content
        elif index == 1:
            return self.logs_content
        elif index == 2:
            return self._memory_content()
        elif index == 3:
            return self._plugins_content()
        elif index == 4:
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
                        self._title_row,
                        self.content_area,
                    ],
                    expand=True,
                    spacing=0,
                ),
            ],
            expand=True,
            spacing=0,
        )

    def _system_snapshot(self) -> dict[str, float]:
        """CPU/RAM/Disk снимок для SystemPanel (best-effort)."""
        snapshot = {"cpu": 0.18, "ram": 0.42, "disk": 0.63}
        try:
            import psutil
            try:
                snapshot["cpu"] = max(0.0, min(1.0, float(psutil.cpu_percent(interval=None)) / 100.0)) or 0.05
            except Exception:
                pass
            try:
                snapshot["ram"] = max(0.0, min(1.0, float(psutil.virtual_memory().percent) / 100.0))
            except Exception:
                pass
            try:
                import os as _os
                _root = _os.path.abspath(_os.sep)
                snapshot["disk"] = max(0.0, min(1.0, float(psutil.disk_usage(_root).percent) / 100.0))
            except Exception:
                pass
        except Exception:
            pass
        return snapshot

    def _on_quick_action(self, key: str) -> None:
        """Быстрые действия правой колонки — через существующий backend."""
        mapping = {
            "browser": "открой браузер",
            "file": "найди файл",
            "music": "включи музыку",
            "vscode": "запусти vs code",
        }
        text = mapping.get(key)
        if text:
            self._run_bg(self._on_listen_once, text)

    def _recent_commands(self, limit: int = 6):
        """Последние команды из лога сессии для правой колонки."""
        items = []
        try:
            import re
            for line in list(getattr(self, "_session_lines", []) or [])[::-1]:
                m = re.match(r"\[(\d+:\d+:\d+)\]\s*(?:USER|YOU)[:\s]+(.+)", str(line))
                if m and m.group(2).strip():
                    items.append((m.group(1)[-5:], m.group(2).strip()[:42]))
                if len(items) >= limit:
                    break
        except Exception:
            pass
        return items or [
            ("17:42", "Открыть браузер"),
            ("17:38", "Включить музыку"),
            ("17:35", "Найди файл проекта"),
            ("17:32", "Покажи погоду"),
            ("17:28", "Запусти VS Code"),
            ("17:25", "Сделай скриншот"),
        ]

    def _memory_content(self) -> ft.Control:
        """Страница MEMORY: каркас (ТЗ п.10)."""
        history = [cmd for _, cmd in self._recent_commands(limit=10)]
        return _memory_plugins.build_memory_page(C, history=history, on_clear=lambda e: None)

    def _plugins_content(self) -> ft.Control:
        """Страница PLUGINS: карточки вкл/выкл (только UI)."""
        return _memory_plugins.build_plugins_page(C, on_toggle=lambda k, v: self._log(f"Плагин {k}: " + ("вкл" if v else "выкл")))

    def _build_sidebar(self):
        """Sidebar Neural Core 250px (ТЗ §2): оформление — ui.sidebar.

        Контракт сохранён: self._nav_buttons[i], _select_section(i), _nav_hover.
        """
        self._nav_buttons = []
        logo_block = _sidebar.build_logo(C, config.resource_path("assets/icons/logo_1024.png"))
        for i, section in enumerate(SECTIONS):
            btn = _sidebar.build_nav_item(section["icon"], section["label"], i == self.selected_index, C)
            btn.data = i
            btn.on_click = lambda e: self._select_section(int(e.control.data))
            btn.on_hover = lambda e: self._nav_hover(e, int(e.control.data) == self.selected_index)
            self._nav_buttons.append(btn)
        status_footer = _sidebar.build_status_footer(C)
        return ft.Container(
            content=ft.Column(
                [
                    logo_block,
                    ft.Container(height=10),
                    *self._nav_buttons,
                    ft.Container(expand=True),
                    status_footer,
                ],
                spacing=6,
                expand=True,
            ),
            width=_sidebar.SIDEBAR_WIDTH,
            bgcolor=C["sidebar"],
            padding=ft.Padding(16, 18, 16, 16),
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
        # Малая подпись текущих аудиоустройств (ТЗ v1.4 §3.2)
        self._devices_label = ft.Text("", size=11, color=C["text_dim"], max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)

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
        self._status_legacy = ft.Column([self.status_dot, self.status_dot_label,
            self.voice_status], spacing=2, visible=False)

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
                expand=False,
            ),
            bgcolor=C["terminal_background"],
            border_radius=12,
            border=ft.Border.all(1, ft.Colors.with_opacity(0.1, ft.Colors.WHITE)),
            padding=20,
            height=200,
        )
        self._log_container = log_container  # для адаптивного ресайза

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
            value=bool(
                self.controller.get_settings().get("app_settings", {}).get(
                    "auto_listening",
                    self.controller.get_settings().get("auto_listening", True),
                )
            ),
            active_color=C["electric"],
            on_change=self._toggle_auto_listen,
            width=250,
        )
        # Информационная иконка (ТЗ v1.2 §Б): подсказка о режиме
        auto_listen_info = ft.Icon(
            ft.Icons.INFO_OUTLINE,
            size=14,
            color=C["text_dim"],
            tooltip="Постоянное распознавание Wake Word «Зевс» без ручного запуска",
        )

        # Кнопка очистки терминала (ТЗ §16): маленькая IconButton 32×32.
        # Важно: чистит только визуальный список — data/zeus.log на диске
        # не трогает, полная история доступна через кнопку открытия лога.
        self.clear_log_btn = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            icon_size=16,
            icon_color=C["text_dim"],
            width=32,
            height=32,
            tooltip="Очистить терминал (файл zeus.log на диске не удаляется)",
            on_click=self._clear_log,
        )

        # Кнопка открытия файла лога (бэклог v1.4 п.2): zeus.log уже пишется
        # logging_service на диск — просто показываем его пользователю.
        self.open_log_btn = ft.IconButton(
            icon=ft.Icons.FOLDER_OPEN_OUTLINED,
            icon_size=16,
            icon_color=C["text_dim"],
            width=32,
            height=32,
            tooltip="Открыть файл журнала zeus.log (полная история, даже после очистки терминала)",
            on_click=self._open_log_file,
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
                    # Подпись текущих устройств ввода/вывода (ТЗ v1.4 §3.2):
                    # видно, через что Зевс слушает/говорит, без настроек.
                    self._devices_label,
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
                self.open_log_btn,
                self.clear_log_btn,
            ],
            spacing=8,
        )

        model_name = "llama3.1"
        try:
            if self.controller is not None:
                model_name = str(getattr(self.controller.chat, "model", model_name) or model_name)
        except Exception:
            pass

        # === ZEUS v1.4 Neural Core: адаптивный макет ===
        # Левая колонка — ZEUS CORE: фиксированная ширина на больших экранах,
        # но с min/max для адаптивности.
        left_panel = ft.Column(
            [
                _core_card.build_core_card(
                    C, model=model_name,
                    voice_engine="Vosk", memory="Активна", plugins="5 загружено",
                    online=True, transparent=True,
                ),
                ft.Container(height=2),
                self._devices_label,
                ft.Row([self.voice_switch, auto_listen_info], spacing=4),
            ],
            spacing=6,
            width=270,
        )

        # Центр — светящаяся сфера-ядро (Ref: яркий круг по центру кадра).
        # Адаптивный expand: занимает всё доступное пространство между панелями.
        center = _neural_core.build_neural_core(
            self.holo_container, "Готов к работе", "Слушаю...", C, size=360, globe=True,
        )
        self._neural_status_text = center.status_text
        self._neural_sub_text = center.sub_text

        snap = self._system_snapshot()
        # Правая колонка — System Status + Быстрые действия + Последние команды.
        # Адаптивная ширина: min 240px, expand 1 (растягивается).
        right_panel = ft.Column(
            [
                _system_panel.build_system_panel(
                    C, cpu=snap["cpu"], ram=snap["ram"], disk=snap["disk"], transparent=True,
                ),
                _system_panel.build_quick_actions(
                    C, on_action=self._on_quick_action, transparent=True,
                ),
                _system_panel.build_recent_commands(
                    C, items=self._recent_commands(limit=5), transparent=True,
                ),
            ],
            spacing=10,
            width=300,
        )
        self._right_column = right_panel
        self._left_panel = left_panel
        self._center = center
        self._right_panel = right_panel

        # Шапка: подзаголовок слева, ONLINE-пилюля + LED + кнопки вправо
        # (заголовок «Главная» живёт в page_title над content_area).
        online_pill = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        width=9, height=9, border_radius=5, bgcolor=C["green"],
                        shadow=ft.BoxShadow(blur_radius=8, spread_radius=1, color=C["green"]),
                    ),
                    ft.Text("ONLINE", size=12, weight=ft.FontWeight.W_600, color=C["text"]),
                    ft.Text("| Соединение с Zeus", size=11, color=C["text_dim"]),
                    self.voice_status,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=8),
            bgcolor=ft.Colors.with_opacity(0.05, C["green"]),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.35, C["green"])),
            border_radius=999,
        )
        header_row = ft.Row(
            [
                ft.Text("Ваш личный AI-ассистент. Всегда рядом.", size=13, color=C["text_dim"]),
                ft.Container(expand=True),
                self.status_dot,
                online_pill,
                ft.Container(width=6),
                self.sleep_button,
                self.listen_button,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # Адаптивный layout: 3 колонки на широких экранах, 1 колонка на узких
        page_w = self.page.window.width if self.page and self.page.window else 1380
        main_row = self._build_adaptive_main_row(left_panel, center, right_panel, page_w)

        controls = ft.Container(
            content=ft.Column(
                [
                    header_row,
                    ft.Container(height=8),
                    main_row,
                    ft.Container(height=10),
                    terminal_header,
                    log_container,
                ],
                spacing=8,
                expand=True,
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

        # === ZEUS v1.4: Переключатель Classic / New UI (ТЗ v1.4 §2) ===
        self._v14_view_toggle = ft.IconButton(
            icon=ft.Icons.AUTO_AWESOME_ROUNDED,
            icon_size=18,
            icon_color=C["text_dim"],
            tooltip="Переключить вид: Classic / ZEUS v1.4",
            on_click=self._toggle_v14_view,
        )
        self._v14_active = False
        self._classic_content = controls

        # === ZEUS v1.4: Панель настроек v1.4 (ТЗ v1.4 §11) ===
        self.settings_panel = SettingsPanel(
            palette=C,
            on_theme_toggle=self._toggle_theme,
        )

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
        """Мгновенно переключает Design Tokens и перестраивает UI.

        ТЗ v1.4 §7: также обновляет ThemeManager для нового окна.
        """
        new_key = "light" if self.page.theme_mode == ft.ThemeMode.DARK else "dark"
        if self.controller is not None:
            self._run_bg(
                self.controller.update_settings, "app_settings", "theme", new_key
            )
        global C
        C = PALETTES[new_key]
        self.page.theme_mode = (
            ft.ThemeMode.LIGHT if new_key == "light" else ft.ThemeMode.DARK
        )
        # ТЗ v1.4 §7: синхронизация с ThemeManager
        if hasattr(self, "_theme_manager") and self._theme_manager:
            self._theme_manager.set_theme(new_key)
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
        paths = settings.get("paths", {})

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
            ], spacing=12),
            bgcolor=C["surface"],
            border_radius=16,
            border=ft.Border.all(1, C["border"]),
            padding=20,
            expand=True,
        )

        # ---- Карточка 2b: Аудиоустройства (v1.4) ----
        self._audio_devices_panel = AudioDevicesPanel(self, C)
        audio_card = self._audio_devices_panel.build()

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

        workspace_path = ft.TextField(
            label="Рабочий каталог",
            value=paths.get("workspace", "./data"),
            width=360,
            text_size=13,
            color=C["text"],
            bgcolor=C["surface"],
            border_color=C["border"],
            on_change=lambda e: self._on_setting_change(
                "paths", "workspace", e.control.value
            ),
        )
        dictionary_path = ft.TextField(
            label="Путь к словарю",
            value=paths.get("dictionary", "./data/dictionary.json"),
            width=360,
            text_size=13,
            color=C["text"],
            bgcolor=C["surface"],
            border_color=C["border"],
            on_change=lambda e: self._on_setting_change(
                "paths", "dictionary", e.control.value
            ),
        )
        self._settings_widgets["workspace_path"] = workspace_path
        self._settings_widgets["dictionary_path"] = dictionary_path

        extra_paths = ft.ExpansionTile(
            title=ft.Text("Показать дополнительные пути", size=13, color=C["text"]),
            leading=ft.Icon(ft.Icons.FOLDER_OPEN_OUTLINED, color=C["electric"]),
            controls=[
                ft.Row([workspace_path], spacing=12),
                ft.Row([dictionary_path], spacing=12),
            ],
            expanded=False,
            maintain_state=True,
            text_color=C["text"],
            icon_color=C["electric"],
            collapsed_text_color=C["text"],
            collapsed_icon_color=C["text_dim"],
            tile_padding=0,
            controls_padding=ft.Padding(left=0, top=8, right=0, bottom=0),
        )

        # ---- Исключения сканирования файлов (deny-лист, аудит п.3) ----
        scanner_cfg = settings.get("scanner_settings", {}) or {}
        deny_input = ft.TextField(
            label="Путь для исключения (например D:\\Личное)",
            width=360,
            text_size=12,
            color=C["text"],
            bgcolor=C["surface"],
            border_color=C["border"],
        )
        deny_list_col = ft.Column(spacing=2, tight=True)

        def _deny_paths() -> list:
            return list(scanner_cfg.get("custom_deny_paths", []) or [])

        def _refresh_deny_rows() -> None:
            deny_list_col.controls = [
                ft.Row([
                    ft.Text(p, size=12, color=C["text"], expand=True,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.IconButton(
                        icon=ft.Icons.CLOSE, icon_size=14,
                        icon_color=C["text_dim"], tooltip="Убрать",
                        on_click=lambda e, p=p: _deny_remove(p),
                    ),
                ], spacing=8)
                for p in _deny_paths()
            ]

        def _deny_update(updated: list) -> None:
            """Сохраняет список и перерисовывает строки (без полной перестройки)."""
            scanner_cfg["custom_deny_paths"] = updated
            _refresh_deny_rows()
            self._on_setting_change("scanner_settings", "custom_deny_paths", updated)
            try:
                if deny_list_col.page:
                    deny_list_col.update()
            except Exception:
                pass

        def _deny_remove(path: str) -> None:
            _deny_update([p for p in _deny_paths() if p != path])

        def _deny_add(e=None) -> None:
            raw = (deny_input.value or "").strip()
            if not raw:
                return
            import os as _os
            norm = _os.path.normpath(raw)
            current = _deny_paths()
            if norm not in current and norm.lower() not in (p.lower() for p in current):
                current.append(norm)
                deny_input.value = ""
                _deny_update(current)
            else:
                self._show_save_notice("⚠️ Такой путь уже в списке")

        _refresh_deny_rows()

        # ---- Статус индекса + «Пересканировать сейчас» (ТЗ rescan_status §5,
        # бэклог v1.4 п.1: кнопка/спиннер синхронизируются с is_indexing) ----
        scan_status_text = ft.Text("", size=12, color=C["text_dim"], expand=True)
        rescan_btn = ft.TextButton(
            "🔄 Пересканировать сейчас",
            icon=ft.Icons.REFRESH_ROUNDED,
            on_click=None,  # назначается ниже (после определения _rescan_now)
        )
        # Спиннер рядом с кнопкой: виден только во время индексации
        rescan_spinner = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)

        def _format_age(hours) -> str | None:
            """Человекочитаемый возраст индекса: минуты/часы/дни."""
            if hours is None:
                return None
            if hours < 1:
                return f"{max(1, int(hours * 60))} мин. назад"
            if hours < 24:
                return f"{int(hours)} ч. назад"
            return f"{int(hours // 24)} дн. назад"

        def _get_stats() -> dict:
            """Статистика через контроллер; fallback на singleton, если он ещё не готов."""
            if self.controller is not None:
                return self.controller.get_file_index_stats()
            from core.file_scanner import get_scanner
            return get_scanner().get_index_stats()

        def _status_from_stats(s: dict) -> str:
            if s.get("is_indexing"):
                return "Индексация дисков идёт…"
            if not s.get("total_files"):
                return "Индекс ещё не построен — запустите сканирование"
            text = f"{s['total_files']} файлов в индексе"
            age = _format_age(s.get("index_age_hours"))
            if age:
                text += f" · обновлено {age}"
            if not s.get("is_fresh", False):
                text += " · ⚠ давно не обновлялся"
            return text

        def _set_ui_safe(fn) -> None:
            try:
                fn()
            except RuntimeError:
                pass  # страница закрыта

        def _set_indexing_ui(indexing: bool) -> None:
            """Синхронизирует кнопку/спиннер с состоянием индексации (бэклог п.1).

            Вызывается и при первой отрисовке (если приложение перезапущено
            во время скана — состояние берётся из is_indexing), и по
            завершении фонового опроса.
            """
            rescan_btn.disabled = indexing
            rescan_btn.text = "Индексация…" if indexing else "🔄 Пересканировать сейчас"
            rescan_spinner.visible = indexing

        def _update_scan_widgets() -> None:
            _set_ui_safe(lambda: (
                scan_status_text.update() if scan_status_text.page else None,
                rescan_btn.update() if rescan_btn.page else None,
                rescan_spinner.update() if rescan_spinner.page else None,
            ))

        def _refresh_scan_status(*_):
            try:
                stats = _get_stats()
            except Exception as exc:
                scan_status_text.value = f"Статус недоступен: {exc}"
            else:
                scan_status_text.value = _status_from_stats(stats)
                # Первая отрисовка: если сканирование уже идёт (например,
                # автоскан при старте приложения) — кнопка сразу в состоянии
                # «занято», а не только после первого клика.
                _set_indexing_ui(bool(stats.get("is_indexing", False)))
            _update_scan_widgets()

        def _apply_poll_result(stats: dict, indexing: bool) -> None:
            """Обновление UI из фонового опроса (вызывается через _post_ui)."""
            scan_status_text.value = _status_from_stats(stats)
            _set_indexing_ui(indexing)
            _update_scan_widgets()

        def _poll_indexing() -> None:
            """Фоновый опрос статистики каждые 2 с, пока идёт индексация."""
            import time as _time
            while True:
                _time.sleep(2)
                try:
                    stats = _get_stats()
                except Exception:
                    stats = {"is_indexing": True}
                indexing = bool(stats.get("is_indexing", False))
                self._post_ui(lambda s=stats, i=indexing: _apply_poll_result(s, i))
                if not indexing:
                    break

        def _rescan_now(e=None) -> None:
            # Явная блокировка повторного запуска поверх идущего сканирования:
            # кнопка дизейблится сразу, плюс бэкенд сам вернёт success: False.
            if rescan_btn.disabled:
                return
            try:
                if self.controller is not None:
                    result = self.controller.rebuild_file_index()
                else:
                    from core.file_scanner import rebuild_system_index
                    result = rebuild_system_index()
            except Exception as exc:
                self._show_save_notice(f"⚠️ Не удалось запустить сканирование: {exc}")
                return
            self._show_save_notice(str(result.get("message", "Запускаю сканирование…")))
            if not result.get("success"):
                _refresh_scan_status()
                return
            rescan_btn.disabled = True
            _set_indexing_ui(True)
            scan_status_text.value = "Индексация дисков идёт…"
            _update_scan_widgets()
            threading.Thread(target=_poll_indexing, daemon=True).start()

        rescan_btn.on_click = _rescan_now
        _refresh_scan_status()

        deny_tile = ft.ExpansionTile(
            title=ft.Text("Исключения сканирования файлов", size=13, color=C["text"]),
            leading=ft.Icon(ft.Icons.BLOCK_OUTLINED, color=C["electric"]),
            controls=[
                deny_input,
                ft.Row([
                    ft.TextButton("➕ Добавить", icon=ft.Icons.PLAYLIST_ADD,
                                  on_click=_deny_add),
                ], spacing=8),
                deny_list_col,
                ft.Text(
                    "Системные папки и служебные каталоги (node_modules, Temp, "
                    "кэши браузеров) исключены из индексации всегда и в этом "
                    "списке не отображаются.",
                    size=11, color=C["text_dim"],
                ),
                ft.Divider(height=1, color=C["border"]),
                ft.Row(
                    [scan_status_text, rescan_spinner, rescan_btn],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            expanded=False,
            maintain_state=True,
            text_color=C["text"],
            icon_color=C["electric"],
            collapsed_text_color=C["text"],
            collapsed_icon_color=C["text_dim"],
            tile_padding=0,
            controls_padding=ft.Padding(left=0, top=8, right=0, bottom=0),
        )

        app_card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.APPS_OUTLINED, color=C["electric"], size=22),
                    ft.Text("Интерфейс и система", size=16, weight=ft.FontWeight.BOLD, color=C["text"]),
                ], spacing=8),
                ft.Divider(height=1, color=C["border"]),
                ft.Row([theme_dd], spacing=12),
                ft.Row([autostart_sw], spacing=12),
                extra_paths,
                deny_tile,
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
                    # Нижний ряд: аудиоустройства + интерфейс (v1.4)
                    ft.Row(
                        [audio_card],
                        expand=True,
                    ),
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
            C = PALETTES[theme_key]
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
            saved = self.controller.settings_manager.save()
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
        В покое фиксированный размер ~320px; при речи ассистента масштаб плавно
        увеличивается (scale 1.0 -> 1.25) через animate_scale.
        """
        is_light = self.page.theme_mode == ft.ThemeMode.LIGHT
        file_name = "hologram_light.webp" if is_light else "hologram.webp"
        webp = os.path.join("assets", "animations", file_name)
        img_path = config.resource_path(webp)

        self.holo_image = ft.Image(
            src=img_path,
            width=320,
            height=320,
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
        # Модели загружаются после подключения UI-колбэков, поэтому каждый
        # этап Vosk/Piper виден в терминале активности.
        ctrl.start_model_loader(on_progress=self._log)

        # Hot-plug аудиоустройств (v1.4, ТЗ §2.4): события подключения/
        # отключения пишутся в терминал активности; при исчезновении
        # выбранного микрофона — автовыбор по приоритету.
        self._run_bg(ctrl.start_device_watcher, self._on_device_changed)
        self._run_bg(self._update_devices_label)

    def _on_device_changed(self, payload: dict) -> None:
        """Уведомление hot-plug аудиоустройств (ТЗ v1.4 §3.1)."""
        event = payload.get("event")
        device = payload.get("device") or {}
        name = device.get("name", "?")
        if event == "added":
            self._log(f"🎧 Подключено аудиоустройство: {name}")
        elif event == "removed":
            self._log(f"🎧 Аудиоустройство отключено: {name}")
        elif event == "auto_switched_input":
            self._log(f"⚠️ Микрофон отключён — переключаюсь на {name}")
            self.speak_or_status("Микрофон переключён, сэр")
        elif event == "input_lost":
            self._log("❌ Микрофон недоступен — устройства ввода не найдены")
        elif event == "output_lost":
            self._log("⚠️ Устройство вывода отключено — звук в системный дефолт")
        self._post_ui(self._update_devices_label)

    def speak_or_status(self, text: str) -> None:
        """Голосовое/статусное уведомление (не блокирует UI-поток)."""
        self._run_bg(self._speak_or_status_worker, text)

    def _speak_or_status_worker(self, text: str) -> None:
        try:
            self.controller.speak(text)
        except Exception:  # noqa: BLE001
            self._on_status(text)

    def _update_devices_label(self) -> None:
        """Обновляет подпись текущих устройств ввода/вывода (ТЗ v1.4 §3.2)."""
        try:
            label = self._do_devices_label()
        except Exception:  # noqa: BLE001
            return

        def apply():
            try:
                self._devices_label.value = label
                self._devices_label.update()
            except RuntimeError:
                pass
        self._post_ui(apply)

    def _do_devices_label(self) -> str:
        audio = self.controller.audio
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            idx_in = audio._mic_device_index
            idx_out = audio._output_device_index
            name_in = str(devices[idx_in]["name"]) if idx_in is not None and 0 <= idx_in < len(devices) else "по умолчанию"
            name_out = str(devices[idx_out]["name"]) if idx_out is not None and 0 <= idx_out < len(devices) else "по умолчанию"
            return f"🎙 {name_in}  ·  🔊 {name_out}"
        except Exception:  # noqa: BLE001
            return ""


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
        # Neural Core: статус-пилюля центральной голограммы
        try:
            if getattr(self, "_neural_status_text", None):
                self._neural_status_text.value = str(text)[:48]
                self._neural_status_text.update()
        except (AttributeError, RuntimeError):
            pass

    def _log(self, text: str):
        """Добавляет запись в лог голосового помощника (через zeus-ui)."""
        try:
            from core.logging_service import infer_level, log
            log(text, infer_level(text))
        except Exception:
            pass
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

    def _open_log_file(self, e=None):
        """Открывает data/zeus.log системным обработчиком (бэклог v1.4 п.2).

        Файл уже пишется logging_service (RotatingFileHandler) при каждой
        записи терминала — просто показываем его. Открывается полная история,
        даже если пользователь до этого нажал «очистить» в UI (очистка
        затрагивает только визуальный список, не файл).
        """
        try:
            from core.logging_service import _default_path
            path = _default_path()
            if not os.path.exists(path):
                # Файла ещё нет — создаём пустой, чтобы открыть не пустоту,
                # а валидный (пусть и свежий) журнал.
                with open(path, "a", encoding="utf-8"):
                    pass
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 — Windows: системный обработчик
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001 — UI не должен падать
            self._show_save_notice(f"⚠️ Не удалось открыть журнал: {exc}")

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
        """Реагирует на смену состояния FSM (обновления — через zeus-ui).

        ТЗ v1.4 §8: также обновляет ZeusStateController для голограммы.
        """
        self._post_ui(lambda: self._do_state(state))
        # ТЗ v1.4 §8: синхронизация с новым контроллером состояния
        self._sync_state_controller(state)

    def _sync_state_controller(self, state: str) -> None:
        """Синхронизирует ZeusStateController с FSM-состоянием контроллера (ТЗ v1.4 §8)."""
        state_map = {
            "IDLE": ZeusState.IDLE,
            "LISTENING": ZeusState.LISTENING,
            "PROCESSING": ZeusState.THINKING,
            "SPEAKING": ZeusState.SPEAKING,
            "ERROR": ZeusState.ERROR,
        }
        zeus_state = state_map.get(state, ZeusState.IDLE)
        if hasattr(self, "state_controller") and self.state_controller:
            self.state_controller.set_state(zeus_state)

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
        enabled = bool(self.voice_switch and self.voice_switch.value)
        # Сохраняем состояние тем же маршрутом, что и настройки страницы.
        # Controller.update_settings применяет изменение к аудиодвижку без
        # перезапуска приложения.
        self._run_bg(
            self.controller.update_settings,
            "app_settings",
            "auto_listening",
            enabled,
        )
        if enabled:
            self._log("🎤 Автоматическое прослушивание включено")
        else:
            self._log("🎤 Автоматическое прослушивание выключено")

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
