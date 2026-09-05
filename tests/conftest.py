"""Pytest configuration: add project root and src/ to sys.path.

Запуск из корня проекта:  python -m pytest tests/ -v
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

# Отключаем реальный Flet/аудио при импорте — тесты проверяют логику, а не UI.
import os
os.environ.setdefault("ZEUS_TESTING", "1")
