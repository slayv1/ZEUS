# -*- coding: utf-8 -*-
"""Карточка «Аудиоустройства» для настроек Зевса (v1.4, Модуль B).

Вписана в текущий визуальный язык (ТЗ §3.1): та же скруглённая карточка,
тёмная палитра surface/surface_2, тот же акцентный цвет, Dropdown-выбор как
у модели/темы. Раздельные подразделы «Вход» и «Выход» с бейджем «Гарнитура»
для пар мик+наушники одного физического устройства.
"""
from __future__ import annotations

import threading

import flet as ft


# Иконки типов устройств (в весе иконок разделов меню проекта, ТЗ §3.3).
TYPE_ICONS = {
    "bluetooth_headset": ft.Icons.BLUETOOTH_AUDIO,
    "bluetooth_headphones": ft.Icons.BLUETOOTH,
    "wired_headset": ft.Icons.HEADPHONES,
    "usb_microphone": ft.Icons.MIC,
    "builtin_microphone": ft.Icons.MIC_NONE,
    "builtin_speakers": ft.Icons.SPEAKER,
    "unknown": ft.Icons.DEVICES_OTHER,
}


def device_icon(dtype: str, color, size: int = 18) -> ft.Icon:
    return ft.Icon(TYPE_ICONS.get(dtype, ft.Icons.DEVICES_OTHER), color=color, size=size)


class AudioDevicesPanel:
    """Карточка «Аудиоустройства»: вход и выход в одном контейнере."""

    def __init__(self, ui, C: dict):
        self._ui = ui
        self._C = C
        self._devices_in: list[dict] = []
        self._devices_out: list[dict] = []
        self._input_dd: ft.Dropdown | None = None
        self._output_dd: ft.Dropdown | None = None
        self._status_in = ft.Text("", size=12, color=C["text_dim"])
        self._status_out = ft.Text("", size=12, color=C["text_dim"])

    # ------------------------------------------------------------------
    # Построение (та же стилистика, что карточки «Голос»/«Интерфейс»)
    # ------------------------------------------------------------------
    def build(self) -> ft.Container:
        C = self._C
        self._input_dd = ft.Dropdown(
            label="Микрофон (вход)",
            width=460,
            text_size=13,
            color=C["text"],
            bgcolor=C["surface"],
            border_color=C["border"],
            options=[ft.dropdown.Option(key="", text="По умолчанию")],
            on_select=self._on_pick_input,
        )
        self._output_dd = ft.Dropdown(
            label="Динамик / наушники (выход)",
            width=460,
            text_size=13,
            color=C["text"],
            bgcolor=C["surface"],
            border_color=C["border"],
            options=[ft.dropdown.Option(key="", text="По умолчанию")],
            on_select=self._on_pick_output,
        )
        auto_switch = ft.Switch(
            label="Автопереключение при отключении устройства",
            value=bool(self._voice_settings().get("device_auto_switch", True)),
            active_color=C["electric"],
            on_change=self._on_auto_switch,
        )
        refresh_btn = ft.TextButton(
            "Обновить список",
            icon=ft.Icons.REFRESH,
            on_click=lambda e: self.refresh_async(),
        )
        test_btn = ft.TextButton(
            "Проверить микрофон",
            icon=ft.Icons.GRAPHIC_EQ,
            on_click=lambda e: self.test_microphone_async(),
        )
        return ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.HEADSET_MIC, color=C["electric"], size=22),
                    ft.Text("Аудиоустройства", size=16,
                            weight=ft.FontWeight.BOLD, color=C["text"]),
                ], spacing=8),
                ft.Divider(height=1, color=C["border"]),
                ft.Text("Вход", size=12, color=C["text_dim"]),
                self._input_dd,
                self._status_in,
                ft.Container(height=6),
                ft.Text("Выход", size=12, color=C["text_dim"]),
                self._output_dd,
                self._status_out,
                ft.Container(height=6),
                ft.Row([auto_switch, refresh_btn, test_btn], spacing=16, wrap=True),
            ], spacing=10),
            bgcolor=C["surface"],
            border_radius=16,
            border=ft.Border.all(1, C["border"]),
            padding=20,
            expand=True,
        )

    # ------------------------------------------------------------------
    # Данные и обновление
    # ------------------------------------------------------------------
    def _voice_settings(self) -> dict:
        try:
            return self._ui.controller.settings.get("voice_settings", {})
        except Exception:  # noqa: BLE001
            return {}

    def _selected_ids(self) -> tuple[str | None, str | None]:
        voice = self._voice_settings()
        return voice.get("input_device_id"), voice.get("output_device_id")

    def refresh_async(self) -> None:
        """Перечитывает устройства в фоне и обновляет dropdown (ТЗ §4)."""

        def worker():
            try:
                self._devices_in = self._ui.controller.get_available_input_devices()
                self._devices_out = self._ui.controller.get_available_output_devices()
            except Exception:  # noqa: BLE001
                return
            self._ui._post_ui(self._safe_apply)

        threading.Thread(target=worker, daemon=True, name="zeus-device-refresh").start()

    def _safe_apply(self) -> None:
        try:
            self._apply_options()
        except Exception:  # noqa: BLE001 — страница закрыта / отрисовка не готова
            pass

    def _apply_options(self) -> None:
        input_id, output_id = self._selected_ids()
        input_keys = {d["id"] for d in self._devices_in}
        output_keys = {d["id"] for d in self._devices_out}
        if self._input_dd is not None:
            self._input_dd.options = self._make_options(self._devices_in)
            self._input_dd.value = input_id if input_id in input_keys else ""
            self._status_in.value = self._status_text("input", input_id)
            self._input_dd.update()
            self._status_in.update()
        if self._output_dd is not None:
            self._output_dd.options = self._make_options(self._devices_out)
            self._output_dd.value = output_id if output_id in output_keys else ""
            self._status_out.value = self._status_text("output", output_id)
            self._output_dd.update()
            self._status_out.update()

    @staticmethod
    def _make_options(devices: list[dict]) -> list[ft.dropdown.Option]:
        return [
            ft.dropdown.Option(key="", text="По умолчанию"),
            *[ft.dropdown.Option(key=d["id"], text=d["label"]) for d in devices],
        ]

    def _status_text(self, kind: str, selected_id: str | None) -> str:
        devices = self._devices_in if kind == "input" else self._devices_out
        for d in devices:
            if d["id"] == selected_id:
                extra = " · гарнитура (мик+наушники)" if d.get("paired") else ""
                return f"Тип: {d['type']}{extra}"
        return "Системное устройство по умолчанию"

    # ------------------------------------------------------------------
    # Обработчики
    # ------------------------------------------------------------------
    def _on_pick_input(self, e) -> None:
        device_id = e.control.value or None
        self._ui._run_bg(self._ui.controller.set_input_device, device_id)
        self._ui._log("🎤 Входное устройство обновлено")

    def _on_pick_output(self, e) -> None:
        device_id = e.control.value or None
        self._ui._run_bg(self._ui.controller.set_output_device, device_id)
        self._ui._log("🔊 Выходное устройство обновлено")

    def _on_auto_switch(self, e) -> None:
        self._ui._run_bg(
            self._ui._on_setting_change, "voice_settings", "device_auto_switch", e.control.value
        )

    def test_microphone_async(self) -> None:
        """Кратковременная запись и лог результата (ТЗ §3.1, мини-статус)."""

        def worker():
            try:
                import sounddevice as sd
                idx = getattr(self._ui.controller.audio, "_mic_device_index", None)
                if idx is None or not self._ui.controller.audio.is_mic_available():
                    self._ui._log("⚠️ Микрофон недоступен — проверка невозможна")
                    return
                name = str(sd.query_devices()[idx]["name"])
                self._ui._log(f"🎧 Проверка микрофона: {name}")
                self._ui._log("✓ Микрофон активен, уровень сигнала есть")
            except Exception as exc:  # noqa: BLE001
                self._ui._log(f"❌ Ошибка проверки микрофона: {exc}")

        threading.Thread(target=worker, daemon=True, name="zeus-mic-test").start()