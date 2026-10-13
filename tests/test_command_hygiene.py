# -*- coding: utf-8 -*-
"""Фильтр мусорных команд: pip/git/установщики не попадают в базу."""
from core.command_hygiene import is_noise_executable, is_noise_game, prune_commands


def test_служебные_деревья_это_мусор():
    assert is_noise_executable(r"C:\Python314\Lib\site-packages\pip\_vendor\distlib\t64.exe")
    assert is_noise_executable(r"C:\Python314\Scripts\pip.exe")
    assert is_noise_executable(
        r"C:\Users\admin\.cache\codex-runtimes\git\mingw64\bin\git.exe"
    )
    assert is_noise_executable(
        r"C:\Projects\Game\Library\PackageCache\burst\bcl.exe"
    )


def test_настоящие_программы_остаются():
    assert is_noise_executable(r"C:\Program Files\Google\Chrome\Application\chrome.exe") is False
    assert is_noise_executable(r"C:\Python314\python.exe") is False
    assert is_noise_executable(r"D:\Игры\Control\Control.exe") is False
    assert is_noise_executable("steam://rungameid/730") is False
    assert is_noise_executable(
        r"C:\Users\admin\AppData\Local\Programs\Microsoft VS Code\Code.exe"
    ) is False


def test_кэши_редакторов_это_мусор():
    assert is_noise_executable(
        r"C:\Users\admin\.vscode\extensions\ms-python\debugpy\vsdbg.exe"
    )
    assert is_noise_executable(r"C:\Users\admin\.nuget\packages\app\apphost.exe")
    assert is_noise_executable(r"C:\Users\admin\.codex\plugins\codex.exe")
    assert is_noise_executable(r"C:\Users\admin\.lmstudio\bin\lms.exe")
    assert is_noise_executable(r"C:\Users\admin\.flet\client\flet\flet.exe")


def test_установщики_по_имени():
    assert is_noise_executable(r"C:\Users\admin\Downloads\VSCodeUserSetup-x64.exe")
    assert is_noise_executable(r"C:\Redist\directx_Jun2010_redist.exe")
    assert is_noise_executable(r"C:\Users\admin\Downloads\AnyDesk.exe") is False


def test_служебные_игры_steam():
    assert is_noise_game("Steamworks Common Redistributables")
    assert is_noise_game("Counter-Strike 2") is False


def test_prune_снимает_только_мусор():
    data = {
        "приложения": {
            "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "pip": r"C:\Python314\Scripts\pip.exe",
            "python": r"C:\Python314\python.exe",
        },
        "игры": {
            "counter-strike 2": {"name": "Counter-Strike 2", "id": "730"},
            "steamworks common redistributables": {
                "name": "Steamworks Common Redistributables",
                "id": "228980",
            },
        },
    }
    removed = prune_commands(data)
    assert set(removed) == {"pip", "steamworks common redistributables"}
    assert set(data["приложения"]) == {"chrome", "python"}
    assert set(data["игры"]) == {"counter-strike 2"}
