"""Тесты движка чата (chat_engine.py).

Проверяют, что клиент Ollama создаётся с параметром host,
а вызов chat() не содержит устаревшего аргумента host.

Опираются на inspect.getsource, а не на реальный запуск —
ollama.Client(host=) требует живого сервера, а тесты проверяют
только статическую корректность кода.
"""
import inspect
import os

from src.core.chat_engine import ChatEngine  # noqa: E402


def test_chat_engine_creates_client_with_host():
    """__init__ должен создавать ollama.Client(host=...)."""
    src = inspect.getsource(ChatEngine.__init__)
    assert "ollama.Client(" in src
    assert "host=" in src, "host должен передаваться в конструктор Client"
    # Должно быть ровно одно обращение к ollama.Client
    assert src.count("ollama.Client(") == 1


def test_chat_engine_init_has_host_param():
    """__init__ должен принимать параметр host."""
    sig = inspect.signature(ChatEngine.__init__)
    assert "host" in sig.parameters, "ChatEngine.__init__ должен иметь параметр host"


def test_run_sync_uses_client_chat():
    """_run_sync должен использовать self.client.chat, а не ollama.chat(..., host=)."""
    src = inspect.getsource(ChatEngine._run_sync)
    assert "self.client.chat(" in src, "должен использоваться self.client.chat"
    assert "ollama.chat(" not in src, "не должно быть прямого ollama.chat()"
    assert "host=" not in src, "host не должен передаваться в chat()"

