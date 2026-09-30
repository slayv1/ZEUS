"""Тесты оптимизаций ZeusController (пул потоков, cleanup, update_settings)."""
import time
from unittest.mock import MagicMock, patch

from core.controller import execute_command_safely, ZeusController


def test_execute_command_safely_runs_in_background():
    ran = []

    def task():
        ran.append(True)

    execute_command_safely(task)
    time.sleep(0.1)
    assert ran == [True]


def test_controller_update_settings_new_key(monkeypatch, tmp_path):
    # Мокаем AudioEngine и ChatEngine чтобы не загружать модели Vosk/Ollama
    with patch("core.controller.AudioEngine"), patch("core.controller.ChatEngine"):
        ctrl = ZeusController()
        try:
            # Проверяем сохранение нового ключа в существующей или новой секции
            res = ctrl.update_settings("custom_section", "new_key", "test_val")
            assert res is True
            assert ctrl.settings.get("custom_section", {}).get("new_key") == "test_val"
        finally:
            ctrl.cleanup()


def test_controller_cleanup_stops_resources():
    with patch("core.controller.AudioEngine"), patch("core.controller.ChatEngine"):
        ctrl = ZeusController()
        assert ctrl._is_cleaned_up is False
        assert ctrl._executor is not None

        ctrl.cleanup()
        assert ctrl._is_cleaned_up is True
        # Проверяем, что stop_device_watcher вызывается безопасно
        ctrl.stop_device_watcher()
