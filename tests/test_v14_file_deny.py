"""Функциональные тесты deny-листа сканера файлов (ТЗ ZEUS_deny_list_TZ.md §5).

Логика config.SettingsManager / file_scanner.build_index вызывается
по-настоящему (без моков); изолируется только путь settings.json — реальные
настройки пользователя не читаются и не перезаписываются.
"""
import os

import pytest

from core import config, file_scanner


@pytest.fixture
def deny_tree(tmp_path):
    """Эмулированное дерево папок: обычная + системные + персональная."""
    base = tmp_path / "apps"
    good = base / "Tools" / "sub"
    good.mkdir(parents=True)
    (good / "good_app.exe").write_bytes(b"MZ")

    # Регистронезависимость: NODE_MODULES записан большими буквами
    for name in ("NODE_MODULES", "TEMP", "Cache", ".git"):
        folder = base / name
        folder.mkdir()
        (folder / f"{name.lower()}.exe").write_bytes(b"MZ")

    personal = tmp_path / "personal_projects"
    (personal / "notes").mkdir(parents=True)
    (personal / "secret.exe").write_bytes(b"MZ")
    (personal / "notes" / "inner.exe").write_bytes(b"MZ")
    return base, personal


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """settings.json во временной папке (реальный data/settings.json не трогаем)."""
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", str(settings_path))
    return settings_path


def test_settings_manager_persists_custom_deny_paths(isolated_settings, deny_tree):
    _, personal = deny_tree
    deny_path = str(personal)

    assert config.SettingsManager(isolated_settings).set(
        "scanner_settings", "custom_deny_paths", [deny_path]
    ) is True

    # «Перезапуск»: новый инстанс читает тот же файл
    reloaded = config.SettingsManager(isolated_settings)
    assert reloaded.get("scanner_settings", "custom_deny_paths") == [deny_path]
    defaults = reloaded.get("scanner_settings", "deny_dir_names")
    assert isinstance(defaults, list)
    assert "node_modules" in defaults and "Temp" in defaults


def test_load_deny_lists_merges_system_defaults_and_custom_paths(isolated_settings, deny_tree):
    _, personal = deny_tree
    config.SettingsManager(isolated_settings).set(
        "scanner_settings", "custom_deny_paths", [str(personal)]
    )

    excluded_names, custom_deny = file_scanner._load_deny_lists()

    assert "windows" in excluded_names and "system32" in excluded_names  # системный список
    assert "node_modules" in excluded_names and "temp" in excluded_names  # deny_dir_names -> lower
    assert custom_deny == [os.path.normpath(str(personal))]


def test_is_denied_path_exact_prefix_and_empty(deny_tree):
    _, personal = deny_tree
    deny_path = str(personal)

    assert file_scanner._is_denied_path(deny_path, [deny_path]) is True
    assert file_scanner._is_denied_path(str(personal / "notes"), [deny_path]) is True  # вложенная
    assert file_scanner._is_denied_path(str(personal.parent / "other"), [deny_path]) is False
    assert file_scanner._is_denied_path(deny_path, []) is False


def test_build_index_excludes_system_and_custom_paths(isolated_settings, deny_tree):
    base, personal = deny_tree
    config.SettingsManager(isolated_settings).set(
        "scanner_settings", "custom_deny_paths", [str(personal)]
    )

    index = file_scanner.build_index([str(base)], max_workers=2)

    assert "good_app.exe" in index  # обычная папка индексируется
    assert "node_modules.exe" not in index  # NODE_MODULES — регистронезависимо
    assert "temp.exe" not in index
    assert "cache.exe" not in index
    assert ".git.exe" not in index


def test_build_index_returns_empty_when_root_is_denied(isolated_settings, deny_tree):
    _, personal = deny_tree
    config.SettingsManager(isolated_settings).set(
        "scanner_settings", "custom_deny_paths", [str(personal)]
    )

    assert file_scanner.build_index([str(personal)], max_workers=2) == {}


def test_old_settings_without_scanner_section_gets_defaults(tmp_path, monkeypatch):
    old_settings = tmp_path / "old_settings.json"
    old_settings.write_text('{"app_settings": {"theme": "light"}}', encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", str(old_settings))

    defaults = config.SettingsManager(old_settings).get("scanner_settings", "deny_dir_names")

    assert isinstance(defaults, list) and defaults  # дефолты подставлены, не падает


