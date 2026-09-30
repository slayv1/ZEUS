"""Тесты Neural Core UI (ТЗ Neural Core Interface §1-12).

Проверяют декомпозицию без запуска Flet-окна:
Sidebar / CoreCard / NeuralCore / SystemPanel+QuickActions+Recent /
Terminal / Memory+Plugins строятся из единого Design System,
SECTIONS идут в порядке ТЗ, backend-контракты целы.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import flet as ft

from ui.design_system import PALETTES, SIDEBAR_WIDTH, TERMINAL_HEIGHT
from ui import sidebar, core_card, neural_core, system_panel, terminal, memory_plugins
import src.ui.main_flet as mf


class TestDesignSystem:
    def test_dark_tokens(self):
        d = PALETTES["dark"]
        assert d["bg"] == "#050816"
        assert d["surface"] == "#0F172A"
        assert d["accent"] == "#2563EB"
        assert d["cyan"] == "#22D3EE"
        assert d["green"] == "#22C55E"
        # Ядро-сфера из референса Ref.jpg
        assert d["globe"] == "#187ECD"
        assert d["globe_ring"] == "#4DD2FF"

    def test_sidebar_width(self):
        assert SIDEBAR_WIDTH == 250

    def test_terminal_height(self):
        assert TERMINAL_HEIGHT == 220


class TestSections:
    def test_order(self):
        labels = [s["label"] for s in mf.SECTIONS]
        assert labels == ["Главная", "Системные логи", "Память", "Плагины", "Настройки"]

    def test_get_section_content_mapping(self):
        ui = mf.ZeusUI.__new__(mf.ZeusUI)
        ui.dashboard_content = "dash"
        ui.logs_content = "logs"
        ui.settings_content = "settings"
        ui._memory_content = lambda: "mem"
        ui._plugins_content = lambda: "plug"
        assert ui._get_section_content(0) == "dash"
        assert ui._get_section_content(1) == "logs"
        assert ui._get_section_content(2) == "mem"
        assert ui._get_section_content(3) == "plug"
        assert ui._get_section_content(4) == "settings"


class TestComponents:
    def _pal(self):
        return dict(PALETTES["dark"])

    def test_sidebar_build(self):
        p = self._pal()
        logo = sidebar.build_logo(p)
        assert isinstance(logo, ft.Row)
        item = sidebar.build_nav_item(ft.Icons.HOME_ROUNDED, "Главная", True, p)
        assert isinstance(item, ft.Container)
        footer = sidebar.build_status_footer(p)
        assert isinstance(footer, ft.Container)

    def test_core_card_size(self):
        card = core_card.build_core_card(self._pal())
        assert card.width == 300
        assert card.height == 250

    def test_neural_core_clamps_size(self):
        box = ft.Container()
        small = neural_core.build_neural_core(box, "a", "b", self._pal(), size=10)
        assert isinstance(small, ft.Column)
        big = neural_core.build_neural_core(box, "a", "b", self._pal(), size=9999)
        assert isinstance(big, ft.Column)

    def test_neural_core_globe_mode(self):
        """globe=True: центр — Stack с аурой, орбитами, спутниками (Ref.jpg)."""
        box = ft.Container()
        column = neural_core.build_neural_core(
            box, "Готов к работе", "Слушаю...", self._pal(), size=470, globe=True
        )
        assert isinstance(column, ft.Column)
        inner = column.controls[0]
        assert isinstance(inner, ft.Stack), "в globe-режиме должен быть Stack"
        assert len(inner.controls) >= 5, "аура + кольца + спутники + голограмма"
        # status-пилюля сохранена
        assert column.status_text is not None
        assert column.status_text.value == "Готов к работе"
        # дефолтный режим — без Stack
        plain = neural_core.build_neural_core(box, "a", "b", self._pal())
        assert not isinstance(plain.controls[0], ft.Stack)

    def test_core_card_transparent(self):
        p = self._pal()
        ghost = core_card.build_core_card(p, transparent=True)
        assert isinstance(ghost, ft.Column), "transparent возвращает Column без рамки"
        car = core_card.build_core_card(p)
        assert isinstance(car, ft.Container)
        assert car.width == 300 and car.height == 250

    def test_system_panel(self):
        p = _sys = system_panel.build_system_panel(self._pal())
        assert isinstance(p, ft.Container)
        assert p.width == 350
        assert p.height == 220
        qa = system_panel.build_quick_actions(self._pal())
        assert isinstance(qa, ft.Container)
        recent = system_panel.build_recent_commands(self._pal())
        assert isinstance(recent, ft.Container)

    def test_system_panel_transparent(self):
        p = self._pal()
        assert isinstance(system_panel.build_system_panel(p, transparent=True), ft.Column)
        assert isinstance(system_panel.build_quick_actions(p, transparent=True), ft.Column)
        assert isinstance(system_panel.build_recent_commands(p, transparent=True), ft.Column)

    def test_terminal_frame(self):
        col = ft.Column([])
        frame = terminal.build_terminal_frame(col, self._pal())
        assert isinstance(frame, ft.Container)
        line = terminal.build_log_line("17:42:01", "VOICE", "hello", self._pal())
        assert isinstance(line, ft.Row)

    def test_memory_plugins_pages(self):
        mem = memory_plugins.build_memory_page(self._pal())
        assert isinstance(mem, ft.Column)
        plug = memory_plugins.build_plugins_page(self._pal())
        assert isinstance(plug, ft.Column)

    def test_quick_action_mapping(self):
        ui = mf.ZeusUI.__new__(mf.ZeusUI)
        calls = []
        ui._run_bg = lambda fn, *a, **k: calls.append((fn, a))
        ui._on_listen_once = lambda *a, **k: None
        ui._on_quick_action("browser")
        ui._on_quick_action("vscode")
        ui._on_quick_action("unknown-key")
        assert len(calls) == 2
        assert calls[0][1] == ("открой браузер",)
        assert calls[1][1] == ("запусти vs code",)

    def test_recent_commands_fallback(self):
        ui = mf.ZeusUI.__new__(mf.ZeusUI)
        ui._session_lines = []
        items = ui._recent_commands()
        assert len(items) == 6
        assert items[0][1] == "Открыть браузер"

    def test_system_snapshot_keys(self):
        ui = mf.ZeusUI.__new__(mf.ZeusUI)
        snap = ui._system_snapshot()
        assert set(snap) == {"cpu", "ram", "disk"}
        for v in snap.values():
            assert 0.0 <= v <= 1.0
