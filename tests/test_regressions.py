import tempfile

from core import actions, actions_search
from core.file_watcher import FileWatcher
from modules.audio_engine import ModelLoaderThread


def test_date_parser_does_not_capture_unrelated_today_question():
    assert actions._handle_time_date_command("что сегодня запустить") is None
    assert actions._handle_time_date_command("скажи сегодняшнюю дату") is not None


def test_drive_scan_response_uses_words_instead_of_substrings(monkeypatch):
    # monkeypatch — чтобы глобальное состояние не утекало в другие тесты
    monkeypatch.setattr(actions_search, "_pending_drive_scan", "E:")
    assert actions._handle_drive_scan_response("интернет")["action"] == "drive_scan_clarify"
    assert actions._handle_drive_scan_response("давай")["action"] == "drive_scan_clarify"


def test_file_watcher_can_restart_after_stop():
    with tempfile.TemporaryDirectory() as folder:
        watcher = FileWatcher()
        assert watcher.start(paths=[folder])
        watcher.stop()
        assert watcher.start(paths=[folder])
        watcher.stop()


def test_model_loader_reports_unavailable_vosk():
    class UnavailableWake:
        available = False

    class FakeEngine:
        wake = UnavailableWake()

    completed = []
    ModelLoaderThread(FakeEngine(), on_complete=completed.append).run()

    assert completed == [False]