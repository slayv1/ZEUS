# -*- coding: utf-8 -*-
"""Общие побочные эффекты команд.

Голосовой диспетчер (``actions.execute_command``) и инструменты агента
вызывают одни и те же функции: запуск, закрытие, открытие ссылки, поиск.
Реестр ``command_router`` регистрирует их под теми же именами.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Callable

from core.agent_tools import (
    http_get,
    parse_ddg_html,
)


def open_url(url: str, *, opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Открывает http/https-ссылку. ``opener`` подменяется в тестах."""
    target = str(url or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        return {
            "success": False,
            "message": "Открывать можно только http/https-ссылки.",
            "action": "open_url",
        }
    open_fn = opener
    if open_fn is None:
        import webbrowser
        open_fn = webbrowser.open
    try:
        open_fn(target)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "message": f"Не открылось: {exc}",
            "action": "open_url",
        }
    return {"success": True, "message": f"Открыл {target}.", "action": "open_url"}


def open_web_search(query: str) -> dict[str, Any]:
    """Голосовой «погугли»: страница Google через тот же ``open_url``."""
    q = str(query or "").strip()
    url = "https://www.google.com/search?q=" + urllib.parse.quote(q)
    opened = open_url(url)
    if not opened.get("success"):
        return {
            "success": False,
            "message": str(opened.get("message") or "Не удалось открыть браузер."),
            "action": "websearch",
            "query": q,
        }
    return {
        "success": True,
        "message": f"Ищу «{q}» в интернете, сэр. Открываю браузер.",
        "action": "websearch",
        "query": q,
        "url": url,
    }


def search_web(
    query: str,
    *,
    fetch: Callable[..., str] | None = None,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Поиск для агента: разбор DuckDuckGo, иначе та же открывалка ссылок."""
    q = str(query or "").strip()
    if not q:
        return {
            "success": False,
            "message": "Пустой поисковый запрос.",
            "action": "web_search",
        }
    getter = fetch or http_get
    try:
        body = getter("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q))
        results = parse_ddg_html(body)
    except Exception:  # noqa: BLE001 — нет сети: не роняем инструмент
        results = []

    if results:
        return {
            "success": True,
            "query": q,
            "results": results,
            "message": f"Нашёл {len(results)} результатов по «{q}».",
            "action": "web_search",
        }

    url = "https://duckduckgo.com/?q=" + urllib.parse.quote(q)
    opened = open_url(url, opener=opener)
    if not opened.get("success"):
        return {
            "success": False,
            "message": str(opened.get("message") or "Не удалось открыть браузер."),
            "action": "web_search",
        }
    return {
        "success": True,
        "query": q,
        "results": [],
        "url": url,
        "message": "Парсер выдачи не сработал — открыл поиск в браузере.",
        "action": "web_search",
    }


def launch_named(name: str) -> dict[str, Any]:
    """Запуск приложения или игры из commands.json."""
    from core import actions

    query = str(name or "").strip()
    if not query:
        return {
            "success": False,
            "message": "Не указано имя приложения.",
            "action": "launch_app",
        }
    commands = actions.load_commands()
    found = actions.find_best_game_or_app(query, commands)
    kind = found[0] if found else None
    entry = found[1] if found else None
    if kind == "game" and isinstance(entry, dict):
        return actions._launch_game(query, entry)
    if kind == "app" and isinstance(entry, str):
        return actions._launch_app(query, entry)
    return {
        "success": False,
        "message": (
            f"Не нашёл «{query}» в базе команд, сэр. "
            "Скажите «запомни эту программу» или обновите базу."
        ),
        "action": "launch_app",
    }


def close_named(name: str) -> dict[str, Any]:
    """Закрытие процесса. Список системных процессов защищён в actions."""
    from core import actions

    query = str(name or "").strip()
    if not query:
        return {
            "success": False,
            "message": "Не указано имя приложения.",
            "action": "close_app",
        }
    return actions.close_application(query)


def describe_effect(name: str, arguments: dict[str, Any]) -> str:
    """Короткая фраза подтверждения для чата и голоса."""
    args = arguments or {}
    if name == "launch_app":
        return f"Запустить «{args.get('name', '')}», сэр? Скажите «да» или «нет»."
    if name == "close_app":
        return f"Закрыть «{args.get('name', '')}», сэр? Скажите «да» или «нет»."
    if name == "open_url":
        return "Открыть ссылку в браузере, сэр? Скажите «да» или «нет»."
    if name == "click":
        return (
            f"Кликнуть в точку ({args.get('x')}, {args.get('y')}), сэр? "
            "Скажите «да» или «нет»."
        )
    if name == "write_file":
        return (
            f"Записать файл «{args.get('path', '')}», сэр? "
            "Скажите «да» или «нет»."
        )
    try:
        raw = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(args)
    return f"Подтвердите действие «{name}» ({raw}), сэр. Скажите «да» или «нет»."
