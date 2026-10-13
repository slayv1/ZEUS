# -*- coding: utf-8 -*-
"""v1.4: один контур команд, один автомат, подтверждение агента, шаблон базы."""
import inspect
import json

from core import actions, actions_launch, command_effects
from core.agent_harness import (
    AgentHarness,
    classify_confirmation,
)
from core.agent_tools import RISK_ACTION, RISK_SAFE, Tool, ToolRegistry
from core.assistant_state import ZeusState, ZeusStateController
from core.command_router import router
from ui.layout import ZeusLayoutMixin


class _ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("модель получила больше запросов, чем ожидалось")
        return self.responses.pop(0)


def _response(text, tool_calls=None):
    return {"message": {"content": text, "tool_calls": tool_calls or []}}


def _tool_call(tool, **arguments):
    return {"function": {"name": tool, "arguments": arguments}}


def test_диспетчер_держит_общие_эффекты():
    assert router.routes["run_command"] is actions.execute_command
    assert router.routes["launch_app"] is command_effects.launch_named
    assert router.routes["close_app"] is command_effects.close_named
    assert router.routes["open_url"] is command_effects.open_url
    assert router.routes["web_search"] is command_effects.search_web
    assert router.call("close_app", "")["success"] is False


def test_голос_и_агент_открывают_ссылку_одной_функцией(monkeypatch):
    opened = []
    monkeypatch.setattr(
        "webbrowser.open", lambda url, *a, **k: opened.append(url) or True
    )
    voice = command_effects.open_web_search("курс доллара")
    agent = command_effects.open_url("https://zeus.local")
    assert voice["url"].startswith("https://www.google.com/search?q=")
    assert agent["success"] is True
    assert opened[0] == voice["url"]
    assert opened[1] == "https://zeus.local"


def test_автомат_переводит_строки_микрофона():
    state = ZeusStateController()
    seen = []
    state.on_change(lambda old, new: seen.append(new))
    state.apply_voice("LISTENING")
    state.apply_voice("PROCESSING")
    state.apply_voice("SPEAKING")
    state.apply_voice("ERROR")
    assert [item for item in seen] == [
        ZeusState.LISTENING,
        ZeusState.THINKING,
        ZeusState.SPEAKING,
        ZeusState.ERROR,
    ]
    assert state.state is ZeusState.ERROR


def test_neural_core_единственный_экран():
    src = inspect.getsource(ZeusLayoutMixin)
    assert "ZeusMainWindow(" not in src
    assert "_toggle_v14_view" not in src
    assert "hologram.webp" in src


def test_действие_ждёт_да(monkeypatch):
    ran = []

    def launch(name):
        ran.append(name)
        return {"success": True, "message": f"Запустил {name}"}

    registry = ToolRegistry()
    registry.register(Tool(
        "launch_app", "запуск",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        launch,
        risk=RISK_ACTION,
    ))
    client = _ScriptedClient([
        _response("", [_tool_call("launch_app", name="блокнот")]),
        _response("Запустил, сэр."),
    ])
    harness = AgentHarness(registry)
    paused = harness.run(client, model="m", messages=[], allow_actions=True)
    assert paused.pending is not None
    assert ran == []
    assert "да" in paused.text.lower()

    done = harness.resume(
        client, paused.pending, approved=True, model="m"
    )
    assert ran == ["блокнот"]
    assert done.text == "Запустил, сэр."
    assert done.pending is None
    assert "launch_app" in done.used_tools


def test_нет_не_запускает_действие():
    ran = []
    registry = ToolRegistry()
    registry.register(Tool(
        "click", "клик", {"type": "object", "properties": {}},
        lambda **kw: ran.append(kw) or {"success": True},
        risk=RISK_ACTION,
    ))
    client = _ScriptedClient([
        _response("", [_tool_call("click", x=1, y=2)]),
        _response("Отменил, сэр."),
    ])
    harness = AgentHarness(registry)
    paused = harness.run(client, model="m", messages=[], allow_actions=True)
    done = harness.resume(client, paused.pending, approved=False, model="m")
    assert ran == []
    assert done.text == "Отменил, сэр."
    payload = json.loads(client.calls[1]["messages"][-1]["content"])
    assert "отклонил" in payload["error"]


def test_выключенные_действия_не_спрашивают_да():
    registry = ToolRegistry()
    registry.register(Tool(
        "launch_app", "запуск", {"type": "object", "properties": {}},
        lambda **kw: {"success": True},
        risk=RISK_ACTION,
    ))
    client = _ScriptedClient([
        _response("", [_tool_call("launch_app", name="x")]),
        _response("Нельзя."),
    ])
    result = AgentHarness(registry).run(
        client, model="m", messages=[], allow_actions=False
    )
    assert result.pending is None
    assert result.text == "Нельзя."


def test_безопасный_инструмент_без_вопроса():
    registry = ToolRegistry()
    registry.register(Tool(
        "read_file", "чтение", {"type": "object", "properties": {}},
        lambda **kw: {"success": True, "content": "ок"},
        risk=RISK_SAFE,
    ))
    client = _ScriptedClient([
        _response("", [_tool_call("read_file", path="a.txt")]),
        _response("Прочитал."),
    ])
    result = AgentHarness(registry).run(client, model="m", messages=[])
    assert result.pending is None
    assert result.text == "Прочитал."


def test_классификация_подтверждения():
    assert classify_confirmation("Да, сэр") == "yes"
    assert classify_confirmation("нет") == "no"
    assert classify_confirmation("открой блокнот") is None


def test_шаблон_базы_только_если_файла_нет(tmp_path, monkeypatch):
    path = tmp_path / "commands.json"
    template = tmp_path / "commands.template.json"
    template.write_text(
        json.dumps({"приложения": {}, "игры": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(actions_launch, "_commands_path", lambda: str(path))
    actions_launch.ensure_commands_file()
    assert json.loads(path.read_text(encoding="utf-8"))["приложения"] == {}

    path.write_text(
        json.dumps({"приложения": {"chrome": "C:/chrome.exe"}, "игры": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    actions_launch.ensure_commands_file()
    assert "chrome" in json.loads(path.read_text(encoding="utf-8"))["приложения"]
