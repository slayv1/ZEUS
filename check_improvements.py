import os, sys, json, time, shutil, tempfile, datetime
import urllib.parse
sys.path.insert(0, 'src')

from core import actions
import core.chat_engine as ce

# мокаем subprocess, чтобы lock/minimize не выполнялись на самом ПК
import subprocess, types
_orig_popen = subprocess.Popen
subprocess.Popen = lambda *a, **k: types.SimpleNamespace(wait=lambda: 0, returncode=0)
actions.subprocess = subprocess

# === 1. Новые команды actions ===
r = actions._handle_time_date_command("который сейчас час")
assert r and r["action"] == "time_date" and "сэр" in r["message"].lower(), r

r = actions._handle_calc_command("сколько будет двенадцать умножить на восемь")
assert r and "96" in r["message"], r
r = actions._handle_calc_command("посчитай 5 плюс 3")
assert r and "8" in r["message"], r
r = actions._handle_calc_command("2 плюс 2")
assert not r  # без триггера — None
assert actions._handle_calc_command("привет друг") is None

r = actions._handle_lock_pc_command("заблокируй компьютер")
assert r and r["action"] == "lock_pc" and r["success"], r
assert actions._handle_lock_pc_command("просто текст") is None

r = actions._handle_minimize_all_command("сверни все окна")
assert r and r["action"] == "minimize_all" and r["success"], r

# === 2. Заметки ===
tmpdir = tempfile.mkdtemp()
try:
    import core.config as cfg
    cfg.data_path = lambda name="": os.path.join(tmpdir, name)
    actions._notes_path = lambda: cfg.data_path("notes.txt")
    add = actions._handle_notes_command("сделай заметку купить молоко")
    assert add and add["success"], add
    rd = actions._handle_notes_command("прочитай мои заметки")
    assert rd and "молоко" in rd["message"], rd
    assert actions._handle_notes_command("просто текст") is None
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

# === 3. chat history cap ===
eng = ce.ChatEngine.__new__(ce.ChatEngine)
eng.conversation = []
for i in range(50):
    eng.conversation.append({"role": "user", "content": f"msg{i}"})
    eng.conversation.append({"role": "assistant", "content": f"ans{i}"})
sent = list(eng.conversation[-ce._MAX_HISTORY:])
assert len(sent) == ce._MAX_HISTORY == 16, len(sent)

# === 4. steam_store — кеш, скидка, best_match (мокаем сеть) ===
import core.steam_store as ss
# Замокаем urlopen: вернём валидный JSON-ответ Valve.
class _FakeResp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b'{"items":[{"id":1091500,"name":"Cyberpunk 2077","price":{"final":71000,"currency":"RUB","discount_percent":50}}]}'
def _fake_urlopen(req, timeout=0):
    return _FakeResp()
_orig_urlopen = ss.urllib.request.urlopen
ss.urllib.request.urlopen = _fake_urlopen
try:
    ss.clear_search_cache()
    r1 = ss.search_store("киберпанк")
    assert r1 and r1[0]["name"] == "Cyberpunk 2077", r1
    assert r1[0]["price_str"] and "710" in r1[0]["price_str"], r1[0]["price_str"]
    assert r1[0]["discount_percent"] == 50, r1[0]
    # второй вызов — должен прийти из кеша (urlopen бы упал)
    ss.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("сеть не должна вызываться"))
    r2 = ss.search_store("киберпанк")
    assert r1 == r2, "кеш должен вернуть те же данные"
    assert ss._SEARCH_CACHE, "кеш должен быть заполнен"
finally:
    ss.urllib.request.urlopen = _orig_urlopen
    ss.clear_search_cache()

# === 5. execute_command_safely — не блокирует и не роняет (daemon-поток) ===
import threading as _th
from core.controller import execute_command_safely

_flag = {"done": False}
def _slow():
    import time
    time.sleep(0.2)
    _flag["done"] = True

t0 = time.time()
execute_command_safely(_slow)
elapsed = time.time() - t0
# Обёртка должна вернуться мгновенно (< 0.1 c), а задача доработать в фоне.
assert elapsed < 0.1, f"обёртка заблокировала вызов: {elapsed:.3f}s"
_fh_deadline = time.time() + 2.0
while not _flag["done"] and time.time() < _fh_deadline:
    time.sleep(0.02)
assert _flag["done"], "фоновая задача не выполнилась"

# === 6. Taobao: очистка + URL-кодирование (браузер не открываем) ===
from modules import taobao_core
# Не открываем реальный браузер в тесте.
taobao_core.webbrowser.open = lambda *a, **k: True
try:
    assert taobao_core.clean_query("найди пуховик на таобао") == "пуховик", \
        taobao_core.clean_query("найди пуховик на таобао")
    assert taobao_core.clean_query("  зевс найди мне кроссовки  ") == "кроссовки"
    # Пустой запрос после очистки
    assert taobao_core.clean_query("найди на таобао") == ""
    # Проверка кодирования
    query = taobao_core.clean_query("найди куртка на таобао")
    encoded = urllib.parse.quote(query)
    assert encoded == "%D0%BA%D1%83%D1%80%D1%82%D0%BA%D0%B0", encoded
finally:
    pass
# Интеграция: командный handler
r = actions._handle_taobao_command("найди пуховик на таобао")
assert r and r["action"] == "taobao" and r["success"], r
assert "пуховик" in r["query"], r
assert actions._handle_taobao_command("как дела") is None

# Устойчивость к искажённому распознаванию (Vosk коверкает «на таобао»)
r = actions._handle_taobao_command("найди пуховик натал бал")
assert r and r["action"] == "taobao" and r.get("query") == "пуховик", r
r = actions._handle_taobao_command("да сэр найди пуховик на таю балу")
assert r and r["action"] == "taobao" and r.get("query") == "пуховик", r
# Обычная фраза без тао-контекста НЕ должна перехватываться
assert actions._handle_taobao_command("найди папку контакты") is None

print("ALL TESTS PASSED")

