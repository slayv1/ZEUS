# -*- coding: utf-8 -*-
"""Сканер Steam читает libraryfolders.vdf с табами, а не литерал \\s."""
from core.auto_scanner import (
    _drop_uninstalled_steam,
    _game_aliases,
    _vdf_value,
    app_track_state,
)
from core.actions_launch import find_best_game_or_app


VDF = (
    '"libraryfolders"\n'
    '{\n'
    '\t"1"\n'
    '\t{\n'
    '\t\t"path"\t\t"D:\\\\SteamLibrary"\n'
    '\t}\n'
    '}\n'
    '"AppState"\n'
    '{\n'
    '\t"appid"\t\t"292030"\n'
    '\t"name"\t\t"The Witcher 3: Wild Hunt — Remastered"\n'
    '}\n'
)


def test_vdf_читает_поля_через_табы():
    assert _vdf_value(VDF, "appid") == "292030"
    assert _vdf_value(VDF, "name").startswith("The Witcher 3")
    assert _vdf_value(VDF, "path") == "D:\\\\SteamLibrary"


def test_скачанная_и_качающаяся_игра_попадают_в_базу():
    assert app_track_state("4", "100", "100") == "installed"
    assert app_track_state("1048576", "1000", "10") == "downloading"
    assert app_track_state("4", "500", "10") == "downloading"
    assert app_track_state("1", "0", "0") is None
    assert app_track_state(None, None, None) == "installed"


def test_снятая_игра_уходит_из_базы():
    games = {
        "marvel rivals": {"name": "Marvel Rivals", "type": "steam", "id": "2767030"},
        "counter-strike 2": {"name": "Counter-Strike 2", "type": "steam", "id": "730"},
        "заметки": {"name": "заметки", "type": "file", "path": "notes.txt"},
    }
    removed = _drop_uninstalled_steam(games, {"730"})
    assert removed == ["Marvel Rivals"]
    assert "marvel rivals" not in games
    assert "counter-strike 2" in games
    assert "заметки" in games


def test_похожие_игры_не_делят_одни_алиасы():
    legacy = _game_aliases("Grand Theft Auto V Legacy")
    enhanced = _game_aliases("Grand Theft Auto V Enhanced")
    assert "гта" in legacy
    assert "гта энхансед" in enhanced
    assert "гта энхансед" not in legacy


def test_ведьмак_находится_по_русскому_имени():
    aliases = _game_aliases("The Witcher 3: Wild Hunt — Remastered")
    assert "ведьмак 3" in aliases
    commands = {
        "приложения": {},
        "игры": {
            "the witcher 3: wild hunt — remastered": {
                "name": "The Witcher 3: Wild Hunt — Remastered",
                "type": "steam",
                "id": "292030",
                "aliases": aliases,
            }
        },
    }
    for phrase in ("ведьмак 3", "запусти ведьмак", "witcher 3"):
        found = find_best_game_or_app(phrase, commands)
        assert found is not None, phrase
        assert found[1]["id"] == "292030"
