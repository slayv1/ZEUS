"""Менеджер тем оформления (ТЗ v1.4 §7 + ТЗ Neural Core §12).

Каноническая палитра — в ui.design_system (единый DS).
Здесь — персистентность выбора темы + обратная совместимость API.
"""
from __future__ import annotations

import json
import os
import threading

try:
    from ui.design_system import PALETTES
except ImportError:  # запуск из src/
    from design_system import PALETTES  # type: ignore[no-redef]

# Обратная совместимость: прямые импорты DARK_PALETTE / LIGHT_PALETTE.
DARK_PALETTE: dict[str, str] = PALETTES["dark"]
LIGHT_PALETTE: dict[str, str] = PALETTES["light"]


def _theme_file_path() -> str:
    """Путь к файлу конфигурации темы (config/theme.json)."""
    # Ищем config/ относительно src/ (на уровень выше ui/)
    this_dir = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.dirname(this_dir)
    config_dir = os.path.join(src_dir, "config")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "theme.json")


class ThemeManager:
    """Менеджер тем (ТЗ v1.4 §7): set_theme / get_theme / toggle_theme."""

    def __init__(self, initial: str = "dark") -> None:
        self._lock = threading.Lock()
        self._theme: str = initial
        self._listeners: list[callable] = []
        self._load()

    def _load(self) -> None:
        path = _theme_file_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                theme = data.get("theme", "dark")
                if theme in PALETTES:
                    self._theme = theme
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self) -> None:
        path = _theme_file_path()
        try:
            from core.atomic_json import write_json_atomic
        except ImportError:  # запуск из src/
            from atomic_json import write_json_atomic  # type: ignore[no-redef]
        # Атомарно: theme.json переписывается при каждом переключении темы,
        # усечённый при краш файл означал бы молчаливый возврат к тёмной теме.
        write_json_atomic(path, {"theme": self._theme}, indent=2)

    def get_theme(self) -> str:
        """Получить текущую тему: 'dark' или 'light'."""
        with self._lock:
            return self._theme

    def get_palette(self) -> dict[str, str]:
        """Получить палитру текущей темы."""
        with self._lock:
            return dict(PALETTES[self._theme])

    def set_theme(self, theme: str) -> bool:
        """Установить тему. Возвращает True если тема изменилась."""
        if theme not in PALETTES:
            return False
        with self._lock:
            if theme == self._theme:
                return False
            self._theme = theme
            self._save()
            palette = dict(PALETTES[theme])
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(theme, palette)
            except Exception:
                pass
        return True

    def toggle_theme(self) -> str:
        """Переключить тему. Возвращает новую тему."""
        new = "light" if self.get_theme() == "dark" else "dark"
        self.set_theme(new)
        return new

    def on_change(self, callback: callable) -> None:
        """Подписаться: callback(theme_name, palette)."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: callable) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)


# Глобальный экземпляр (синглтон)
_theme_manager: ThemeManager | None = None


def get_theme_manager() -> ThemeManager:
    """Получить глобальный менеджер тем."""
    global _theme_manager
    if _theme_manager is None:
        _theme_manager = ThemeManager()
    return _theme_manager
