"""Интеграционные тесты ТЗ: системный трей и жизненный цикл окна Zeus UI.

Покрывают критерии приёмки (Definition of Done):
  1. Иконка «Зевса» запускается в трее (TrayManager.start() -> True, icon.run()).
  2. Крестик (X) при активном трее скрывает окно, а не завершает процесс.
  3. «Открыть» (в т.ч. двойной клик) возвращает окно на передний план.
  4. «Выход» останавливает трей, watcher, хоткеи, анимацию и закрывает окно.
  5. Без поддержки трея (pystray недоступен) — штатная деградация: крестик
     завершает приложение, ошибок нет.

Реальные Flet-приложение и иконка в трее не запускаются: page/window и
pystray/PIL подменяются фейками, ZeusUI создаётся без __init__ (без потоков
и тяжёлых движков).
"""
import sys
import types

import ui.boot as boot
import src.ui.main_flet as mf
from modules.tray import TrayManager


# ----------------------------------------------------------------------
# Фейковые страница/окно Flet (минимальный API, используемый ZeusUI)
# ----------------------------------------------------------------------
class FakeWindow:
    def __init__(self):
        self.visible = True
        self.minimized = False
        self.focused = False
        self.prevent_close = True
        self.close_calls = 0
        self.bring_to_front_calls = 0
        # API Flet >= 0.85: события окна через window.on_event
        self.on_event = None
        # Чек-лист ТЗ №4: значение prevent_close в момент вызова close()
        self.prevent_close_at_close = None

    async def close(self):
        self.close_calls += 1
        self.prevent_close_at_close = self.prevent_close

    async def bring_to_front(self):
        self.bring_to_front_calls += 1


class FakePage:
    def __init__(self):
        self.window = FakeWindow()
        self.update_calls = 0
        self.run_task_calls = []

    def update(self):
        self.update_calls += 1

    def run_task(self, fn, *args):
        """Flet-совместимо: принимает корутинную функцию и запускает её.

        Здесь корутина выполняется синхронно до конца, чтобы тест мог
        сразу проверить её эффект (например window.close_calls).
        """
        self.run_task_calls.append(fn)
        coro = fn(*args)
        try:
            coro.send(None)
        except StopIteration:
            pass


class FakeEvent:
    def __init__(self, data):
        self.data = data


class FakeTray:
    """Подмена TrayManager на уровне ZeusUI._start_tray."""

    def __init__(self, **kwargs):
        self.start_result = kwargs.get("start_result", True)
        self.started = False
        self.stopped = False
        self.kwargs = kwargs

    def start(self):
        self.started = True
        return self.start_result

    def stop(self):
        self.stopped = True


def make_ui() -> "mf.ZeusUI":
    """ZeusUI без __init__ (без фоновых потоков и движков)."""
    ui = mf.ZeusUI.__new__(mf.ZeusUI)
    ui.page = FakePage()
    ui._tray = None
    ui._hotkey = None
    ui._watcher_started = False
    ui._exit_requested = False
    ui._holo_running = True
    # Атрибуты Спринта 1 (аудио-подменю трея и мост [AUDIO]-логов):
    # _start_tray передаёт on_pick_* в TrayManager и читает self.controller;
    # без них заглушка падала в except и трей оставался None.
    ui.controller = None
    ui._on_tray_pick_input = lambda device_id: None
    ui._on_tray_pick_output = lambda device_id: None
    ui._core_log_unsubscribe = None
    # UI-задачи выполняем синхронно, чтобы проверять эффект сразу
    ui._post_ui = lambda fn: fn()
    ui._log = lambda *a, **k: None
    return ui


# ----------------------------------------------------------------------
# DoD №1: TrayManager — реальный класс, pystray/PIL подменены
# ----------------------------------------------------------------------
def _install_fake_pystray(monkeypatch):
    fake = types.ModuleType("pystray")

    class FakeIcon:
        instances: list = []
        fail_run = False  # True -> имитирует падение цикла pystray при старте

        def __init__(self, name, image, title, menu):
            self.name, self.image, self.title, self.menu = name, image, title, menu
            self.visible = False
            self.run_called = False
            self.stop_called = False
            self.setup = None
            FakeIcon.instances.append(self)

        def run(self, setup=None):
            self.run_called = True
            self.setup = setup
            if FakeIcon.fail_run:
                raise RuntimeError("tray backend unavailable")
            if setup is not None:
                setup(self)

        def stop(self):
            self.stop_called = True

    fake.Icon = FakeIcon
    fake.Menu = lambda *items: ("menu", items)
    fake.MenuItem = lambda *a, **kw: ("item", a, kw)

    pil = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")
    draw_mod = types.ModuleType("PIL.ImageDraw")

    class FakeImg:
        def convert(self, mode):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeDraw:
        def __init__(self, img):
            pass

        def ellipse(self, *a, **kw):
            pass

        def polygon(self, *a, **kw):
            pass

    image_mod.open = lambda path: FakeImg()
    image_mod.new = lambda *a, **kw: FakeImg()
    draw_mod.Draw = FakeDraw
    pil.Image = image_mod
    pil.ImageDraw = draw_mod

    monkeypatch.setitem(sys.modules, "pystray", fake)
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_mod)
    monkeypatch.setitem(sys.modules, "PIL.ImageDraw", draw_mod)
    return FakeIcon


def test_tray_menu_items_call_callbacks():
    """Меню трея вызывает ровно те колбэки, что переданы конструктору."""
    calls = []
    tray = TrayManager(
        on_open=lambda: calls.append("open"),
        on_settings=lambda: calls.append("settings"),
        on_exit=lambda: calls.append("exit"),
    )
    tray._open()
    tray._settings()
    tray._exit()
    assert calls == ["open", "settings", "exit"]


def test_tray_start_runs_icon_and_stop_stops_it(monkeypatch):
    """DoD №1 и №4: start() поднимает иконку (icon.run), stop() глушит её."""
    FakeIcon = _install_fake_pystray(monkeypatch)
    tray = TrayManager(
        on_open=lambda: None,
        on_settings=lambda: None,
        on_exit=lambda: None,
        icon_path=None,  # fallback-иконка, рисуется через PIL
    )
    assert tray.start() is True
    assert FakeIcon.instances, "иконка pystray не создана"
    icon = FakeIcon.instances[0]
    assert icon.run_called, "цикл трея не запущен"
    assert icon.menu is not None
    # Handshake: setup-колбэк сделал иконку видимой
    assert icon.visible is True

    tray.stop()
    assert icon.stop_called


def test_tray_start_returns_false_when_run_loop_fails(monkeypatch):
    """Баг-фикс: цикл pystray упал при старте -> start() обязан вернуть False.

    Раньше start() возвращал True сразу после запуска потока, UI считал трей
    рабочим и прятал окно по крестику — получался зомби-процесс без иконки.
    """
    FakeIcon = _install_fake_pystray(monkeypatch)
    monkeypatch.setattr(FakeIcon, "fail_run", True)
    tray = TrayManager(
        on_open=lambda: None,
        on_settings=lambda: None,
        on_exit=lambda: None,
        icon_path=None,
    )
    assert tray.start() is False


def test_tray_callbacks_are_exception_safe():
    """Баг-фикс: исключение в колбэке не должно убивать цикл трея."""
    def boom():
        raise RuntimeError("ui error")

    tray = TrayManager(on_open=boom, on_settings=boom, on_exit=boom)
    tray._open()      # не должно бросать исключений
    tray._settings()
    tray._exit()


def test_tray_start_degrades_without_pystray(monkeypatch):
    """DoD №5: pystray недоступен -> start() вернёт False без исключений."""
    monkeypatch.setitem(sys.modules, "pystray", None)
    tray = TrayManager(
        on_open=lambda: None,
        on_settings=lambda: None,
        on_exit=lambda: None,
        icon_path=None,
    )
    assert tray.start() is False


# ----------------------------------------------------------------------
# DoD №2/№5: ZeusUI._start_tray и развилка в _on_window_event
# ----------------------------------------------------------------------
def test_start_tray_wires_menu_actions(monkeypatch):
    """ТЗ §3.2: _start_tray привязывает «Открыть/Настройки/Выход» и иконку."""
    ui = make_ui()
    sections = []
    exits = []

    created = {}

    class RecordingTray(FakeTray):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            created.update(kwargs)

    monkeypatch.setattr(boot, "TrayManager", RecordingTray)
    monkeypatch.setattr(ui, "_select_section", lambda idx: sections.append(idx), raising=False)
    monkeypatch.setattr(ui, "_exit_application", lambda: exits.append(1), raising=False)

    ui._start_tray()

    assert ui._tray is not None and ui._tray.started
    assert created["icon_path"].replace("\\", "/").endswith("assets/icons/app_icon.ico")

    # «Открыть» -> окно возвращается на передний план (DoD №3)
    created["on_open"]()
    assert ui.page.window.visible is True
    assert ui.page.window.minimized is False
    assert ui.page.window.focused is True
    assert ui.page.window.bring_to_front_calls == 1

    # «Настройки» -> раздел 2 (ТЗ §2.1)
    created["on_settings"]()
    assert sections == [2]

    # «Выход» -> полное завершение (DoD №4)
    created["on_exit"]()
    assert exits == [1]


def test_start_tray_failure_leaves_no_tray(monkeypatch):
    """DoD №5: трей не стартовал -> _tray=None, крестик завершает приложение."""
    ui = make_ui()
    monkeypatch.setattr(
        boot, "TrayManager", lambda **kw: FakeTray(**kw, start_result=False)
    )
    ui._start_tray()
    assert ui._tray is None

    ui._on_window_event(FakeEvent("close"))
    assert ui._exit_requested is True
    assert ui.page.window.prevent_close is False
    assert ui.page.window.close_calls == 1


def test_assemble_ui_starts_tray_in_chain(monkeypatch):
    """ТЗ §3.2: _start_tray входит в цепочку сборки _assemble_ui.

    Хоткей (_start_hotkey) и свернуто-старт (_apply_start_minimized)
    из цепочки удалены — вызываться не должны.
    """
    ui = make_ui()
    calls = []
    for name in (
        "_build_ui",
        "_setup_controller",
        "_init_file_watcher",
        "_start_tray",
        "_subscribe_core_logs",  # мост [AUDIO]-логов (Спринт 1)
    ):
        monkeypatch.setattr(ui, name, lambda n=name: calls.append(n), raising=False)
    removed = []
    for name in ("_start_hotkey", "_apply_start_minimized"):
        monkeypatch.setattr(ui, name, lambda n=name: removed.append(n), raising=False)

    ui._assemble_ui()
    assert "_start_tray" in calls
    assert calls.index("_start_tray") > calls.index("_init_file_watcher")
    assert "_subscribe_core_logs" in calls
    assert removed == [], "хоткей и старт-в-трее должны быть отключены"


def test_removed_features_are_gone_from_code():
    """Тумблеры «Запускать в свернутом виде» и «Ctrl+Alt+Space» удалены из UI."""
    import inspect

    settings_src = inspect.getsource(mf.ZeusUI._build_settings_section)
    assert "start_minimized_sw" not in settings_src
    assert "hotkey_sw" not in settings_src
    assert "Глобальная клавиша вызова" not in settings_src
    assert "Запускать в свернутом виде" not in settings_src
    # Трей при этом остаётся
    assert "_start_tray" in inspect.getsource(mf.ZeusUI._assemble_ui)
    assert "_start_hotkey" not in inspect.getsource(mf.ZeusUI._assemble_ui)
    assert "_apply_start_minimized" not in inspect.getsource(mf.ZeusUI._assemble_ui)


def test_init_file_watcher_intercepts_close(monkeypatch):
    """ТЗ §2.2: prevent_close=True и обработчик привязан к API Flet >= 0.85.

    КЛЮЧЕВОЙ РЕГРЕССИОННЫЙ ТЕСТ: раньше обработчик присваивался
    page.on_window_event — атрибуту, которого в Flet 0.85 больше нет,
    из-за чего событие закрытия не доходило и окно нельзя было закрыть.
    """
    ui = make_ui()
    monkeypatch.setattr(boot, "start_watching", lambda callback=None: True)
    ui._init_file_watcher()
    assert ui.page.window.prevent_close is True
    assert ui.page.window.on_event == ui._on_window_event
    assert ui._watcher_started is True


def test_window_event_new_flet_shape_closes_and_resizes():
    """События в формате Flet >= 0.85 (WindowEvent.type, значение 'close')."""
    ui = make_ui()
    ui._tray = FakeTray()

    # close в новом формате -> скрытие в трей
    ui._on_window_event(types.SimpleNamespace(type="close"))
    assert ui.page.window.visible is False
    assert ui.page.window.close_calls == 0

    # resize в новом формате -> только обновление layout
    ui.page.window.visible = True
    ui._on_window_event(types.SimpleNamespace(type="resize"))
    assert ui.page.window.visible is True
    assert ui.page.window.close_calls == 0

    # enum-значение (WindowEventType.CLOSE == 'close') тоже распознаётся
    class FakeEnum:
        value = "close"

    ui._on_window_event(types.SimpleNamespace(type=FakeEnum()))
    assert ui.page.window.visible is False


def test_window_event_old_flet_shape_still_works():
    """Обратная совместимость: события старых Flet с полем .data."""
    ui = make_ui()
    ui._tray = FakeTray()
    ui._on_window_event(FakeEvent("close"))
    assert ui.page.window.visible is False


# ----------------------------------------------------------------------
# DoD №2: крестик при активном трее прячет окно
# ----------------------------------------------------------------------
def test_show_window_reasserts_close_interception():
    """После цикла «X -> скрыть в трей -> Открыть» перехват крестика активен.

    _show_window обязан заново выставить prevent_close=True и привязать
    _on_window_event, даже если клиент Flet их сбросил, — иначе крестик
    после восстановления окна завершит приложение вместо скрытия в трей.
    """
    ui = make_ui()
    ui._tray = FakeTray()

    # 1) Крестик прячет окно в трей
    ui._on_window_event(FakeEvent("close"))
    assert ui.page.window.visible is False

    # 2) Имитируем сброс перехвата клиентом Flet за время скрытия
    ui.page.window.prevent_close = False
    ui.page.window.on_event = None

    # 3) «Открыть» из трея возвращает окно
    ui._show_window()
    assert ui.page.window.visible is True
    assert ui.page.window.prevent_close is True, "перехват крестика восстановлен"
    assert ui.page.window.on_event == ui._on_window_event

    # 4) Крестик после восстановления снова скрывает в трей, а не закрывает
    ui._on_window_event(FakeEvent("close"))
    assert ui.page.window.visible is False
    assert ui.page.window.close_calls == 0
    assert ui._exit_requested is False


def test_close_event_hides_window_to_tray():
    ui = make_ui()
    ui._tray = FakeTray()
    ui._on_window_event(FakeEvent("close"))
    assert ui.page.window.visible is False
    assert ui.page.window.prevent_close is True, "процесс не должен завершаться"
    assert ui.page.window.close_calls == 0
    assert ui.page.update_calls >= 1


def test_resize_event_does_not_touch_lifecycle():
    ui = make_ui()
    ui._tray = FakeTray()
    ui._on_window_event(FakeEvent("resize"))
    assert ui.page.window.visible is True
    assert ui.page.window.close_calls == 0
    assert ui._exit_requested is False


# ----------------------------------------------------------------------
# DoD №4: полное завершение и остановка фоновых служб
# ----------------------------------------------------------------------
def test_shutdown_background_services_stops_everything(monkeypatch):
    ui = make_ui()
    ui._tray = FakeTray()
    ui._watcher_started = True
    watcher_stops = []
    monkeypatch.setattr(boot, "stop_watching", lambda: watcher_stops.append(1))
    tray = ui._tray

    ui._shutdown_background_services()

    assert tray.stopped and ui._tray is None
    assert watcher_stops == [1] and ui._watcher_started is False
    assert ui._holo_running is False  # цикл анимации остановлен


def test_shutdown_background_services_stops_hotkeys(monkeypatch):
    """ТЗ §2.3: глобальные горячие клавиши останавливаются при выходе."""
    ui = make_ui()
    hotkey_stops = []

    class FakeHotkey:
        def stop(self):
            hotkey_stops.append(1)

    ui._hotkey = FakeHotkey()
    monkeypatch.setattr(boot, "stop_watching", lambda: None)

    ui._shutdown_background_services()

    assert hotkey_stops == [1], "горячие клавиши должны быть остановлены"
    assert ui._hotkey is None


def test_shutdown_background_services_is_idempotent(monkeypatch):
    ui = make_ui()
    ui._tray = FakeTray()
    ui._watcher_started = True
    monkeypatch.setattr(boot, "stop_watching", lambda: None)

    ui._shutdown_background_services()
    ui._shutdown_background_services()  # повторный вызов не падает

    assert ui._tray is None


def test_close_event_with_exit_requested_shuts_down(monkeypatch):
    """«close» после запроса выхода: выгрузка служб + штатное закрытие окна."""
    ui = make_ui()
    ui._tray = FakeTray()
    ui._watcher_started = True
    ui._exit_requested = True
    watcher_stops = []
    monkeypatch.setattr(boot, "stop_watching", lambda: watcher_stops.append(1))

    ui._on_window_event(FakeEvent("close"))

    assert ui._tray is None and watcher_stops == [1]
    assert ui.page.window.prevent_close is False
    assert ui.page.window.close_calls == 1
    # Чек-лист №4: prevent_close сброшен ДО вызова close()
    assert ui.page.window.prevent_close_at_close is False


def test_close_event_twice_with_exit_requested_does_not_crash(monkeypatch):
    """Повторный «close» (наш programmatic close -> событие снова) не ломает поток."""
    ui = make_ui()
    ui._tray = FakeTray()
    ui._exit_requested = True
    monkeypatch.setattr(boot, "stop_watching", lambda: None)

    ui._on_window_event(FakeEvent("close"))
    ui._on_window_event(FakeEvent("close"))  # без исключений и без зацикливания

    assert ui.page.window.prevent_close is False
    assert ui.page.window.close_calls >= 1


def test_exit_application_full_lifecycle(monkeypatch):
    """«Выход» в трее: сброс prevent_close, остановка служб, закрытие окна."""
    ui = make_ui()
    ui._tray = FakeTray()
    ui._watcher_started = True
    watcher_stops = []
    monkeypatch.setattr(boot, "stop_watching", lambda: watcher_stops.append(1))

    ui._exit_application()

    assert ui._exit_requested is True
    assert ui._tray is None and watcher_stops == [1]
    assert ui.page.window.prevent_close is False
    assert ui.page.run_task_calls, "close должен вызываться через run_task"
    assert ui.page.window.close_calls == 1, "окно должно закрыться штатно"
    # Чек-лист №4: prevent_close был False уже в момент close()
    assert ui.page.window.prevent_close_at_close is False


def test_close_window_helper_handles_sync_close_api(monkeypatch):
    """_close_window корректен и для синхронного Window.close (другие версии Flet)."""
    ui = make_ui()

    class SyncCloseWindow(FakeWindow):
        def close(self):  # не async
            self.close_calls += 1
            self.prevent_close_at_close = self.prevent_close

    ui.page.window = SyncCloseWindow()
    ui._close_window()
    assert ui.page.window.close_calls == 1
    assert ui.page.window.prevent_close_at_close is False
    assert ui.page.run_task_calls == []  # sync-вариант без run_task





# ----------------------------------------------------------------------
# Спринт 1 (v1.4): выбор устройства в меню трея → контроллер
# ----------------------------------------------------------------------
class FakeAudioTray:
    """Трей, сохраняющий последние переданные устройства (проверка «галочек»)."""

    def __init__(self):
        self.started = False
        self.stopped = False
        self.audio_updates: list = []

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.stopped = True

    def update_audio_devices(self, devices_in, devices_out, active_in, active_out):
        self.audio_updates.append(
            (devices_in, devices_out, active_in, active_out)
        )


class FakeAudioController:
    """Контроллер-заглушка для проверок трей ↔ контроллер."""

    def __init__(self):
        self.inputs = [{"id": "m1", "name": "Мик 1"}, {"id": "m2", "name": "Мик 2"}]
        self.outputs = [{"id": "s1", "name": "Динамик"}]
        self.settings = {"voice_settings": {
            "input_device_id": "m1", "output_device_id": None}}
        self.input_calls: list = []
        self.output_calls: list = []
        self.on_tray_audio_update = None

    def get_available_input_devices(self):
        return self.inputs

    def get_available_output_devices(self):
        return self.outputs

    def set_input_device(self, device_id):
        self.input_calls.append(device_id)
        self.settings["voice_settings"]["input_device_id"] = device_id

    def set_output_device(self, device_id):
        self.output_calls.append(device_id)
        self.settings["voice_settings"]["output_device_id"] = device_id

    def stop_device_watcher(self):
        pass


def _ui_with_audio_tray() -> "mf.ZeusUI":
    ui = make_ui()
    ui._tray = FakeAudioTray()
    ui.controller = FakeAudioController()
    return ui


class TestTrayToController:
    def test_pick_input_delegates_and_marks_active_in_menu(self):
        ui = _ui_with_audio_tray()
        mf.ZeusUI._on_tray_pick_input(ui, "m2")
        assert ui.controller.input_calls == ["m2"]
        # «галочка» в подменю: активный ввод передан в трей
        assert ui._tray.audio_updates[-1][2] == "m2"

    def test_pick_output_delegates_and_marks_active_in_menu(self):
        ui = _ui_with_audio_tray()
        mf.ZeusUI._on_tray_pick_output(ui, "s1")
        assert ui.controller.output_calls == ["s1"]
        assert ui._tray.audio_updates[-1][3] == "s1"

    def test_pick_auto_option_passes_none(self):
        ui = _ui_with_audio_tray()
        mf.ZeusUI._on_tray_pick_input(ui, None)   # пункт «Авто (системный)»
        assert ui.controller.input_calls == [None]
        assert ui._tray.audio_updates[-1][2] is None

    def test_pick_without_controller_is_safe(self):
        ui = make_ui()
        ui._tray = FakeAudioTray()
        ui.controller = None
        mf.ZeusUI._on_tray_pick_input(ui, "m1")
        mf.ZeusUI._on_tray_pick_output(ui, "s1")
        assert ui._tray.audio_updates == []   # падений нет, трей не тронут

    def test_start_tray_wires_callbacks_and_fills_devices(self, monkeypatch):
        """Регрессия: _start_tray читал self._controller (AttributeError),
        из-за чего иконка создавалась, но ссылка терялась, а подменю
        оставалось пустым."""
        ui = make_ui()
        ui.controller = FakeAudioController()
        kwargs: dict = {}

        def fake_tr_manager(**kw):
            kwargs.update(kw)
            return FakeAudioTray()

        monkeypatch.setattr(boot, "TrayManager", fake_tr_manager)
        monkeypatch.setattr(
            boot, "config", types.SimpleNamespace(resource_path=lambda p: p)
        )

        mf.ZeusUI._start_tray(ui)

        assert ui._tray is not None and ui._tray.started is True
        # колбэки выбора устройств переданы в меню
        assert callable(kwargs.get("on_pick_input"))
        assert callable(kwargs.get("on_pick_output"))
        # hot-plug: контроллер уведомляет трей об изменениях
        assert callable(ui.controller.on_tray_audio_update)
        # при старте подменю заполнено и активный микрофон отмечен
        assert ui._tray.audio_updates[-1][2] == "m1"

        ui.controller.on_tray_audio_update()   # hot-plug из потока watcher
        assert len(ui._tray.audio_updates) == 2

    def test_refresh_tray_survives_controller_errors(self):
        ui = _ui_with_audio_tray()

        def boom():
            raise RuntimeError("устройства недоступны")

        ui.controller.get_available_input_devices = boom
        ui._refresh_tray_audio_devices()   # не должно бросить наружу
        assert ui._tray.audio_updates == []
