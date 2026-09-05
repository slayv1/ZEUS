# -*- mode: python ; coding: utf-8 -*-
"""Spec РґР»СЏ СЃР±РѕСЂРєРё В«Р—РµРІСЃ v1.2В» РІ .exe С‡РµСЂРµР· PyInstaller.

РЎР±РѕСЂРєР°:  pyinstaller zeus.spec --noconsole
РћС‚Р»Р°РґРєР°: pyinstaller zeus.spec  (СЃ РєРѕРЅСЃРѕР»СЊСЋ)

Р РµСЃСѓСЂСЃС‹:
  - data/   вЂ” РєРѕРЅС„РёРіСѓСЂР°С†РёРё (commands.json, settings.json Рё С‚.Рґ.)
  - models/ вЂ” Vosk РјРѕРґРµР»СЊ + TTS
data/ РєРѕРїРёСЂСѓРµС‚СЃСЏ СЂСЏРґРѕРј СЃ exe (frozen-aware config.data_dir() РёС‰РµС‚ РµС‘ С‚Р°Рј Р¶Рµ).
"""
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

flet_datas, flet_bins, flet_hidden = collect_all("flet")

# РќР°С‚РёРІРЅС‹Рµ DLL Vosk (libvosk.dll Рё MinGW-Р·Р°РІРёСЃРёРјРѕСЃС‚Рё) РЅРµ РїРѕРґС…РІР°С‚С‹РІР°СЋС‚СЃСЏ
# Р°РІС‚РѕР°РЅР°Р»РёР·РѕРј вЂ” РґРѕР±Р°РІР»СЏРµРј РІСЂСѓС‡РЅСѓСЋ РІ РєРѕСЂРµРЅСЊ _internal (СЂСЏРґРѕРј СЃ vosk_cffi).
vosk_pkg = os.path.join(
    os.environ.get("APPDATA", ""), "Python", "Python314", "site-packages", "vosk"
)
vosk_bins = [
    # Vosk РіСЂСѓР·РёС‚ DLL РёР· СЃРѕР±СЃС‚РІРµРЅРЅРѕР№ РїРѕРґРїР°РїРєРё РїР°РєРµС‚Р°: _internal/vosk/
    (os.path.join(vosk_pkg, dll), "vosk") for dll in (
        "libvosk.dll", "libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll",
    ) if os.path.exists(os.path.join(vosk_pkg, dll))
]

# Piper TTS: espeak-ng-data РёС‰РµС‚СЃСЏ РѕС‚РЅРѕСЃРёС‚РµР»СЊРЅРѕ РјРѕРґСѓР»СЏ piper -> _internal/piper/
piper_espeak = os.path.join(vosk_pkg, "..", "piper", "espeak-ng-data")
piper_datas = [(piper_espeak, os.path.join("piper", "espeak-ng-data"))]

a = Analysis(
    ["main.py"],
    pathex=[os.path.join(SPECPATH, "src")],
    binaries=flet_bins + vosk_bins,
    datas=flet_datas + [
        ("data", "data"),
        ("models", "models"),
        ("assets", "assets"),   # иконки + центральная анимация hologram.webp
    ] + piper_datas,
    hiddenimports=flet_hidden + [
        "core.crash_handler",
        "core.command_router",
        "modules.taobao_core",
        "modules.taobao_translator",
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
    name="ZeusAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # РѕС‚Р»Р°РґРєР° РЅР°С‚РёРІРЅРѕРіРѕ РєСЂР°С€Р°
    icon="assets/icons/app_icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ZeusAssistant",
)
