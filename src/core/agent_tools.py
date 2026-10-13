# -*- coding: utf-8 -*-
"""Агентный каркас (Harness, ТЗ v1.4): реестр инструментов оркестрации LLM.

Инструмент — функция, которую локальная модель может вызвать через нативный
tool-calling Ollama (``client.chat(tools=...)``), вместо того чтобы просто
отвечать текстом. Модель получает JSON-схемы из :meth:`ToolRegistry.schemas`,
а результат каждого вызова возвращается ей сообщением ``role=tool``.

Семейства инструментов (ТЗ Harness): запуск приложений, работа с файлами,
клик мышью, поиск в сети.

Философия безопасности:

* сбой одного инструмента НЕ роняет цикл агента — :meth:`ToolRegistry.execute`
  возвращает ``{"success": False, "error": ...}`` как данные для модели;
* запись файлов гейтится настройкой ``ai_settings.agent_allow_write``
  (чтение и список папок — всегда разрешены), системные пути закрыты
  deny-листом файлового сканера;
* запуск, закрытие, ссылки и клики гейтятся ``ai_settings.agent_allow_actions``
  (по умолчанию выключено): поиск в сети и чтение файлов остаются доступны;
* результат инструмента усекается до ``_MAX_TOOL_RESULT`` символов, чтобы
  огромный файл не забил контекст модели.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Лимиты
# ---------------------------------------------------------------------------

# Максимальный размер результата инструмента (символов JSON) в контексте модели.
_MAX_TOOL_RESULT = 10_000
# Чтение файла: до 200 КБ на диск, текст — до 30 000 символов.
_MAX_READ_RAW = 200_000
_MAX_READ_CHARS = 30_000
# Запись файла: не больше 200 000 символов за раз.
_MAX_WRITE_CHARS = 200_000
# Список папки: не больше 200 записей в ответе.
_MAX_LIST_ENTRIES = 200
# Клики: допустимый диапазон экранных координат Windows.
_MAX_CURSOR_COORD = 32767

# Заголовок HTTP: без него DuckDuckGo отдаёт капчу/редирект.
_UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

# Уровни риска инструмента (гейтится только RISK_WRITE).
RISK_SAFE = "safe"        # чтение/поиск — без побочных эффектов
RISK_ACTION = "action"    # запуск приложений, браузер, клики
RISK_WRITE = "write"      # изменение ФС (гейтится настройкой)


# ---------------------------------------------------------------------------
# Инструмент и реестр
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    """Описание одного инструмента в формате Ollama/OpenAI function-calling."""

    name: str
    description: str
    parameters: dict[str, Any]          # JSON Schema объекта аргументов
    handler: Callable[..., dict[str, Any]]
    risk: str = RISK_SAFE

    def to_schema(self) -> dict[str, Any]:
        """Схема для параметра ``tools=`` клиента Ollama."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Реестр инструментов: схемы для модели + изолированное исполнение."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Инструмент уже зарегистрирован: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """Список схем для ``client.chat(tools=...)``."""
        return [tool.to_schema() for tool in self._tools.values()]

    def execute(
        self,
        name: str,
        arguments: Any,
        *,
        allow_write: bool = False,
        allow_actions: bool = False,
    ) -> str:
        """Выполняет инструмент и возвращает JSON-строку для ``role=tool``.

        Никогда не бросает исключений наружу: любая ошибка — это данные,
        которые модель должна увидеть, чтобы попробовать иначе.
        Запись и действия с побочным эффектом по умолчанию запрещены.
        """
        result = self._run(
            name, arguments, allow_write=allow_write, allow_actions=allow_actions
        )
        return _encode_result(result)

    # -- внутренности -------------------------------------------------------
    def _run(
        self,
        name: str,
        arguments: Any,
        *,
        allow_write: bool,
        allow_actions: bool = False,
    ) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"success": False, "error": f"Неизвестный инструмент: {name}"}
        if tool.risk == RISK_WRITE and not allow_write:
            return {
                "success": False,
                "error": "Запись файлов отключена в настройках (agent_allow_write).",
            }
        if tool.risk == RISK_ACTION and not allow_actions:
            return {
                "success": False,
                "error": (
                    "Действия с компьютером отключены в настройках "
                    "(agent_allow_actions): запуск, закрытие, ссылки и клики."
                ),
            }

        args = _coerce_args(arguments)
        try:
            result = tool.handler(**args)
        except TypeError as exc:
            # Модель передала лишние/пропущенные аргументы — тоже ошибка данных.
            return {"success": False, "error": f"Некорректные аргументы {name}: {exc}"}
        except Exception as exc:  # noqa: BLE001 — изоляция сбоя инструмента
            return {"success": False, "error": f"Сбой инструмента {name}: {exc}"}

        if not isinstance(result, dict):
            result = {"success": True, "result": str(result)}
        _log_call(name, args, bool(result.get("success")))
        return result


# Module-level helpers -------------------------------------------------------

def _coerce_args(arguments: Any) -> dict[str, Any]:
    """Аргументы из tool_calls: dict, JSON-строка или мусор -> dict."""
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            data = json.loads(arguments)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _encode_result(result: dict[str, Any]) -> str:
    """Сериализует результат в JSON с усечением гигантских ответов."""
    try:
        text = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        text = json.dumps(
            {"success": False, "error": "Результат не сериализуется в JSON"},
            ensure_ascii=False,
        )
    if len(text) <= _MAX_TOOL_RESULT:
        return text
    return json.dumps(
        {
            "success": bool(result.get("success", False)),
            "truncated": True,
            "message": "Результат инструмента обрезан (не влезает в контекст модели).",
            "preview": text[:_MAX_TOOL_RESULT],
        },
        ensure_ascii=False,
    )


def _log_call(name: str, args: dict[str, Any], ok: bool) -> None:
    """Терминальный след инструмента: мост UI показывает строки с [AI]."""
    try:
        arg_s = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover — аргументы уже dict
        arg_s = str(args)
    if len(arg_s) > 120:
        arg_s = arg_s[:120] + "…"
    status = "ок" if ok else "ошибка"
    print(f"[AI] [TOOLS] {name}({arg_s}) -> {status}")


# ---------------------------------------------------------------------------
# Файловая безопасность
# ---------------------------------------------------------------------------
def _load_deny_lists() -> tuple[set[str], list[str]]:
    """Делегирует deny-листы файловому сканеру (единый источник правды)."""
    from core import file_scanner
    return file_scanner._load_deny_lists()


def is_path_denied(path: str) -> bool:
    """True, если путь запрещён для файловых инструментов.

    Проверяются: системные/пользовательские имена папок (любой компонент
    пути) и полные пути из ``scanner_settings.custom_deny_paths`` (префикс).
    """
    if not path:
        return True
    try:
        norm = os.path.normpath(os.path.abspath(str(path)))
    except (OSError, ValueError):
        return True
    try:
        excluded, custom = _load_deny_lists()
    except Exception:  # noqa: BLE001 — сбой конфига не должен открывать доступ
        excluded, custom = set(), []

    low = norm.lower()
    for denied in custom:
        d = os.path.normpath(str(denied)).lower()
        if low == d or low.startswith(d + os.sep):
            return True

    for part in norm.split(os.sep):
        p = part.lower()
        if not p or p.endswith(":"):
            continue  # буква диска
        if p in excluded:
            return True
    # Полные пути из системного списка (C:\$Windows.~BT) — сравниваем префиксом.
    for entry in excluded:
        e = str(entry).lower()
        if ":" in e and (low == e or low.startswith(e.rstrip("\\") + os.sep)):
            return True
    return False


# ---------------------------------------------------------------------------
# Инструменты: приложения
# ---------------------------------------------------------------------------
def _tool_launch_app(name: str) -> dict[str, Any]:
    """Запуск из той же функции, что и голосовая команда."""
    from core.command_effects import launch_named
    return launch_named(name)


def _tool_close_app(name: str) -> dict[str, Any]:
    """Закрытие из той же функции, что и голосовая команда."""
    from core.command_effects import close_named
    return close_named(name)


# ---------------------------------------------------------------------------
# Инструменты: веб-поиск
# ---------------------------------------------------------------------------
def http_get(url: str, timeout: float = 8.0) -> str:
    """GET-запрос с браузерным User-Agent (тестируется подменой)."""
    req = urllib.request.Request(url, headers=dict(_UA_HEADERS))
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


_RESULT_A_RE = re.compile(
    r'<a\s+([^>]*class="result__a"[^>]*)>(.*?)</a>', re.S | re.I
)
_GENERIC_A_RE = re.compile(
    r'<a\s+([^>]*href="https?://[^"]*"[^>]*)>(.*?)</a>', re.S | re.I
)
_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
_SNIPPET_RE = re.compile(
    r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', re.S | re.I
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", fragment)).strip()


def _normalize_ddg_href(href: str) -> str:
    """DDG отдаёт редиректы //duckduckgo.com/l/?uddg=<реальный URL>."""
    href = href.replace("&amp;", "&")
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href and "uddg=" in href:
        query = urllib.parse.urlparse(href).query
        pairs = urllib.parse.parse_qs(query).get("uddg")
        if pairs:
            return pairs[0]
    return href


def parse_ddg_html(body: str, limit: int = 5) -> list[dict[str, str]]:
    """Разбирает выдачу html.duckduckgo.com в [{title, url, snippet}]."""
    anchors = _RESULT_A_RE.findall(body)
    if not anchors:
        anchors = _GENERIC_A_RE.findall(body)

    results: list[dict[str, str]] = []
    for attrs, title_html in anchors:
        href_match = _HREF_RE.search(attrs)
        if not href_match:
            continue
        url = _normalize_ddg_href(href_match.group(1))
        if not url.startswith(("http://", "https://")):
            continue
        if "duckduckgo.com" in url:
            continue  # служебные ссылки самой выдачи
        title = _strip_tags(title_html)
        if not title:
            continue
        results.append({"title": title, "url": url, "snippet": ""})
        if len(results) >= limit:
            break

    snippets = [_strip_tags(s) for s in _SNIPPET_RE.findall(body)]
    for i, item in enumerate(results):
        if i < len(snippets):
            item["snippet"] = snippets[i]
    return results


def _tool_web_search(query: str) -> dict[str, Any]:
    """Веб-поиск через общий эффект (тот же разбор выдачи и открывалка ссылок)."""
    from core.command_effects import search_web
    return search_web(query, fetch=http_get, opener=webbrowser.open)


def _tool_open_url(url: str) -> dict[str, Any]:
    """Открывает http/https-ссылку той же функцией, что и голосовой поиск."""
    from core.command_effects import open_url
    return open_url(url, opener=webbrowser.open)


# ---------------------------------------------------------------------------
# Инструменты: файлы
# ---------------------------------------------------------------------------
def _tool_read_file(path: str) -> dict[str, Any]:
    """Читает текстовый файл (лимит 30 000 символов, бинарники отклоняются)."""
    if not path:
        return {"success": False, "message": "Не указан путь.", "action": "read_file"}
    if is_path_denied(path):
        return {"success": False,
                "message": "Доступ к этому пути запрещён (системная/запретная папка).",
                "action": "read_file"}
    norm = os.path.normpath(os.path.abspath(str(path)))
    if os.path.isdir(norm):
        return {"success": False,
                "message": "Это папка — используй инструмент list_dir.",
                "action": "read_file"}
    if not os.path.isfile(norm):
        return {"success": False, "message": f"Файл не найден: {norm}",
                "action": "read_file"}
    try:
        with open(norm, "rb") as fh:
            raw = fh.read(_MAX_READ_RAW)
    except OSError as exc:
        return {"success": False, "message": f"Не прочитал: {exc}",
                "action": "read_file"}

    if b"\x00" in raw[:8192]:
        return {"success": False,
                "message": "Похоже, бинарный файл — не читаю как текст.",
                "action": "read_file"}

    text = raw.decode("utf-8", errors="replace")
    truncated = os.path.getsize(norm) > len(raw)
    if len(text) > _MAX_READ_CHARS:
        text = text[:_MAX_READ_CHARS]
        truncated = True
    return {"success": True, "path": norm, "content": text,
            "truncated": truncated, "action": "read_file"}


def _tool_list_dir(path: str) -> dict[str, Any]:
    """Список папки (до 200 записей; папки помечаются суффиксом «/»)."""
    if not path:
        return {"success": False, "message": "Не указан путь.", "action": "list_dir"}
    if is_path_denied(path):
        return {"success": False,
                "message": "Доступ к этому пути запрещён (системная/запретная папка).",
                "action": "list_dir"}
    norm = os.path.normpath(os.path.abspath(str(path)))
    if not os.path.isdir(norm):
        return {"success": False, "message": f"Папки не существует: {norm}",
                "action": "list_dir"}
    entries: list[str] = []
    truncated = False
    try:
        with os.scandir(norm) as it:
            for entry in it:
                if len(entries) >= _MAX_LIST_ENTRIES:
                    truncated = True
                    break
                entries.append(entry.name + ("/" if entry.is_dir() else ""))
    except OSError as exc:
        return {"success": False, "message": f"Не прочитал папку: {exc}",
                "action": "list_dir"}
    entries.sort()
    return {"success": True, "path": norm, "entries": entries,
            "truncated": truncated, "action": "list_dir"}


def _tool_write_file(path: str, content: str) -> dict[str, Any]:
    """Атомарная запись текста (tmp + os.replace), гейтится agent_allow_write."""
    if not path:
        return {"success": False, "message": "Не указан путь.", "action": "write_file"}
    if is_path_denied(path):
        return {"success": False,
                "message": "Запись в этот путь запрещена (системная/запретная папка).",
                "action": "write_file"}
    text = "" if content is None else str(content)
    if len(text) > _MAX_WRITE_CHARS:
        return {"success": False,
                "message": f"Слишком большой текст (лимит {_MAX_WRITE_CHARS} символов).",
                "action": "write_file"}
    norm = os.path.normpath(os.path.abspath(str(path)))
    parent = os.path.dirname(norm)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = norm + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, norm)
    except OSError as exc:
        try:
            if os.path.exists(norm + ".tmp"):
                os.remove(norm + ".tmp")
        except OSError:
            pass
        return {"success": False, "message": f"Не записал: {exc}",
                "action": "write_file"}
    return {"success": True, "path": norm, "bytes": len(text.encode("utf-8")),
            "action": "write_file"}


# ---------------------------------------------------------------------------
# Инструменты: клики мышью
# ---------------------------------------------------------------------------
def _mouse_click(x: int, y: int, button: str, double: bool) -> None:
    """Низкоуровневый клик Win32 (выделяется под тесты подменой)."""
    if os.name != "nt":
        raise OSError("Клик мышью доступен только на Windows")
    import ctypes

    flags = {
        "left": (0x0002, 0x0004),     # MOUSEEVENTF_LEFTDOWN / LEFTUP
        "right": (0x0008, 0x0010),    # RIGHTDOWN / RIGHTUP
        "middle": (0x0020, 0x0040),   # MIDDLEDOWN / MIDDLEUP
    }
    down, up = flags[button]
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    for _ in range(2 if double else 1):
        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.05)


def _tool_click(
    x: int,
    y: int,
    button: str = "left",
    double: bool = False,
) -> dict[str, Any]:
    """Клик мышью по экранным координатам."""
    try:
        xi, yi = int(x), int(y)
    except (TypeError, ValueError):
        return {"success": False, "message": "Координаты должны быть числами.",
                "action": "click"}
    if not (0 <= xi <= _MAX_CURSOR_COORD and 0 <= yi <= _MAX_CURSOR_COORD):
        return {"success": False,
                "message": f"Координаты вне диапазона 0..{_MAX_CURSOR_COORD}.",
                "action": "click"}
    btn = str(button or "left").lower()
    if btn not in ("left", "right", "middle"):
        return {"success": False, "message": "button: left | right | middle.",
                "action": "click"}
    if isinstance(double, str):
        double = double.strip().lower() in ("1", "true", "yes", "да")
    try:
        _mouse_click(xi, yi, btn, bool(double))
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "message": f"Клик не вышел: {exc}",
                "action": "click"}
    kind = "двойной " if double else ""
    return {"success": True,
            "message": f"{kind.capitalize()}{btn}-клик в ({xi}, {yi}).",
            "action": "click"}


# ---------------------------------------------------------------------------
# Сборка реестра
# ---------------------------------------------------------------------------
def build_default_registry() -> ToolRegistry:
    """Полный набор инструментов агентного каркаса."""
    registry = ToolRegistry()

    registry.register(Tool(
        name="launch_app",
        description=(
            "Запускает установленное приложение или игру по имени "
            "(база команд пользователя: «блокнот», «стим» и т.п.)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Название приложения или игры"},
            },
            "required": ["name"],
        },
        handler=_tool_launch_app,
        risk=RISK_ACTION,
    ))
    registry.register(Tool(
        name="close_app",
        description=(
            "Закрывает приложение по имени процесса. Системные процессы "
            "(explorer, python, svchost...) закрыть нельзя."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Имя процесса, например chrome.exe"},
            },
            "required": ["name"],
        },
        handler=_tool_close_app,
        risk=RISK_ACTION,
    ))
    registry.register(Tool(
        name="web_search",
        description=(
            "Ищет информацию в интернете (DuckDuckGo) и возвращает топ-5 "
            "результатов: заголовок, ссылка и краткое описание."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Поисковый запрос"},
            },
            "required": ["query"],
        },
        handler=_tool_web_search,
        risk=RISK_SAFE,
    ))
    registry.register(Tool(
        name="open_url",
        description="Открывает http/https-ссылку в браузере пользователя.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Полный URL (http/https)"},
            },
            "required": ["url"],
        },
        handler=_tool_open_url,
        risk=RISK_ACTION,
    ))
    registry.register(Tool(
        name="read_file",
        description=(
            "Читает текстовый файл и возвращает его содержимое (до 30 000 "
            "символов). Системные папки недоступны."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к файлу"},
            },
            "required": ["path"],
        },
        handler=_tool_read_file,
        risk=RISK_SAFE,
    ))
    registry.register(Tool(
        name="list_dir",
        description=(
            "Показывает содержимое папки (до 200 записей; папки с суффиксом «/»)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к папке"},
            },
            "required": ["path"],
        },
        handler=_tool_list_dir,
        risk=RISK_SAFE,
    ))
    registry.register(Tool(
        name="write_file",
        description=(
            "Записывает текст в файл (создаёт папки, запись атомарная). "
            "Требует разрешения agent_allow_write в настройках."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к файлу"},
                "content": {"type": "string", "description": "Текст для записи"},
            },
            "required": ["path", "content"],
        },
        handler=_tool_write_file,
        risk=RISK_WRITE,
    ))
    registry.register(Tool(
        name="click",
        description=(
            "Клик мышью по экранным координатам (пиксели, начало — левый "
            "верхний угол). button: left|right|middle, double — двойной клик."
        ),
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Координата X"},
                "y": {"type": "integer", "description": "Координата Y"},
                "button": {"type": "string", "enum": ["left", "right", "middle"],
                           "description": "Кнопка мыши (по умолчанию left)"},
                "double": {"type": "boolean",
                           "description": "Двойной клик (по умолчанию false)"},
            },
            "required": ["x", "y"],
        },
        handler=_tool_click,
        risk=RISK_ACTION,
    ))

    return registry


_DEFAULT_REGISTRY: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """Синглтон-реестр приложения (общий для ChatEngine и тестов)."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY
