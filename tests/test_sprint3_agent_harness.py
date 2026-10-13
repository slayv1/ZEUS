# -*- coding: utf-8 -*-
"""Спринт 3 (ТЗ v1.4, §Harness): агентный каркас — инструменты + цикл LLM.

Что проверяется:
  1. core/agent_tools.py — схемы для модели, изоляция сбоев, гейт записи,
     deny-лист файлов, файловые/web/клик/запуск-приложений инструменты.
  2. core/agent_harness.py — цикл «tool_calls -> role=tool -> повтор»,
     лимит шагов, object- и dict-ответы Ollama, сквозной allow_write.
  3. core/chat_engine.py — set_agent_config, агентный путь в _run_sync и
     откат на обычный стриминг, если модель/каркас не тянут tools.
  4. core/config.py + core/controller.py — ключи agent_* в настройках и
     мгновенное применение без перезапуска.
  5. UI — тумблеры агентного режима в карточке «Модель и генерация».

Внешние эффекты подменяются: сеть (http_get), браузер (webbrowser.open),
Win32-клик (_mouse_click), запуск приложений (actions). Реальные настройки
пользователя изолируются подменой config.SETTINGS_PATH.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from core import agent_tools, config
from core.agent_harness import AgentHarness, HarnessResult
from core.agent_tools import RISK_ACTION, RISK_SAFE, RISK_WRITE, Tool, ToolRegistry
from core.chat_engine import ChatEngine
from core.config import DEFAULT_SETTINGS
from core.controller import ZeusController

DDG_HTML = """
<html><body>
  <div class="result results_links">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=1">Первый <b>заголовок</b></a>
    <td class="result-snippet">Короткое описание</td>
  </div>
  <div class="result results_links">
    <a class="result__a" href="https://example.org/second">Второй результат</a>
    <td class="result-snippet">Второе описание</td>
  </div>
</body></html>
"""


def _tool_call(name: str, **arguments) -> dict:
    """tool_call в том виде, в котором его отдаёт Ollama."""
    return {"function": {"name": name, "arguments": arguments}}


def _response(content: str = "", tool_calls=None) -> dict:
    return {"message": {"content": content, "tool_calls": tool_calls or []}}


def _obj(**kwargs):
    """Объект с атрибутами — ответ ollama в новых версиях (не dict)."""
    return type("Resp", (), kwargs)()


@pytest.fixture
def fs_guard(tmp_path, monkeypatch):
    """settings.json во временной папке с «мягким» deny-листом.

    Дефолтный список запрещает «Temp», а pytest кладёт tmp_path именно туда —
    поэтому для функциональных тестов файлов deny-лист урезаем до node_modules.
    """
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", str(settings_path))
    config.SettingsManager(settings_path).set(
        "scanner_settings", "deny_dir_names", ["node_modules", ".git"]
    )
    return settings_path


# -----------------------------------------------------------------------
# 1. Схемы и реестр инструментов
# -----------------------------------------------------------------------
class TestToolSchema:
    def test_схема_в_формате_function_calling(self):
        tool = Tool(
            name="noop",
            description="Ничего не делает",
            parameters={"type": "object", "properties": {}},
            handler=lambda: {"success": True},
        )
        schema = tool.to_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "noop"
        assert schema["function"]["description"] == "Ничего не делает"
        assert schema["function"]["parameters"]["type"] == "object"

    def test_дефолтный_реестр_покрывает_семейства(self):
        names = set(agent_tools.get_registry().names())
        # ТЗ Harness: запуск приложений, файлы, клик, поиск в сети.
        assert {"launch_app", "close_app"} <= names
        assert {"read_file", "list_dir", "write_file"} <= names
        assert {"web_search", "open_url", "click"} <= names

    def test_каждый_инструмент_валиден_для_ollama(self):
        for schema in agent_tools.build_default_registry().schemas():
            fn = schema["function"]
            assert fn["name"] and fn["description"]
            assert fn["parameters"]["type"] == "object"
            assert isinstance(fn["parameters"].get("required", []), list)
            assert json.loads(json.dumps(schema, ensure_ascii=False))


class TestToolRegistry:
    def test_повторная_регистрация_запрещена(self):
        registry = ToolRegistry()
        tool = Tool("a", "d", {"type": "object"}, lambda: {"success": True})
        registry.register(tool)
        with pytest.raises(ValueError):
            registry.register(tool)

    def test_неизвестный_инструмент_возвращает_ошибку_данными(self):
        out = json.loads(ToolRegistry().execute("нет_такого", {}))
        assert out["success"] is False
        assert "нет_такого" in out["error"]

    def test_сбой_инструмента_не_поднимается_наружу(self):
        def boom():
            raise RuntimeError("бум")

        registry = ToolRegistry()
        registry.register(Tool("boom", "падает", {"type": "object"}, boom))
        out = json.loads(registry.execute("boom", {}))
        assert out["success"] is False
        assert "бум" in out["error"]

    def test_лишние_аргументы_дают_ошибку_а_не_исключение(self):
        registry = ToolRegistry()
        registry.register(
            Tool("need2", "нужны два", {"type": "object"},
                 lambda a, b: {"success": True, "sum": a + b})
        )
        out = json.loads(registry.execute("need2", {"a": 1}))
        assert out["success"] is False
        assert "Некорректные аргументы" in out["error"]

    def test_аргументы_приходят_строкой_json(self):
        seen = {}

        def handler(path: str = ""):
            seen["path"] = path
            return {"success": True}

        registry = ToolRegistry()
        registry.register(Tool("p", "принимает путь", {"type": "object"}, handler))
        out = json.loads(registry.execute("p", json.dumps({"path": "C:/x.txt"})))
        assert out["success"] is True
        assert seen["path"] == "C:/x.txt"

    def test_мусор_в_аргументах_не_ломает_выполнение(self):
        registry = ToolRegistry()
        registry.register(
            Tool("p", "путь", {"type": "object"},
                 lambda path="": {"success": True, "path": path})
        )
        out = json.loads(registry.execute("p", "не json"))
        assert out["success"] is True and out["path"] == ""

    def test_не_dict_результат_оборачивается(self):
        registry = ToolRegistry()
        registry.register(Tool("s", "строка", {"type": "object"}, lambda: "готово"))
        assert json.loads(registry.execute("s", {})) == {
            "success": True, "result": "готово"}

    def test_огромный_результат_обрезается(self):
        registry = ToolRegistry()
        registry.register(
            Tool("big", "много данных", {"type": "object"},
                 lambda: {"success": True,
                          "content": "x" * (agent_tools._MAX_TOOL_RESULT + 500)})
        )
        out = json.loads(registry.execute("big", {}))
        assert out["truncated"] is True
        assert len(out["preview"]) <= agent_tools._MAX_TOOL_RESULT

    def test_запись_файлов_выключается_настройкой(self):
        registry = ToolRegistry()
        registry.register(
            Tool("w", "пишет", {"type": "object"}, lambda: {"success": True},
                 risk=RISK_WRITE)
        )
        assert json.loads(registry.execute("w", {}, allow_write=True))["success"] is True
        denied = json.loads(registry.execute("w", {}, allow_write=False))
        assert denied["success"] is False
        assert "agent_allow_write" in denied["error"]

    def test_действия_выключаются_настройкой(self):
        registry = ToolRegistry()
        registry.register(
            Tool("c", "кликает", {"type": "object"}, lambda: {"success": True},
                 risk=RISK_ACTION)
        )
        denied = json.loads(registry.execute("c", {}))
        assert denied["success"] is False
        assert "agent_allow_actions" in denied["error"]
        allowed = json.loads(registry.execute("c", {}, allow_actions=True))
        assert allowed["success"] is True

    def test_чтение_не_гейтится_флагом_записи(self):
        registry = ToolRegistry()
        registry.register(
            Tool("r", "читает", {"type": "object"}, lambda: {"success": True},
                 risk=RISK_SAFE)
        )
        out = json.loads(registry.execute("r", {}, allow_write=False))
        assert out["success"] is True

    def test_реестр_синглтон(self):
        assert agent_tools.get_registry() is agent_tools.get_registry()


# -----------------------------------------------------------------------
# 2. Файловая безопасность
# -----------------------------------------------------------------------
class TestPathDeny:
    def test_пустой_путь_запрещён(self, fs_guard):
        assert agent_tools.is_path_denied("") is True

    def test_системная_папка_запрещена(self, fs_guard):
        assert agent_tools.is_path_denied(r"C:\Windows\System32\etc\hosts") is True

    def test_пользовательский_deny_путь(self, fs_guard, tmp_path):
        secret = tmp_path / "secret"
        secret.mkdir()
        config.SettingsManager(fs_guard).set(
            "scanner_settings", "custom_deny_paths", [str(secret)]
        )
        assert agent_tools.is_path_denied(str(secret / "notes.txt")) is True
        # Соседняя папка с тем же префиксом разрешена: границы по разделителю
        assert agent_tools.is_path_denied(str(tmp_path / "secret_public")) is False

    def test_обычная_папка_разрешена(self, fs_guard, tmp_path):
        assert agent_tools.is_path_denied(str(tmp_path / "docs")) is False

    def test_сбой_загрузки_настроек_не_пробрасывается(self, fs_guard, monkeypatch):
        def boom():
            raise OSError("settings.json недоступен")

        monkeypatch.setattr(agent_tools, "_load_deny_lists", boom)
        assert agent_tools.is_path_denied(r"C:\Users\me\file.txt") is False


# -----------------------------------------------------------------------
# 3. Файловые инструменты
# -----------------------------------------------------------------------
class TestFileTools:
    def test_чтение_файла(self, fs_guard, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("привет, Зевс", encoding="utf-8")
        out = agent_tools._tool_read_file(str(f))
        assert out["success"] is True
        assert out["content"] == "привет, Зевс"
        assert out["truncated"] is False

    def test_чтение_отклоняет_бинарник(self, fs_guard, tmp_path):
        f = tmp_path / "app.exe"
        f.write_bytes(b"MZ\x00\x00binary")
        out = agent_tools._tool_read_file(str(f))
        assert out["success"] is False
        assert "бинарный" in out["message"].lower()

    def test_чтение_папки_отправляет_в_list_dir(self, fs_guard, tmp_path):
        out = agent_tools._tool_read_file(str(tmp_path))
        assert out["success"] is False
        assert "list_dir" in out["message"]

    def test_чтение_несуществующего_файла(self, fs_guard, tmp_path):
        assert agent_tools._tool_read_file(str(tmp_path / "нет.txt"))["success"] is False

    def test_чтение_обрезает_огромный_файл(self, fs_guard, tmp_path):
        f = tmp_path / "huge.txt"
        f.write_text("y" * (agent_tools._MAX_READ_CHARS + 1000), encoding="utf-8")
        out = agent_tools._tool_read_file(str(f))
        assert out["success"] is True
        assert out["truncated"] is True
        assert len(out["content"]) <= agent_tools._MAX_READ_CHARS

    def test_чтение_запрещённого_пути(self, fs_guard):
        out = agent_tools._tool_read_file(r"C:\Windows\System32\config\SAM")
        assert out["success"] is False
        assert "запрещён" in out["message"].lower()

    def test_список_папки_помечает_подпапки(self, fs_guard, tmp_path):
        folder = tmp_path / "листинг"
        (folder / "sub").mkdir(parents=True)
        (folder / "file.txt").write_text("x", encoding="utf-8")
        out = agent_tools._tool_list_dir(str(folder))
        assert out["success"] is True
        assert out["entries"] == ["file.txt", "sub/"]

    def test_список_папки_обрезается(self, fs_guard, tmp_path):
        for i in range(agent_tools._MAX_LIST_ENTRIES + 10):
            (tmp_path / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
        out = agent_tools._tool_list_dir(str(tmp_path))
        assert out["truncated"] is True
        assert len(out["entries"]) == agent_tools._MAX_LIST_ENTRIES

    def test_список_несуществующей_папки(self, fs_guard, tmp_path):
        assert agent_tools._tool_list_dir(str(tmp_path / "нет"))["success"] is False

    def test_запись_создаёт_папки_и_не_оставляет_tmp(self, fs_guard, tmp_path):
        target = tmp_path / "new" / "deep" / "out.txt"
        out = agent_tools._tool_write_file(str(target), "данные")
        assert out["success"] is True
        assert target.read_text(encoding="utf-8") == "данные"
        assert not (tmp_path / "new" / "deep" / "out.txt.tmp").exists()

    def test_запись_перезаписывает(self, fs_guard, tmp_path):
        target = tmp_path / "log.txt"
        target.write_text("старое", encoding="utf-8")
        assert agent_tools._tool_write_file(str(target), "новое")["success"] is True
        assert target.read_text(encoding="utf-8") == "новое"

    def test_запись_в_запрещённый_путь(self, fs_guard):
        out = agent_tools._tool_write_file(r"C:\Windows\System32\evil.dll", "x")
        assert out["success"] is False
        assert "запрещена" in out["message"].lower()

    def test_запись_слишком_большого_текста(self, fs_guard, tmp_path):
        out = agent_tools._tool_write_file(
            str(tmp_path / "big.txt"), "z" * (agent_tools._MAX_WRITE_CHARS + 1)
        )
        assert out["success"] is False
        assert not (tmp_path / "big.txt").exists()

    def test_пустой_путь_отклоняется(self, fs_guard):
        assert agent_tools._tool_read_file("")["success"] is False
        assert agent_tools._tool_list_dir("")["success"] is False
        assert agent_tools._tool_write_file("", "x")["success"] is False


# -----------------------------------------------------------------------
# 4. Web-поиск, ссылки, клик
# -----------------------------------------------------------------------
class TestWebSearch:
    def test_парсер_выдачи_duckduckgo(self):
        results = agent_tools.parse_ddg_html(DDG_HTML)
        assert len(results) == 2
        assert results[0]["url"] == "https://example.com/page"  # uddg-редирект распёрт
        assert results[0]["title"] == "Первый заголовок"
        assert results[0]["snippet"] == "Короткое описание"
        assert results[1]["url"] == "https://example.org/second"

    def test_парсер_учитывает_лимит(self):
        assert len(agent_tools.parse_ddg_html(DDG_HTML, limit=1)) == 1

    def test_поиск_возвращает_результаты(self, monkeypatch):
        monkeypatch.setattr(agent_tools, "http_get", lambda url, **k: DDG_HTML)
        out = agent_tools._tool_web_search("что нового")
        assert out["success"] is True
        assert out["query"] == "что нового"
        assert len(out["results"]) == 2
        assert out["action"] == "web_search"

    def test_без_сети_открывается_браузер(self, monkeypatch):
        def offline(url, **kwargs):
            raise OSError("сети нет")

        opened = []
        monkeypatch.setattr(agent_tools, "http_get", offline)
        monkeypatch.setattr(agent_tools.webbrowser, "open",
                            lambda url, *a, **k: opened.append(url) or True)
        out = agent_tools._tool_web_search("погода")
        assert out["success"] is True
        assert out["results"] == []
        assert opened and "duckduckgo.com" in opened[0]

    def test_пустой_запрос(self, monkeypatch):
        monkeypatch.setattr(agent_tools, "http_get", lambda url, **k: DDG_HTML)
        assert agent_tools._tool_web_search("  ")["success"] is False


class TestOpenUrl:
    def test_только_http_https(self, monkeypatch):
        opened = []
        monkeypatch.setattr(agent_tools.webbrowser, "open",
                            lambda url, *a, **k: opened.append(url) or True)
        assert agent_tools._tool_open_url("file:///C:/x")["success"] is False
        assert agent_tools._tool_open_url("")["success"] is False
        assert opened == []
        assert agent_tools._tool_open_url("https://zeus.local")["success"] is True
        assert opened == ["https://zeus.local"]


class TestClickTool:
    @pytest.fixture
    def clicks(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            agent_tools, "_mouse_click",
            lambda x, y, button, double: calls.append((x, y, button, double)),
        )
        return calls

    def test_клик_левой_кнопкой(self, clicks):
        out = agent_tools._tool_click(100, 200)
        assert out["success"] is True
        assert clicks == [(100, 200, "left", False)]

    def test_двойной_клик_строкой(self, clicks):
        assert agent_tools._tool_click(1, 2, double="да")["success"] is True
        assert clicks == [(1, 2, "left", True)]

    def test_правая_кнопка(self, clicks):
        assert agent_tools._tool_click(5, 6, button="RIGHT")["success"] is True
        assert clicks == [(5, 6, "right", False)]

    def test_нечисловые_координаты(self, clicks):
        assert agent_tools._tool_click("лево", 10)["success"] is False
        assert clicks == []

    def test_координаты_вне_экрана(self, clicks):
        assert agent_tools._tool_click(-1, 0)["success"] is False
        assert agent_tools._tool_click(0, 99999)["success"] is False
        assert clicks == []

    def test_неизвестная_кнопка(self, clicks):
        assert agent_tools._tool_click(1, 1, button="extra")["success"] is False
        assert clicks == []

    def test_сбой_win32_не_поднимается(self, monkeypatch):
        def boom(*args):
            raise OSError("user32 недоступен")

        monkeypatch.setattr(agent_tools, "_mouse_click", boom)
        out = agent_tools._tool_click(3, 4)
        assert out["success"] is False
        assert "user32" in out["message"]


# -----------------------------------------------------------------------
# 5. Запуск и закрытие приложений
# -----------------------------------------------------------------------
class TestAppTools:
    def test_запуск_игры_делегирует_actions(self, monkeypatch):
        from core import actions

        monkeypatch.setattr(actions, "load_commands", lambda: {"games": {}})
        monkeypatch.setattr(actions, "find_best_game_or_app",
                            lambda q, c: ("game", {"type": "steam", "appid": "42"}))
        monkeypatch.setattr(actions, "_launch_game",
                            lambda name, entry: {"success": True,
                                                 "action": "launch_app",
                                                 "message": f"Запустил {name}"})
        out = agent_tools._tool_launch_app("half life")
        assert out["success"] is True
        assert out["message"] == "Запустил half life"

    def test_запуск_приложения_по_пути(self, monkeypatch):
        from core import actions

        monkeypatch.setattr(actions, "load_commands", lambda: {"apps": {}})
        monkeypatch.setattr(actions, "find_best_game_or_app",
                            lambda q, c: ("app", "C:/Tools/notepad.exe"))
        monkeypatch.setattr(actions, "_launch_app",
                            lambda name, path: {"success": True, "path": path})
        assert agent_tools._tool_launch_app("блокнот")["path"] == "C:/Tools/notepad.exe"

    def test_неизвестное_приложение_даёт_подсказку(self, monkeypatch):
        from core import actions

        monkeypatch.setattr(actions, "load_commands", lambda: {"apps": {}})
        monkeypatch.setattr(actions, "find_best_game_or_app", lambda q, c: None)
        out = agent_tools._tool_launch_app("нетакая программа")
        assert out["success"] is False
        assert "Не нашёл" in out["message"]

    def test_пустое_имя_приложения(self, monkeypatch):
        from core import actions

        monkeypatch.setattr(actions, "load_commands", lambda: {"apps": {}})
        assert agent_tools._tool_launch_app("")["success"] is False

    def test_закрытие_приложения_делегирует_actions(self, monkeypatch):
        from core import actions

        seen = []
        monkeypatch.setattr(actions, "close_application",
                            lambda name: seen.append(name) or {"success": True})
        assert agent_tools._tool_close_app("chrome.exe")["success"] is True
        assert seen == ["chrome.exe"]

    def test_пустое_имя_для_закрытия(self):
        assert agent_tools._tool_close_app("  ")["success"] is False


# -----------------------------------------------------------------------
# 6. Цикл AgentHarness
# -----------------------------------------------------------------------
class _ScriptedClient:
    """Клиент Ollama, отдающая заранее заданные ответы по очереди."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("модель получила больше запросов, чем ожидалось")
        return self.responses.pop(0)


def _registry_with(name: str, result: dict, *, risk: str = RISK_SAFE) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(name, "тестовый инструмент",
                          {"type": "object", "properties": {}},
                          lambda **kw: dict(result), risk=risk))
    return registry


class TestHarnessLoop:
    def test_без_tool_calls_возвращается_финальный_текст(self):
        client = _ScriptedClient([_response("Здравствуйте, сэр.")])
        harness = AgentHarness(_registry_with("noop", {"success": True}))
        result = harness.run(client, model="llama3.1", messages=[])
        assert isinstance(result, HarnessResult)
        assert result.text == "Здравствуйте, сэр."
        assert result.used_tools == []
        assert result.limit_reached is False
        # Схемы инструментов уходят в модель, стриминг выключен
        assert client.calls[0]["tools"]
        assert client.calls[0]["stream"] is False

    def test_инструмент_выполняется_и_результат_возвращается_модели(self):
        client = _ScriptedClient([
            _response("", [_tool_call("noop", x=1)]),
            _response("Готово, сэр."),
        ])
        harness = AgentHarness(_registry_with("noop", {"success": True, "echo": 1}))
        result = harness.run(
            client, model="llama3.1", messages=[{"role": "user", "content": "сделай"}]
        )
        assert result.text == "Готово, сэр."
        assert result.used_tools == ["noop"]
        sent = client.calls[1]["messages"]
        assert sent[0]["role"] == "user"
        assert sent[1]["role"] == "assistant"
        assert sent[1]["tool_calls"][0]["function"]["name"] == "noop"
        assert sent[2]["role"] == "tool"
        assert sent[2]["tool_name"] == "noop"
        assert json.loads(sent[2]["content"])["echo"] == 1

    def test_ошибка_инструмента_доходит_до_модели_данными(self):
        def boom(**kwargs):
            raise RuntimeError("инструмент сломан")

        registry = ToolRegistry()
        registry.register(Tool("boom", "падает", {"type": "object"}, boom))
        client = _ScriptedClient([
            _response("", [_tool_call("boom")]),
            _response("Инструмент не сработал."),
        ])
        result = AgentHarness(registry).run(client, model="m", messages=[])
        payload = json.loads(client.calls[1]["messages"][1]["content"])
        assert payload["success"] is False
        assert "инструмент сломан" in payload["error"]
        assert result.text == "Инструмент не сработал."

    def test_несколько_вызовов_за_один_шаг(self):
        registry = ToolRegistry()
        registry.register(Tool("a", "первый", {"type": "object"},
                               lambda: {"success": True, "n": 1}))
        registry.register(Tool("b", "второй", {"type": "object"},
                               lambda: {"success": True, "n": 2}))
        client = _ScriptedClient([
            _response("", [_tool_call("a"), _tool_call("b")]),
            _response("Оба выполнены."),
        ])
        result = AgentHarness(registry).run(client, model="m", messages=[])
        assert result.used_tools == ["a", "b"]
        assert result.steps == 2

    def test_лимит_шагов_даёт_честный_текст(self):
        client = _ScriptedClient([_response("", [_tool_call("noop")]) for _ in range(5)])
        result = AgentHarness(_registry_with("noop", {"success": True})).run(
            client, model="m", messages=[], max_steps=2
        )
        assert result.limit_reached is True
        assert result.steps == 2
        assert "не успел" in result.text.lower()
        assert result.used_tools == ["noop", "noop"]

    def test_allow_write_проброшен_в_реестр(self):
        registry = _registry_with("w", {"success": True}, risk=RISK_WRITE)
        seen: list[bool] = []
        original = registry.execute

        def spy(name, arguments, *, allow_write=True, allow_actions=False):
            seen.append(allow_write)
            return original(
                name, arguments,
                allow_write=allow_write, allow_actions=allow_actions,
            )

        registry.execute = spy  # type: ignore[method-assign]
        client = _ScriptedClient([_response("", [_tool_call("w")]), _response("готово")])
        AgentHarness(registry).run(client, model="m", messages=[], allow_write=False)
        assert seen == [False]
        payload = json.loads(client.calls[1]["messages"][1]["content"])
        assert payload["success"] is False

    def test_ответы_объектами_ollama(self):
        """Новые версии ollama отдают объекты, а не dict — цикл обязан это терпеть."""
        client = _ScriptedClient([
            _obj(message=_obj(content="", tool_calls=[_obj(
                function=_obj(name="noop", arguments={"x": 1}))])),
            _obj(message=_obj(content="Готово.", tool_calls=None)),
        ])
        result = AgentHarness(_registry_with("noop", {"success": True})).run(
            client, model="m", messages=[]
        )
        assert result.text == "Готово."
        assert result.used_tools == ["noop"]

    def test_аргументы_строкой_json_разбираются(self):
        seen = []
        registry = ToolRegistry()
        registry.register(Tool("p", "путь", {"type": "object"},
                               lambda path="": seen.append(path) or {"success": True}))
        client = _ScriptedClient([
            _response("", [{"function": {"name": "p",
                                         "arguments": '{"path": "C:/a.txt"}'}}]),
            _response("ок"),
        ])
        AgentHarness(registry).run(client, model="m", messages=[])
        assert seen == ["C:/a.txt"]

    def test_ошибка_клиента_пробрасывается_вызывающей_стороне(self):
        class Broken:
            def chat(self, **kwargs):
                raise ConnectionError("Ollama недоступна")

        with pytest.raises(ConnectionError):
            AgentHarness(_registry_with("noop", {"success": True})).run(
                Broken(), model="m", messages=[]
            )

    def test_события_диагностики_помечены(self):
        events = []
        client = _ScriptedClient([_response("", [_tool_call("noop")]), _response("готово")])
        AgentHarness(_registry_with("noop", {"success": True}),
                     on_event=events.append).run(client, model="m", messages=[])
        assert events and all(e.startswith("[AI] [HARNESS]") for e in events)
        assert any("noop" in e for e in events)

    def test_сбой_обработчика_событий_не_ломает_цикл(self):
        def bad(text):
            raise RuntimeError("мост UI упал")

        client = _ScriptedClient([_response("просто текст")])
        result = AgentHarness(_registry_with("noop", {"success": True}),
                              on_event=bad).run(client, model="m", messages=[])
        assert result.text == "просто текст"


# -----------------------------------------------------------------------
# 7. Интеграция с ChatEngine
# -----------------------------------------------------------------------
@pytest.fixture
def engine(monkeypatch, tmp_path):
    """ChatEngine без живой Ollama и без записи в data/cache.json."""
    from core import chat_engine as ce

    monkeypatch.setattr(ce, "_CACHE_PATH", str(tmp_path / "cache.json"))
    eng = ChatEngine()
    eng.client = MagicMock()
    return eng


def _patch_harness(monkeypatch, result=None, side_effect=None):
    """Подменяет AgentHarness в chat_engine заглушкой с заданным результатом."""
    from core import agent_harness

    harness = MagicMock()
    if side_effect is not None:
        harness.run.side_effect = side_effect
    else:
        harness.run.return_value = result
    monkeypatch.setattr(agent_harness, "AgentHarness", MagicMock(return_value=harness))
    return harness


class TestChatEngineAgentConfig:
    def test_дефолты(self, engine):
        assert engine.agent_enabled is False
        assert engine.agent_max_steps == 5
        assert engine.agent_allow_write is False
        assert engine.agent_allow_actions is False

    def test_set_agent_config_нормализует_значения(self, engine):
        engine.set_agent_config(enabled=True, max_steps=99, allow_write=0)
        assert engine.agent_enabled is True
        assert engine.agent_max_steps == 20        # верхняя граница
        assert engine.agent_allow_write is False
        engine.set_agent_config(enabled=1, max_steps=0, allow_write=1)
        assert engine.agent_max_steps == 1         # нижняя граница

    def test_мусорный_max_steps_не_ломает(self, engine):
        engine.set_agent_config(enabled=True, max_steps="много")
        assert engine.agent_max_steps == 5

    def test_выключенный_агент_не_трогает_каркас(self, engine, monkeypatch):
        harness = _patch_harness(monkeypatch,
                                 result=HarnessResult(text="через каркас", used_tools=[]))
        engine.client.chat.return_value = iter([{"message": {"content": "обычный ответ"}}])
        tokens = []
        engine.on_token = tokens.append
        engine.set_agent_config(enabled=False)
        engine._is_busy = True
        engine._run_sync("любой вопрос")
        assert harness.run.called is False
        assert tokens == ["обычный ответ"]


class TestChatEngineAgentPath:
    def test_агентный_цикл_используется_и_эмитит_ответ(self, engine, monkeypatch):
        harness = _patch_harness(
            monkeypatch,
            result=HarnessResult(text="Запустил, сэр.", used_tools=["launch_app"], steps=2),
        )
        tokens, done = [], []
        engine.on_token = tokens.append
        engine.on_complete = done.append
        engine.set_agent_config(enabled=True, max_steps=3, allow_write=False)
        engine.conversation.append({"role": "user", "content": "запусти блокнот"})
        engine._is_busy = True
        engine._run_sync("запусти блокнот")

        assert tokens == ["Запустил, сэр."] and done == ["Запустил, сэр."]
        kwargs = harness.run.call_args.kwargs
        assert kwargs["max_steps"] == 3
        assert kwargs["allow_write"] is False
        # В историю попал только финальный ответ: tool-сообщения одноразовые
        assert engine.conversation[-1] == {"role": "assistant",
                                           "content": "Запустил, сэр."}
        assert all(m["role"] != "tool" for m in engine.conversation)
        engine.client.chat.assert_not_called()   # стриминг не запускался

    def test_ответ_с_инструментами_не_кэшируется(self, engine, monkeypatch):
        _patch_harness(
            monkeypatch,
            result=HarnessResult(text="Запустил, сэр.", used_tools=["launch_app"], steps=2),
        )
        engine.set_agent_config(enabled=True)
        engine._run_agent([{"role": "user", "content": "запусти блокнот"}],
                          "запусти блокнот")
        assert engine.get_cached_answer("запусти блокнот") is None

    def test_ответ_без_инструментов_кэшируется(self, engine, monkeypatch):
        _patch_harness(
            monkeypatch,
            result=HarnessResult(text="Столица Франции — Париж.", used_tools=[], steps=1),
        )
        engine.set_agent_config(enabled=True)
        engine._run_agent([{"role": "user", "content": "столица франции"}], "столица франции")
        assert engine.get_cached_answer("столица франции") == "Столица Франции — Париж."

    def test_сбой_каркаса_даёт_откат_на_стриминг(self, engine, monkeypatch):
        _patch_harness(monkeypatch, side_effect=RuntimeError("модель без tools"))
        engine.client.chat.return_value = iter([{"message": {"content": "Обычный ответ"}}])
        tokens = []
        engine.on_token = tokens.append
        engine.set_agent_config(enabled=True)
        engine._is_busy = True
        engine._run_sync("просто текст")
        assert tokens == ["Обычный ответ"]

    def test_сбой_модели_в_агентном_режиме_не_молчит(self, engine, monkeypatch):
        # Каркас упал, и обычный стриминг тоже: пользователь обязан увидеть ошибку,
        # а не тишину — иначе «Зевс просто не отвечает».
        _patch_harness(monkeypatch, side_effect=RuntimeError("модель без tools"))
        engine.client.chat.side_effect = ConnectionError("Ollama недоступна")
        errors = []
        engine.on_error = errors.append
        engine.set_agent_config(enabled=True)
        engine._is_busy = True
        engine._run_sync("любой вопрос")
        assert errors and "Ollama" in errors[0]
        assert engine.is_busy() is False     # флаг занятости снят в finally


# -----------------------------------------------------------------------
# 8. Настройки, контроллер, UI
# -----------------------------------------------------------------------
class TestAgentSettings:
    def test_ключи_в_дефолтах(self):
        ai = DEFAULT_SETTINGS["ai_settings"]
        assert ai["agent_enabled"] is False       # выключен по умолчанию
        assert ai["agent_max_steps"] == 5
        assert ai["agent_allow_write"] is False
        assert ai["agent_allow_actions"] is False

    def test_ключи_сохраняются_в_json(self, tmp_path):
        path = tmp_path / "settings.json"
        manager = config.SettingsManager(path)
        assert manager.set("ai_settings", "agent_enabled", True) is True
        assert manager.set("ai_settings", "agent_max_steps", 7) is True
        reloaded = config.SettingsManager(path)
        assert reloaded.get("ai_settings", "agent_enabled") is True
        assert reloaded.get("ai_settings", "agent_max_steps") == 7


def _shell_controller(ai_settings: dict) -> ZeusController:
    """Контроллер без тяжёлого __init__: только то, что нужно для настроек."""
    ctrl = object.__new__(ZeusController)
    ctrl.settings = {"ai_settings": dict(ai_settings), "voice_settings": {}}
    ctrl.settings_manager = MagicMock()
    ctrl.settings_manager.get_all.return_value = ctrl.settings
    ctrl.settings_manager.set.return_value = True
    ctrl.chat = MagicMock()
    return ctrl


class TestControllerAgentBridge:
    def test_apply_settings_передаёт_параметры_в_chat(self):
        ctrl = _shell_controller({"agent_enabled": True, "agent_max_steps": 4,
                                  "agent_allow_write": False})
        ctrl._apply_settings()
        ctrl.chat.set_agent_config.assert_called_once_with(
            enabled=True, max_steps=4, allow_write=False, allow_actions=False
        )

    def test_update_settings_применяется_мгновенно(self):
        ctrl = _shell_controller({"agent_enabled": False, "agent_max_steps": 5,
                                  "agent_allow_write": True})
        assert ctrl.update_settings("ai_settings", "agent_enabled", True) is True
        ctrl.chat.set_agent_config.assert_called_once()
        kwargs = ctrl.chat.set_agent_config.call_args.kwargs
        assert kwargs["enabled"] is True
        assert ctrl.settings["ai_settings"]["agent_enabled"] is True

    def test_update_settings_лимита_шагов(self):
        ctrl = _shell_controller({"agent_enabled": True, "agent_max_steps": 5,
                                  "agent_allow_write": True})
        ctrl.update_settings("ai_settings", "agent_max_steps", 9)
        assert ctrl.chat.set_agent_config.call_args.kwargs["max_steps"] == 9

    def test_update_settings_флага_записи(self):
        ctrl = _shell_controller({"agent_enabled": True, "agent_max_steps": 5,
                                  "agent_allow_write": True})
        ctrl.update_settings("ai_settings", "agent_allow_write", False)
        assert ctrl.chat.set_agent_config.call_args.kwargs["allow_write"] is False


class TestAgentUiToggles:
    def test_тумблеры_есть_в_карточке_модели(self):
        import inspect

        import src.ui.main_flet as mf

        source = inspect.getsource(mf.ZeusUI._build_settings_section)
        assert "agent_enabled" in source
        assert "agent_allow_write" in source
        assert "agent_allow_actions" in source
        # Тумблеры зарегистрированы и попадают в карточку «Модель и генерация»
        assert 'self._settings_widgets["agent_switch"]' in source
        assert 'self._settings_widgets["agent_write_switch"]' in source
        assert 'self._settings_widgets["agent_actions_switch"]' in source
        assert source.count("ft.Switch(") >= 2
