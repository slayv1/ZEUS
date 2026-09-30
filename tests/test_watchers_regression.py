# -*- coding: utf-8 -*-
"""Регрессионные тесты аудита: сканер индекса, вотчеры, погода.

Покрывают найденные при ревью баги:
- file_scanner.save_index: атомарная запись (tmp + replace) — раньше прямая
  запись через open("w") при обрыве оставляла пустой/битый index.json;
- file_watcher: события удалённых папок (event.is_directory вместо
  os.path.isdir постфактум) и recent_events(limit<=0);
- watch_watchdog: устойчивый вывод в cp1251-stdout (эмодзи не роняет CLI)
  и фильтр директорий по флагу события;
- actions: обращения Vosk («офицер», «саша» из логов 22.09) не попадают
  в город;
- index_watcher.IndexEditor: upsert/remove и сохранение.
"""
import io
import json
import os
from types import SimpleNamespace

import pytest

from core import actions, file_scanner, file_watcher, watch_watchdog


# === file_scanner.save_index: атомарность ===

def test_save_index_atomic_writes_valid_json(tmp_path, monkeypatch):
    target = tmp_path / "index.json"
    monkeypatch.setattr(file_scanner, "_index_path", lambda: str(target))

    index = {"app.exe": {"path": r"C:\app.exe", "source": "internal"}}
    file_scanner.save_index(index)

    assert json.loads(target.read_text(encoding="utf-8")) == index
    assert not (tmp_path / "index.json.tmp").exists()  # tmp заменён на файл


def test_save_index_keeps_old_file_on_write_failure(tmp_path, monkeypatch):
    """Сбой os.replace не должен уничтожить существующий индекс."""
    target = tmp_path / "index.json"
    old = {"old.exe": {"path": r"C:\old.exe", "source": "internal"}}
    target.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(file_scanner, "_index_path", lambda: str(target))

    def _fail_replace(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(file_scanner.os, "replace", _fail_replace)
    file_scanner.save_index({"new.exe": {"path": r"C:\n.exe", "source": "internal"}})

    # Старый файл цел (новый остался во временном tmp — основной не тронут)
    assert json.loads(target.read_text(encoding="utf-8")) == old


# === file_watcher: удалённые папки и лимиты ===

def test_deleted_directory_event_is_emitted(tmp_path):
    """Удалённая папка не должна отсекаться фильтром *.json.

    Раньше _matches/os.path.isdir(path) постфактум возвращал False
    (папки уже нет на диске) и событие терялось.
    """
    captured: list[dict] = []
    handler = file_watcher.ProjectFileHandler(captured.append, patterns=["*.json"])
    ghost_dir = str(tmp_path / "vanished_dir")
    assert not os.path.exists(ghost_dir)  # папки действительно нет

    handler.on_deleted(SimpleNamespace(
        src_path=ghost_dir, dest_path=None, is_directory=True,
    ))

    assert len(captured) == 1
    assert captured[0]["event"] == "deleted"
    assert captured[0]["is_directory"] is True  # флаг из события, не с диска


def test_deleted_nonmatching_file_still_filtered(tmp_path):
    captured: list[dict] = []
    handler = file_watcher.ProjectFileHandler(captured.append, patterns=["*.json"])
    handler.on_deleted(SimpleNamespace(
        src_path=str(tmp_path / "notes.txt"), dest_path=None, is_directory=False,
    ))
    assert captured == []  # обычный файл не по паттерну — фильтруется


def test_deleted_json_file_is_emitted(tmp_path):
    captured: list[dict] = []
    handler = file_watcher.ProjectFileHandler(captured.append, patterns=["*.json"])
    handler.on_deleted(SimpleNamespace(
        src_path=str(tmp_path / "settings.json"), dest_path=None, is_directory=False,
    ))
    assert len(captured) == 1
    assert captured[0]["event"] == "deleted"


def test_recent_events_nonpositive_limit_returns_empty():
    watcher = file_watcher.FileWatcher()
    watcher._dispatch({"event": "created", "path": "x.json",
                       "src_path": "x.json", "dest_path": None})
    # Раньше recent_events(0) возвращал ВЕСЬ лог (срез [-0:])
    assert watcher.recent_events(0) == []
    assert watcher.recent_events(-1) == []
    assert len(watcher.recent_events(5)) == 1


# === watch_watchdog: cp1251-вывод и фильтр директорий ===

def test_watch_watchdog_out_survives_cp1251_stream():
    """Эмодзи в перенаправленном (cp1251) stdout не роняет мониторинг."""
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp1251", errors="strict", newline="")

    watch_watchdog._out("📁 создана папка", file=stream)  # раньше: UnicodeEncodeError

    stream.flush()
    data = buf.getvalue().decode("cp1251")
    assert "создана папка" in data  # кириллица дошла
    assert "📁" not in data          # эмодзи заменено, а не исключение


def test_watch_watchdog_matches_deleted_directory_via_flag(tmp_path):
    handler = watch_watchdog.PrintHandler(["*.json"])
    ghost = str(tmp_path / "some_dir")
    # Удалённая папка (диска уже нет) должна проходить по флагу события
    assert handler._matches(ghost, is_directory=True) is True
    # Файлы фильтруются паттернами как раньше
    assert handler._matches(str(tmp_path / "a.txt"), is_directory=False) is False
    assert handler._matches(str(tmp_path / "a.json"), is_directory=False) is True


# === actions: обращения не должны попадать в город (логи 22.09) ===

@pytest.mark.parametrize(
    ("phrase", "city"),
    [
        ("офицер какая погода в душанбе", "душанбе"),   # лог 22.09 15:25:35
        ("саша какая погода в москве", "москве"),       # лог 22.09 15:25:53
        ("да сэр какая погода в сочи", "сочи"),
        ("какая погода в душанбе", "душанбе"),
        ("погода в Москве", "москве"),
        ("погода в Санкт-Петербурге", "санкт-петербурге"),
    ],
)
def test_weather_city_strips_vocatives(phrase, city):
    assert actions._extract_weather_city(phrase) == city


def test_weather_city_keeps_names_containing_vocative_substrings():
    # \b-защита: «да» не режет «даугавпилс», «ну» — «нур-султан»
    assert actions._extract_weather_city("погода в даугавпилсе") == "даугавпилсе"
    assert actions._extract_weather_city("погода в нур-султане") == "нур-султане"


# === index_watcher.IndexEditor: операции и сохранение ===

def test_index_editor_roundtrip(tmp_path, monkeypatch):
    from core import index_watcher

    target = tmp_path / "index.json"
    monkeypatch.setattr(file_scanner, "_index_path", lambda: str(target))

    editor = index_watcher.IndexEditor()  # reload из (пустого) tmp
    assert editor.entries_count() == 0

    exe = tmp_path / "prog.exe"
    exe.write_bytes(b"MZ")
    assert editor.upsert(str(exe)) is True
    assert editor.entries_count() == 1
    assert editor.remove(str(exe)) is True
    assert editor.entries_count() == 0

    # Не .exe — не индексируется
    assert editor.upsert(str(tmp_path / "a.txt")) is False

    # Сбрасываем отложенную запись ПОКА путь ещё подменён, иначе таймер
    # (1 с) после отмены monkeypatch сохранил бы в реальный data/index.json.
    editor.flush()
    if editor._timer is not None:
        editor._timer.cancel()
