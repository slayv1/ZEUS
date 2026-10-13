# -*- coding: utf-8 -*-
"""Обработчики событий: голос, чат, настройки, состояние."""
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


class ZeusHandlersMixin:
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



    def _toggle_theme(self):
        """Мгновенно переключает Design Tokens и перестраивает UI.

        ТЗ v1.4 §7: также обновляет ThemeManager для нового окна.
        """
        new_key = "light" if self.page.theme_mode == ft.ThemeMode.DARK else "dark"
        if self.controller is not None:
            self._run_bg(
                self.controller.update_settings, "app_settings", "theme", new_key
            )
        C.clear()
        C.update(PALETTES[new_key])
        self.page.theme_mode = (
            ft.ThemeMode.LIGHT if new_key == "light" else ft.ThemeMode.DARK
        )
        # ТЗ v1.4 §7: синхронизация с ThemeManager
        if hasattr(self, "_theme_manager") and self._theme_manager:
            self._theme_manager.set_theme(new_key)
        self._rebuild_all()
        self.page.update()



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
            # Обновляем общую палитру (тот же словарь, что у layout и boot).
            C.clear()
            C.update(PALETTES[theme_key])
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
            def _on_speech(speaking: bool, ui=self, engine=ctrl) -> None:
                engine._on_speaking_changed(speaking)
                ui.update_assistant_state(speaking)
            ctrl.audio.on_speaking_changed = _on_speech
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
                elif state == "SPEAKING":
                    led.bgcolor = C.get("accent", "#00E5FF")
                    led.shadow = ft.BoxShadow(
                        blur_radius=6, color=C.get("accent", "#00E5FF"), spread_radius=0
                    )
                    if dot_label is not None:
                        dot_label.value = "Говорю..."
                        dot_label.color = C.get("accent", "#00E5FF")
                elif state == "ERROR":
                    led.bgcolor = "#FF5A5A"
                    led.shadow = ft.BoxShadow(blur_radius=6, color="#FF5A5A", spread_radius=0)
                    if dot_label is not None:
                        dot_label.value = "Ошибка"
                        dot_label.color = "#FF5A5A"
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
                elif state == "SPEAKING":
                    btn.icon_color = C.get("accent", "#00E5FF")
                    btn.tooltip = "Говорю..."
                    self._on_status("Говорю...")
                elif state == "ERROR":
                    btn.icon_color = "#FF5A5A"
                    btn.tooltip = "Ошибка"
                    self._on_status("Ошибка")
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


