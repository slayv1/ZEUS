# -*- coding: utf-8 -*-
"""Патчер: chat_engine -> синхронный Ollama в потоке (без Event loop is closed)."""
import io

P = "src/core/chat_engine.py"
s = io.open(P, encoding="utf-8").read()

# --- Заменяем AsyncClient на синхронный import на уровне модуля ---
old_import = "from ollama import AsyncClient"
new_import = ("# Синхронный клиент Ollama (ТЗ 2.3): устраняет «Event loop is closed».\\n"
              "# Раньше использовался AsyncClient, чья aiohttp-сессия привязывалась к\\n"
              "# закрытому event-loop.""")
# аккуратно: сначала заменим использование ниже, а импорт оставим
