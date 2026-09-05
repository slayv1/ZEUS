"""Тесты конфигурации и разрешения путей к ресурсам (ТЗ v1.2: hologram.webp).

resource_path + app_base_dir — единственный способ доступа к ассетам.
"""
import os

from src.core.config import resource_path, app_base_dir  # noqa: E402


def test_resource_path_returns_existing_hologram():
    """resource_path должен вернуть реальный путь к hologram.webp."""
    p = resource_path("assets/animations/hologram.webp")
    assert p is not None
    assert os.path.exists(p), f"hologram.webp not found at {p}"
    assert os.access(p, os.R_OK), f"hologram.webp not readable"


def test_resource_path_icons_exist():
    """Иконки приложения должны быть доступны через resource_path."""
    for ic in ("app_icon.ico", "logo_1024.png"):
        p = resource_path(f"assets/icons/{ic}")
        assert os.path.exists(p), f"{ic} missing"


def test_assets_animations_dir_exists():
    """Папка assets/animations/ должна существовать в корне проекта."""
    animations_dir = os.path.join(app_base_dir(), "assets", "animations")
    assert os.path.isdir(animations_dir)
    files = os.listdir(animations_dir)
    assert any(f.endswith(".webp") for f in files), "hologram.webp missing"

