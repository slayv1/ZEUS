"""Тесты конфигурации и разрешения путей к ресурсам (ТЗ v1.2: hologram.webp).

resource_path + app_base_dir — единственный способ доступа к ассетам.
"""
import os
import threading

from src.core.config import SettingsManager, resource_path, app_base_dir  # noqa: E402


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


def test_settings_manager_creates_and_persists(tmp_path):
    """Менеджер создаёт конфиг и сохраняет изменение сразу."""
    settings_path = tmp_path / "settings.json"
    manager = SettingsManager(settings_path)

    assert settings_path.exists()
    assert manager.get("app_settings", "auto_listening") is True

    assert manager.set("app_settings", "theme", "light") is True
    reloaded = SettingsManager(settings_path)
    assert reloaded.get("app_settings", "theme") == "light"


def test_settings_manager_serializes_parallel_updates(tmp_path):
    """Параллельные сохранения не повреждают JSON и не теряют ключи."""
    manager = SettingsManager(tmp_path / "settings.json")
    values = {f"path_{index}": f"./data/{index}" for index in range(8)}

    threads = [
        threading.Thread(target=manager.set, args=("paths", key, value))
        for key, value in values.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert manager.get_all()["paths"] | values == manager.get_all()["paths"]


def test_settings_manager_preserves_v13_flat_config(tmp_path):
    """Плоский конфиг v1.3 мигрирует в совместимую внутреннюю схему."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        '{"theme": "light", "auto_listening": false, '
        '"audio": {"input_device_index": 4}}',
        encoding="utf-8",
    )
    manager = SettingsManager(settings_path)
    settings = manager.get_all()

    assert settings["theme"] == "light"
    assert settings["app_settings"]["theme"] == "light"
    assert settings["auto_listening"] is False
    assert settings["app_settings"]["auto_listening"] is False
    assert settings["audio"]["input_device_index"] == 4


def test_settings_manager_repairs_corrupted_file(tmp_path):
    """Повреждённый settings.json заменяется безопасными дефолтами."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{broken", encoding="utf-8")

    manager = SettingsManager(settings_path)
    reloaded = SettingsManager(settings_path)

    assert manager.get("app_settings", "theme") == "dark"
    assert reloaded.get("app_settings", "auto_listening") is True

