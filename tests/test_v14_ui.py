"""Тесты для ZEUS v1.4 UI компонентов (ТЗ v1.4)."""
import sys, os, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from ui.zeus_state import ZeusState, ZeusStateController
from ui.theme import ThemeManager, PALETTES, _theme_file_path

def _clean_theme_file():
    p=_theme_file_path()
    if os.path.exists(p):
        os.unlink(p)

class TestZeusState:
    def test_initial(self):
        c=ZeusStateController(ZeusState.IDLE)
        assert c.state==ZeusState.IDLE
    def test_set_state(self):
        c=ZeusStateController(ZeusState.IDLE)
        c.set_state(ZeusState.LISTENING)
        assert c.state==ZeusState.LISTENING
    def test_callback(self):
        e=[]
        c=ZeusStateController(ZeusState.IDLE)
        c.on_change(lambda o,n:e.append((o,n)))
        c.set_state(ZeusState.THINKING)
        assert len(e)==1
        assert e[0]==(ZeusState.IDLE, ZeusState.THINKING)
    def test_idempotent(self):
        e=[]
        c=ZeusStateController(ZeusState.IDLE)
        c.on_change(lambda o,n:e.append(n))
        c.set_state(ZeusState.IDLE)
        assert len(e)==0
    def test_is_active(self):
        c=ZeusStateController(ZeusState.IDLE)
        assert c.is_active()==False
        c.set_state(ZeusState.LISTENING)
        assert c.is_active()==True

class TestTheme:
    def setup_method(self):
        _clean_theme_file()
    def teardown_method(self):
        _clean_theme_file()
    def test_initial_dark(self):
        tm=ThemeManager(initial="dark")
        assert tm.get_theme()=="dark"
    def test_set_theme(self):
        tm=ThemeManager(initial="dark")
        assert tm.set_theme("light")==True
        assert tm.get_theme()=="light"
    def test_toggle(self):
        tm=ThemeManager(initial="dark")
        assert tm.toggle_theme()=="light"
        assert tm.get_theme()=="light"
        assert tm.toggle_theme()=="dark"
        assert tm.get_theme()=="dark"
    def test_invalid(self):
        tm=ThemeManager(initial="dark")
        assert tm.set_theme("invalid")==False
        assert tm.get_theme()=="dark"
    def test_persistence(self):
        tm=ThemeManager(initial="dark")
        tm.set_theme("light")
        tm2=ThemeManager(initial="dark")
        assert tm2.get_theme()=="light"

class TestPalettes:
    def test_dark(self):
        assert PALETTES["dark"]["bg"]=="#050816"
        assert PALETTES["dark"]["accent"]=="#2563EB"
    def test_light(self):
        assert PALETTES["light"]["bg"]=="#F1F5F9"
        assert PALETTES["light"]["accent"]=="#2563EB"

class TestZeusStateEnum:
    def test_states(self):
        assert ZeusState.IDLE.value=="idle"
        assert ZeusState.LISTENING.value=="listening"
        assert ZeusState.THINKING.value=="thinking"
        assert ZeusState.SPEAKING.value=="speaking"
        assert ZeusState.ERROR.value=="error"
