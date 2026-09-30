"""Тесты статуса индекса и «Пересканировать сейчас» (ТЗ ZEUS_rescan_status_TZ.md §6).

FileScanner вызывается по-настоящему; изолируются только корень data/
(app_base_dir -> tmp_path) и синглтон get_scanner(), чтобы тесты не сканировали
реальные диски и не трогали data/index.json пользователя.
"""
import json
import time

import pytest

from core import config, file_scanner


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """data/ и index.json/index_meta.json живут во временной папке."""
    monkeypatch.setattr(config, "app_base_dir", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def drive_tree(isolated_data):
    """Эмулированный диск: одна папка с одной программой."""
    root = isolated_data / "drive"
    apps = root / "Apps"
    apps.mkdir(parents=True)
    (apps / "tool.exe").write_bytes(b"MZ")
    return str(root)


@pytest.fixture
def scanner(isolated_data, drive_tree, monkeypatch):
    """FileScanner на временном дереве + подмена синглтона для мостов."""
    instance = file_scanner.FileScanner(search_paths=[drive_tree])
    monkeypatch.setattr(file_scanner, "get_scanner", lambda: instance)
    return instance


def _wait_scan_finished(scanner_obj, timeout: float = 30.0) -> None:
    """Ждёт завершения фоновой индексации."""
    deadline = time.time() + timeout
    while scanner_obj.is_indexing() and time.time() < deadline:
        time.sleep(0.05)
    assert not scanner_obj.is_indexing(), "индексация не завершилась за 30 с"


# ----------------------------------------------------------------------
# Статистика до/после сканирования
# ----------------------------------------------------------------------
def test_stats_before_first_scan(scanner):
    stats = scanner.get_index_stats()

    assert stats["index_age_hours"] is None  # сканов не было — не врём «свежий»
    assert stats["is_fresh"] is False
    assert stats["is_indexing"] is False
    assert stats["total_files"] == 0


def test_rebuild_index_fills_stats_and_meta(scanner, isolated_data):
    result = scanner.rebuild_index(max_workers=1)
    assert result["success"] is True

    _wait_scan_finished(scanner)

    stats = scanner.get_index_stats()
    assert stats["total_files"] == 1
    assert stats["is_indexing"] is False
    assert isinstance(stats["index_age_hours"], float)
    assert 0 <= stats["index_age_hours"] < 0.1  # только что отсканировано
    assert stats["is_fresh"] is True

    meta_path = isolated_data / "data" / "index_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["last_scan_at"].endswith("Z")  # UTC ISO8601
    assert meta["total_files"] == 1


def test_new_instance_reads_persisted_index_and_meta(isolated_data, drive_tree):
    first = file_scanner.FileScanner(search_paths=[drive_tree])
    first.rebuild_index(max_workers=1)
    _wait_scan_finished(first)

    # «Перезапуск»: новый инстанс видит тот же индекс и мету
    second = file_scanner.FileScanner(search_paths=[drive_tree])
    stats = second.get_index_stats()

    assert stats["total_files"] == 1
    assert isinstance(stats["index_age_hours"], float)


def test_rebuild_index_guard_while_indexing(scanner):
    scanner._indexing = True  # эмулируем идущее сканирование
    try:
        result = scanner.rebuild_index()
    finally:
        scanner._indexing = False

    assert result["success"] is False
    assert "уже выполняется" in result["message"]


def test_corrupted_meta_is_silently_ignored(scanner, isolated_data):
    meta_path = isolated_data / "data" / "index_meta.json"
    meta_path.write_text("{broken", encoding="utf-8")

    stats = scanner.get_index_stats()

    assert stats["index_age_hours"] is None
    assert stats["is_fresh"] is False


# ----------------------------------------------------------------------
# Мосты контроллера (ТЗ rescan_status §4)
# ----------------------------------------------------------------------
def test_controller_bridges_delegate_to_scanner(scanner):
    from core.controller import ZeusController

    bridge = ZeusController.__new__(ZeusController)  # без тяжёлого __init__

    result = bridge.rebuild_file_index()
    assert isinstance(result, dict) and "success" in result
    _wait_scan_finished(scanner)

    stats = bridge.get_file_index_stats()
    assert isinstance(stats, dict)
    assert "is_indexing" in stats and "is_fresh" in stats
    assert stats["total_files"] == 1



