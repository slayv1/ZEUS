# -*- mode: python ; coding: utf-8 -*-
"""Spec для сборки «Зевс» в .exe через PyInstaller.

Сборка:  pyinstaller zeus.spec --noconsole
Отладка: pyinstaller zeus.spec  (с консолью)

Ресурсы:
  - data/   — конфигурации (commands.json, settings.json и т.д.)
  - models/ — Vosk-модель + TTS кладутся рядом с exe скриптом build_release.bat
data/ копируется рядом с exe (frozen-aware config.data_dir() ищет её там же).

DLL Vosk и espeak-ng-data Piper ищутся в установленном пакете, а не по
жёсткому пути Python314 в APPDATA.
"""
import importlib.util
import os

from PyInstaller.utils.hooks import collect_all

block_cipher = None

flet_datas, flet_bins, flet_hidden = collect_all("flet")


def _package_dir(name: str) -> str:
    """Каталог установленного пакета или пустая строка, если его нет."""
    spec = importlib.util.find_spec(name)
    origin = getattr(spec, "origin", None) if spec is not None else None
    if not origin or origin == "namespace":
        return ""
    return os.path.dirname(os.path.abspath(origin))


# Нативные DLL Vosk (libvosk.dll и MinGW-зависимости) не подхватываются
# автоанализом — добавляем вручную в _internal/vosk/.
vosk_pkg = _package_dir("vosk")
_VOSK_DLLS = (
    "libvosk.dll",
    "libgcc_s_seh-1.dll",
    "libstdc++-6.dll",
    "libwinpthread-1.dll",
)
vosk_bins = [
    (os.path.join(vosk_pkg, dll), "vosk")
    for dll in _VOSK_DLLS
    if vosk_pkg and os.path.exists(os.path.join(vosk_pkg, dll))
]

# Piper TTS: espeak-ng-data ищется относительно модуля piper -> _internal/piper/
piper_pkg = _package_dir("piper")
piper_espeak = os.path.join(piper_pkg, "espeak-ng-data") if piper_pkg else ""
piper_datas = (
    [(piper_espeak, os.path.join("piper", "espeak-ng-data"))]
    if piper_espeak and os.path.isdir(piper_espeak)
    else []
)

a = Analysis(
    ["main.py"],
    pathex=[os.path.join(SPECPATH, "src")],
    binaries=flet_bins + vosk_bins,
    # ВНИМАНИЕ: models/ НЕ включаем в datas — из-за этого в сборке появлялась
    # вторая копия моделей в _internal/models (+147 МБ). И Vosk (wake_word:
    # app_base_dir()/models), и Piper (config.resource_path) ищут модели РЯДОМ
    # С .exe — туда их кладёт build_release.bat (robocopy models). Если собираете
    # вручную без бата — скопируйте models/ рядом с Zeus.exe, иначе Vosk скачает
    # модель (~88 МБ) при первом запуске, а Piper-голос будет недоступен.
    datas=flet_datas + [
        ("data", "data"),
        ("assets", "assets"),   # иконки + центральная анимация hologram.webp
    ] + piper_datas,
    hiddenimports=flet_hidden + [
        "core.crash_handler",
        "core.command_router",
        "core.command_hygiene",
        "core.agent_harness",
        "core.agent_tools",
        "core.atomic_json",
        "core.audio_gate",
        "core.audio_devices",
        "core.auto_scanner",
        "modules.taobao_core",
        "modules.taobao_translator",
        "keyboard",
        "pystray",
        "vosk",
        "sounddevice",
        "psutil",
        "requests",
        "deep_translator",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy.tests"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Zeus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # отладка нативного краша
    icon="assets/icons/app_icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Zeus",
)
