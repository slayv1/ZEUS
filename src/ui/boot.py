# -*- coding: utf-8 -*-
"""Старт окна: сплэш, трей, горячие клавиши, жизненный цикл."""
from __future__ import annotations

import os
import queue
import sys
import threading
from typing import Any

import flet as ft

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_SRC_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import config
from core.config import THEMES
from core.file_watcher import start_watching, stop_watching
from modules.tray import TrayManager
from ui.shell_globals import C, PALETTES, SECTIONS
from ui.theme import get_theme_manager
from ui.zeus_state import ZeusState, ZeusStateController

try:
    from ui import core_card as _core_card
    from ui import memory_plugins as _memory_plugins
    from ui import neural_core as _neural_core
    from ui import sidebar as _sidebar
    from ui import system_panel as _system_panel
    from ui import terminal as _terminal
except ImportError:  # запуск из src/
    import core_card as _core_card  # type: ignore[no-redef]
    import memory_plugins as _memory_plugins  # type: ignore[no-redef]
    import neural_core as _neural_core  # type: ignore[no-redef]
    import sidebar as _sidebar  # type: ignore[no-redef]
    import system_panel as _system_panel  # type: ignore[no-redef]
    import terminal as _terminal  # type: ignore[no-redef]

try:
    from ui.audio_devices_panel import AudioDevicesPanel
except ImportError:
    from audio_devices_panel import AudioDevicesPanel  # type: ignore[no-redef]


class ZeusBootMixin:
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
                self.state_controller = self.controller.state_controller

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


