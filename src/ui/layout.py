# -*- coding: utf-8 -*-
"""Сборка экранов: сайдбар, главная, логи, настройки, голограмма."""
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


class ZeusLayoutMixin:
    def _build_ui(self):
        page = self.page
        saved_settings = self.controller.get_settings() if self.controller else {}
        saved_app_settings = saved_settings.get("app_settings", {})
        saved_theme = saved_app_settings.get(
            "theme", saved_settings.get("theme", "dark")
        )
        C.clear()
        C.update(PALETTES["light" if saved_theme == "light" else "dark"])
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

        # Заголовок контентной части (вместо AppBar — ТЗ п.4.2).
        # Живой экран один: Neural Core.
        self.page_title = ft.Text(
            SECTIONS[0]["label"],
            size=32,
            weight=ft.FontWeight.BOLD,
            color=C["text"],
        )
        self._page_subtitle = ft.Text(
            "Ваш личный AI-ассистент. Всегда рядом.",
            size=13,
            color=C["text_dim"],
        )

        self._title_row = ft.Row(
            [
                ft.Column(
                    [self.page_title, self._page_subtitle],
                    spacing=2,
                ),
                ft.Container(expand=True),
                self.sleep_button,
                self.listen_button,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
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
        logo_block = _sidebar.build_logo(C)
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
            icon=ft.Icons.GRAPHIC_EQ_ROUNDED,
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
                    online=True,
                ),
            ],
            spacing=6,
            width=300,
        )

        # Центр — светящаяся сфера с подписью ZEUS / NEURAL CORE.
        center = _neural_core.build_neural_core(
            self.holo_container, "Готов к работе", "Слушаю...", C, size=440, globe=True,
        )
        self._neural_status_text = center.status_text
        self._neural_sub_text = center.sub_text

        snap = self._system_snapshot()
        # Правая колонка — System Status + Быстрые действия + Последние команды.
        # Адаптивная ширина: min 240px, expand 1 (растягивается).
        right_panel = ft.Column(
            [
                _system_panel.build_system_panel(
                    C, cpu=snap["cpu"], ram=snap["ram"], disk=snap["disk"],
                ),
                _system_panel.build_quick_actions(
                    C, on_action=self._on_quick_action,
                ),
                _system_panel.build_recent_commands(
                    C, items=self._recent_commands(limit=5),
                ),
            ],
            spacing=12,
            width=340,
        )
        self._right_column = right_panel
        self._left_panel = left_panel
        self._center = center
        self._right_panel = right_panel

        # Адаптивный layout: 3 колонки на широких экранах, 1 колонка на узких
        page_w = self.page.window.width if self.page and self.page.window else 1380
        main_row = self._build_adaptive_main_row(left_panel, center, right_panel, page_w)

        controls = ft.Container(
            content=ft.Column(
                [
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

        # --- Агентный каркас (Harness, ТЗ v1.4) ---
        # Тумблеры применяются мгновенно через controller.update_settings ->
        # chat.set_agent_config: пользователь щёлкает и следующий же запрос
        # к модели идёт с инструментами (или без, если выключили).
        agent_switch = ft.Switch(
            label="Агентный режим (инструменты: запуск, файлы, клики, поиск)",
            value=bool(ai.get("agent_enabled", False)),
            active_color=C["electric"],
            tooltip="Модель не просто отвечает текстом: она вызывает "
                    "инструменты — запускает приложения, читает файлы, "
                    "кликает, ищет в сети.",
            on_change=lambda e: self._on_setting_change(
                "ai_settings", "agent_enabled", bool(e.control.value)
            ),
        )
        self._settings_widgets["agent_switch"] = agent_switch

        agent_write_switch = ft.Switch(
            label="Разрешить инструментам запись файлов",
            value=bool(ai.get("agent_allow_write", False)),
            active_color=C["electric"],
            tooltip="Без этого тумблера write_file возвращает отказ модели. "
                    "Системные папки (System32, Temp...) запрещены всегда.",
            on_change=lambda e: self._on_setting_change(
                "ai_settings", "agent_allow_write", bool(e.control.value)
            ),
        )
        self._settings_widgets["agent_write_switch"] = agent_write_switch

        agent_actions_switch = ft.Switch(
            label="Разрешить запуск программ, ссылки и клики",
            value=bool(ai.get("agent_allow_actions", False)),
            active_color=C["electric"],
            tooltip="Без этого тумблера модель может искать в сети и читать "
                    "файлы, но не запускает программы, не открывает ссылки "
                    "и не кликает мышью.",
            on_change=lambda e: self._on_setting_change(
                "ai_settings", "agent_allow_actions", bool(e.control.value)
            ),
        )
        self._settings_widgets["agent_actions_switch"] = agent_actions_switch

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
                ft.Row([agent_switch], spacing=12),
                ft.Row([agent_write_switch], spacing=12),
                ft.Row([agent_actions_switch], spacing=12),
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

        # --- Шумовой гейт (ТЗ v1.4 §2.2) ---
        # Отсекает гул кулеров и клики клавиатуры ДО распознавания — иначе Vosk
        # рождает «команды-призраки» из шума. Порог применяется на лету
        # (controller._apply_noise_gate_settings), результат виден сразу.
        gate_enabled = ft.Switch(
            label="Шумовой гейт (отсекать клавиатуру и гул)",
            value=bool(voice.get("noise_gate_enabled", True)),
            active_color=C["electric"],
            tooltip="Пока Зевс ждёт слово «Зевс», фоновый шум не попадает в "
                    "распознавание. Выключайте, только если ассистент не слышит "
                    "тихую речь: вместо шума в логе появится «[AUDIO] [GATE]».",
            on_change=lambda e: self._on_setting_change(
                "voice_settings", "noise_gate_enabled", e.control.value
            ),
        )
        self._settings_widgets["noise_gate_enabled"] = gate_enabled

        wake_gate_slider = ft.Slider(
            label="{value}",
            min=0.0,
            max=150.0,
            divisions=30,
            value=float(voice.get("wake_energy_threshold", 30) or 0),
            width=240,
            active_color=C["electric"],
            inactive_color=C["surface_2"],
            on_change=lambda e: self._on_setting_change(
                "voice_settings", "wake_energy_threshold", int(e.control.value)
            ),
        )
        cmd_gate_slider = ft.Slider(
            label="{value}",
            min=0.0,
            max=150.0,
            divisions=30,
            value=float(voice.get("command_energy_threshold", 25) or 0),
            width=240,
            active_color=C["electric"],
            inactive_color=C["surface_2"],
            on_change=lambda e: self._on_setting_change(
                "voice_settings", "command_energy_threshold", int(e.control.value)
            ),
        )
        self._settings_widgets["wake_gate_slider"] = wake_gate_slider
        self._settings_widgets["cmd_gate_slider"] = cmd_gate_slider

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
                ft.Divider(height=1, color=C["border"]),
                ft.Row([gate_enabled], spacing=12),
                ft.Row([
                    wake_gate_slider,
                    ft.Text("порог в ожидании «Зевс»", size=12, color=C["text_dim"]),
                ], spacing=12),
                ft.Row([
                    cmd_gate_slider,
                    ft.Text("порог в режиме команды", size=12, color=C["text_dim"]),
                ], spacing=12),
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


