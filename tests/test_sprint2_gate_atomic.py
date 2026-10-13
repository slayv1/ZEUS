# -*- coding: utf-8 -*-
"""Спринт 2 (ТЗ v1.4 §2.2): шумовой гейт, атомарная запись JSON, calc без eval.

Что проверяется:
  1. core/audio_gate.py — уровень сигнала, адаптивный шумовой фон, hangover,
     отключение гейта, счётчики диагностики.
  2. wake_word — гейт отсекает тихие ложные срабатывания, обход по настройке,
     сброс состояния при смене микрофона.
  3. audio_engine — _has_partial (fail-open) и чтение порогов из настроек.
  4. core/atomic_json.py — сбой сериализации НЕ калечит существующий файл,
     *.tmp не остаются хвостов.
  5. actions — commands.json пишется только атомарно, ошибка записи видна.
  6. actions._safe_calc — арифметика работает, инъекции и DoS отклоняются.
  7. requirements.txt — pycaw/comtypes объявлены (иначе Core Audio деградирует).
"""
from __future__ import annotations

import array
import json
import os
import struct
import threading
from unittest.mock import MagicMock, patch

import pytest

from core import actions
from core.atomic_json import read_json_safe, write_json_atomic
from core.audio_gate import NoiseGate, peak_level
from core.config import DEFAULT_SETTINGS
from core.controller import ZeusController
from modules.audio_engine import AudioEngine
from modules.wake_word import WakeWordDetector


def _pcm(value: int, samples: int = 400) -> bytes:
    """Блок PCM int16, где каждый отсчёт равен ``value``."""
    return struct.pack(f"<{samples}h", *([value] * samples))


# ---------------------------------------------------------------------------
# 1. Энергетический гейт
# ---------------------------------------------------------------------------

class TestPeakLevel:
    def test_тишина_даёт_ноль(self):
        assert peak_level(_pcm(0)) == 0
        assert peak_level(b"") == 0

    def test_уровень_равен_среднему_модулю(self):
        assert peak_level(_pcm(1000)) == 1000
        assert peak_level(_pcm(-1000)) == 1000      # знак не важен

    def test_нечётное_число_байт_не_роняет(self):
        assert peak_level(b"\x01\x02\x03") == peak_level(b"\x01\x02")

    def test_принимает_array_и_memoryview(self):
        buf = array.array("h", [500] * 400)
        assert peak_level(buf) == 500
        assert peak_level(memoryview(_pcm(700))) == 700


class TestNoiseGate:
    def test_тишина_отклоняется(self):
        gate = NoiseGate(30)
        results = [gate.allow(_pcm(3), now=0.25 * i) for i in range(1, 10)]
        assert not any(results)
        assert gate.rejected == 9 and gate.passed == 0

    def test_речь_открывает_гейт_и_держит_хвост(self):
        gate = NoiseGate(30)
        assert gate.allow(_pcm(3000), now=1.0) is True
        # 250 мс после речи hangover открыт: окончание фразы не отрежем
        assert gate.allow(_pcm(2), now=1.25) is True
        # спустя 500 мс (> hangover 350 мс) хвост закрыт
        assert gate.allow(_pcm(2), now=1.5) is False

    def test_порог_растёт_над_шумовым_фоном(self):
        gate = NoiseGate(30)
        for i in range(60):
            gate.allow(_pcm(20), now=0.25 * i)
        assert gate.noise_floor == 20
        assert gate.gate_level == 40          # фон × 2 выше абсолютного минимума
        # 35 громче минимума 30, но это ещё шум: не пропускаем
        assert gate.allow_peak(35, now=100.0) is False
        assert gate.allow_peak(60, now=100.25) is True

    def test_один_хлопок_не_вскидывает_порог(self):
        gate = NoiseGate(30)
        for i in range(50):
            gate.allow(_pcm(4), now=0.25 * i)
        before = gate.noise_floor
        gate.allow(_pcm(20000), now=100.0)    # щелчок мышью / хлопок
        # Фон — трекер минимума с ограниченным ростом: всплеск поднимает его
        # максимум на FLOOR_RISE (1), а не до своего уровня. Иначе один хлопок
        # сделал бы гейт глухим ко всей остальной речи.
        assert gate.noise_floor <= before + 1
        assert gate.gate_level < 200, "порог не должен улетать до уровня хлопка"

    def test_выключенный_гейт_пропускает_всё(self):
        gate = NoiseGate(30, enabled=False)
        assert gate.allow(_pcm(0), now=1.0) is True
        assert gate.passed == 1 and gate.rejected == 0

    def test_сброс_обнуляет_состояние(self):
        gate = NoiseGate(30)
        gate.allow(_pcm(500), now=1.0)
        gate.reset()
        assert gate.noise_floor == 0 and gate.passed == 0 and gate.rejected == 0
        assert gate.gate_level == 30

    def test_describe_содержит_диагностику(self):
        gate = NoiseGate(30)
        gate.allow(_pcm(123), now=1.0)
        text = gate.describe()
        assert "peak=" in text and "gate=" in text and "floor=" in text


# ---------------------------------------------------------------------------
# 2. Гейт внутри WakeWordDetector
# ---------------------------------------------------------------------------

def _detector() -> WakeWordDetector:
    det = WakeWordDetector()
    det.on_wake = MagicMock()
    det._last_wake_ts = 0.0
    return det


class TestWakeWordGate:
    def test_тихий_зевс_не_будит(self):
        det = _detector()
        for _ in range(10):
            det._update_recent_peak(2)
        assert det._maybe_trigger("зевс") is True   # слово найдено...
        det.on_wake.assert_not_called()             # ...но отклонено гейтом
        assert det._gate_rejected == 1

    def test_громкий_зевс_будит(self):
        det = _detector()
        for _ in range(10):
            det._update_recent_peak(2)
        det._update_recent_peak(4000)
        assert det._maybe_trigger("зевс") is True
        det.on_wake.assert_called_once()
        assert det._gate_passed >= 1

    def test_обход_гейта_по_настройке(self):
        det = _detector()
        det.set_gate_config(enabled=False)
        for _ in range(10):
            det._update_recent_peak(1)
        det._maybe_trigger("зевс")
        det.on_wake.assert_called_once()
        assert det._gate_rejected == 0

    @pytest.mark.parametrize("value,expected", [
        ("0", False), ("false", False), ("off", False),
        ("1", True), ("true", True), (True, True), (False, False),
    ])
    def test_настройка_из_json_тракуется_одинообразно(self, value, expected):
        det = _detector()
        det.set_gate_config(enabled=value)
        assert det._gate_enabled is expected

    def test_статус_гейта_для_терминала(self):
        text = _detector().gate_status()
        assert "Шумовой гейт" in text and "фон=" in text and "отклонено=" in text

    def test_смена_микрофона_сбрасывает_фон(self):
        det = _detector()
        for _ in range(20):
            det._update_recent_peak(50)
        assert det._noise_floor > 0
        det.set_mic_device(3)
        assert det._noise_floor == 0
        assert det._mic_device_index == 3

    def test_лог_гейта_троттлится(self):
        det = _detector()
        det._last_gate_log_ts = 0.0
        det._log_gate_block("пик низкий")
        first_ts = det._last_gate_log_ts
        det._log_gate_block("пик низкий")
        assert det._last_gate_log_ts == first_ts, "чаще раза в 5 с печатать нельзя"


# ---------------------------------------------------------------------------
# 3. AudioEngine: вспомогательная логика гейта
# ---------------------------------------------------------------------------

class TestEngineGateHelpers:
    @pytest.mark.parametrize("payload,expected", [
        ('{"partial":"зев"}', True),
        ('{"partial":""}', False),
        ('{"partial":"  "}', False),
    ])
    def test_has_partial_по_результату_vosk(self, payload, expected):
        rec = MagicMock()
        rec.PartialResult.return_value = payload
        assert AudioEngine._has_partial(rec) is expected

    def test_has_partial_fail_open(self):
        """Сбой распознавателя = «фраза есть»: речь резать опаснее, чем шум."""
        rec = MagicMock()
        rec.PartialResult.side_effect = RuntimeError("Vosk упал")
        assert AudioEngine._has_partial(rec) is True

    def test_пороги_читаются_из_настроек(self):
        engine = object.__new__(AudioEngine)
        settings = {"voice_settings": {
            "noise_gate_enabled": False, "command_energy_threshold": 55,
        }}
        with patch("core.config.load_settings", return_value=settings):
            gate = engine._make_gate()
        assert gate.threshold == 55
        assert gate.enabled is False

    def test_без_настроек_разумный_дефолт(self):
        engine = object.__new__(AudioEngine)
        with patch("core.config.load_settings", side_effect=RuntimeError("нет файла")):
            gate = engine._make_gate()
        assert gate.threshold == 30 and gate.enabled is True

    def test_настройки_гейта_есть_в_дефолтах(self):
        voice = DEFAULT_SETTINGS["voice_settings"]
        assert voice["noise_gate_enabled"] is True
        assert voice["command_energy_threshold"] == 25

    def test_применение_настроек_без_перезапуска(self):
        """Контроллер доводит гейт до живого детектора мгновенно (ТЗ §2.2)."""
        ctrl = object.__new__(ZeusController)
        ctrl.settings = {"voice_settings": {
            "noise_gate_enabled": False, "wake_energy_threshold": 44,
        }}
        ctrl.audio = MagicMock()
        ctrl.audio.wake.gate_status.return_value = "Шумовой гейт: выкл"
        ctrl._apply_noise_gate_settings()
        ctrl.audio.wake.set_gate_config.assert_called_once_with(
            enabled=False, threshold=44
        )



# ---------------------------------------------------------------------------
# 4. Атомарная запись JSON
# ---------------------------------------------------------------------------

class TestAtomicJson:
    def test_круговой_прогон_и_юникод(self, tmp_path):
        path = str(tmp_path / "commands.json")
        assert write_json_atomic(path, {"приложения": {"хром": "C:\\x.exe"}}) is True
        assert read_json_safe(path) == {"приложения": {"хром": "C:\\x.exe"}}

    def test_tmp_не_остаётся(self, tmp_path):
        write_json_atomic(str(tmp_path / "a.json"), {"a": 1})
        assert sorted(os.listdir(tmp_path)) == ["a.json"]

    def test_сбой_сериализации_не_калечит_файл(self, tmp_path):
        """Главный смысл атомарности: битые данные не затирают хорошие."""
        path = str(tmp_path / "commands.json")
        write_json_atomic(path, {"приложения": {"хром": "C:\\x.exe"}})
        # set не сериализуется: json.dump упадёт в середине записи
        assert write_json_atomic(path, {"бит": {1, 2, 3}}) is False
        assert read_json_safe(path) == {"приложения": {"хром": "C:\\x.exe"}}
        assert sorted(os.listdir(tmp_path)) == ["commands.json"]

    def test_создаёт_вложенные_каталоги(self, tmp_path):
        path = str(tmp_path / "data" / "deep" / "x.json")
        assert write_json_atomic(path, [1, 2]) is True
        assert read_json_safe(path) == [1, 2]

    def test_параллельные_писатели_не_портят_файл(self, tmp_path):
        path = str(tmp_path / "cache.json")

        def writer(n: int) -> None:
            for _ in range(20):
                write_json_atomic(path, {"n": n})

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert read_json_safe(path)["n"] in set(range(8))
        json.loads(open(path, encoding="utf-8").read())   # файл всегда полный

    def test_read_json_safe_на_битом_файле(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{обрыв", encoding="utf-8")
        assert read_json_safe(str(bad), {}) == {}
        assert read_json_safe(str(tmp_path / "нет.json"), None) is None



# ---------------------------------------------------------------------------
# 5. commands.json: только атомарная запись
# ---------------------------------------------------------------------------

class TestCommandsAtomicSave:
    def test_save_commands_пишет_и_не_оставляет_tmp(self, tmp_path):
        path = tmp_path / "commands.json"
        with patch.object(actions, "_commands_path", return_value=str(path)):
            assert actions._save_commands({"приложения": {"хром": "C:\\x.exe"}}) is True
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "приложения": {"хром": "C:\\x.exe"}
        }
        assert sorted(p.name for p in tmp_path.iterdir()) == ["commands.json"]

    def test_ошибка_записи_видна_пользователю(self, tmp_path):
        """Не «успех» с молча проглоченной ошибкой, а честный отказ."""
        with patch.object(actions, "_commands_path",
                          return_value=str(tmp_path / "commands.json")), \
             patch.object(actions, "load_commands",
                          return_value={"приложения": {"хром": "C:\\x.exe"}}), \
             patch.object(actions, "write_json_atomic", return_value=False):
            result = actions.delete_command("хром")
        assert result["success"] is False
        assert "диск" in result["message"].lower()

    def test_удаление_доходит_до_файла(self, tmp_path):
        path = tmp_path / "commands.json"
        path.write_text(json.dumps({"приложения": {"хром": "C:\\x.exe",
                                                   "браузер": "C:\\y.exe"}},
                                   ensure_ascii=False), encoding="utf-8")
        with patch.object(actions, "_commands_path", return_value=str(path)):
            result = actions.delete_command("хром")
        assert result["success"] is True
        assert "хром" not in json.loads(path.read_text(encoding="utf-8"))["приложения"]

    def test_в_коде_не_осталось_прямых_записей_commands(self):
        """open(...,'w') + json.dump для commands.json — риск потери данных."""
        source = open(actions.__file__, encoding="utf-8").read()
        assert "json.dump(commands" not in source


# ---------------------------------------------------------------------------
# 6. Калькулятор: арифметика есть, eval — нет
# ---------------------------------------------------------------------------

class TestSafeCalc:
    @pytest.mark.parametrize("expr,expected", [
        ("2+2*2", 6.0),
        ("(10-4)/3", 2.0),
        ("2**10", 1024.0),
        ("-5+3", -2.0),
        ("7//2", 3.0),
        ("7%3", 1.0),
        ("1e3+1", 1001.0),
        ("-(2+3)*2", -10.0),
    ])
    def test_арифметика(self, expr, expected):
        assert actions._safe_calc(expr) == pytest.approx(expected)

    @pytest.mark.parametrize("expr", [
        "1/0",
        "9**999999999",            # DoS через гигантскую степень
        "2**2**2**2**10",          # башня степеней
        "999999999999**2",         # основание слишком большое
        "1e999",                   # inf
        "__import__('os').system('calc')",
        "open('C:/Windows/win.ini').read()",
        "(1).__class__",
        "[1,2][0]",
        "'abc'",
        "True+1",
        "",
        "abc",
        "1+" + "1" * 200,          # слишком длинное
    ])
    def test_опасное_и_мусорное_отклоняется(self, expr):
        assert actions._safe_calc(expr) is None

    def test_eval_больше_не_вызывается(self):
        """Проверяем не текстом (слово eval встречается в комментариях-объяснениях),
        а синтаксическим деревом: вызова eval() в модуле быть не должно."""
        import ast as _ast
        import io as _io
        tree = _ast.parse(_io.open(actions.__file__, encoding="utf-8").read())
        calls = {
            node.func.id for node in _ast.walk(tree)
            if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name)
        }
        assert "eval" not in calls and "exec" not in calls
        source = _io.open(actions.__file__, encoding="utf-8").read()
        assert "import ast" in source, "калькулятор обязан разбирать выражения парсером"


# ---------------------------------------------------------------------------
# 7. Зависимости Core Audio (ТЗ v1.4 §2.2)
# ---------------------------------------------------------------------------

def test_pycaw_обязателен_на_windows():
    """С закомментированным pycaw Зевс тихо деградировал до sounddevice:
    без форм-факторов, транспорта и ContainerId автовыбор гарнитуры хуже."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lines = [ln.strip() for ln in open(os.path.join(root, "requirements.txt"),
                                       encoding="utf-8") if ln.strip()]
    active = [ln for ln in lines if not ln.startswith("#")]
    assert any(ln.lower().startswith("pycaw") for ln in active)
    assert any(ln.lower().startswith("comtypes") for ln in active)



# ---------------------------------------------------------------------------
# 8. Настоящий _listen_loop на подставных vosk / sounddevice
# ---------------------------------------------------------------------------

class TestListenLoopGating:
    """Прогон реального цикла прослушивания: шум не должен доходить до Vosk."""

    @staticmethod
    def _run_loop(blocks, *, gate_enabled=True, threshold=30):
        import sys
        import types

        fed: list[int] = []

        class FakeRecognizer:
            def __init__(self, model, rate):
                pass

            def SetWords(self, flag):
                pass

            def Reset(self):
                pass

            def PartialResult(self):
                return '{"partial": ""}'

            def AcceptWaveform(self, data):
                fed.append(peak_level(data))   # что реально попало в распознаватель
                return False

            def Result(self):
                return '{"text": ""}'

        class FakeStream:
            def __init__(self, *args, **kwargs):
                self._i = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, frames):
                if self._i >= len(blocks) - 1:
                    engine._listen_running = False
                data = blocks[min(self._i, len(blocks) - 1)]
                self._i += 1
                return data, None

        engine = object.__new__(AudioEngine)
        engine._mic_available = True
        engine._mic_device_index = 0
        engine.mic_gain = 1.0
        engine._listen_running = True
        engine._get_vosk_model = lambda: (object(), 16000)
        engine._handle_listen_text = lambda text: None

        fake_sd = types.SimpleNamespace(RawInputStream=FakeStream)
        fake_vosk = types.SimpleNamespace(KaldiRecognizer=FakeRecognizer)
        settings = {"voice_settings": {
            "noise_gate_enabled": gate_enabled, "command_energy_threshold": threshold,
        }}
        with patch.dict(sys.modules, {"sounddevice": fake_sd, "vosk": fake_vosk}), \
             patch("core.config.load_settings", return_value=settings), \
             patch("time.sleep", lambda _s: None):
            engine._listen_loop()
        return fed

    def test_шум_не_попадает_в_распознаватель(self, capsys):
        noise, speech = _pcm(2, 4000), _pcm(3000, 4000)
        # Первые 2 блока цикл вычитывает «на прогрев» и не подаёт в Vosk
        blocks = [noise] * 2 + [noise] * 6 + [speech] + [noise]
        fed = self._run_loop(blocks)
        # 6 блоков тишины до речи отсеяны, дальше — речь и один блок «хвоста»
        # (hangover держит гейт открытым, чтобы не обрезать окончание фразы).
        assert fed[0] == 3000, "речь обязана дойти до распознавателя"
        assert len(fed) == 2, f"шум до речи не должен проскакивать: {fed}"
        # Статус гейта обязан попасть в терминал (ТЗ §4.3:.prefix [AUDIO] — мост UI)
        assert "[AUDIO] [GATE] шумовой гейт: вкл порог=30" in capsys.readouterr().out

    def test_выключенный_гейт_пропускает_всё_как_раньше(self):
        """Обход гейта возвращает поведение v1.3: в распознаватель летит всё."""
        noise = _pcm(2, 4000)
        blocks = [noise] * 2 + [noise] * 5
        fed = self._run_loop(blocks, gate_enabled=False)
        assert len(fed) == 5, "без гейта блоки не отсекаются"
        assert all(level == 2 for level in fed)

