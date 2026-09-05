"""Тесты интеграции голографического визуализатора в main_flet.py (ТЗ v1.2).

Проверяют, что:
- визуализатор использует hologram.webp (а не .gif);
- метод update_assistant_state переключает scale при is_speaking;
- мёртвый код старого визуализатора удалён.
"""
import inspect

from src.ui.main_flet import ZeusUI  # noqa: E402


def test_hologram_uses_webp_source():
    """Источник визуализатора должен быть hologram.webp, а не .gif."""
    src = inspect.getsource(ZeusUI)
    assert "hologram.webp" in src, "hologram.webp должен использоваться"
    assert "hologram.gif" not in src, "hologram.gif не должен оставаться"


def test_update_assistant_state_method_exists():
    """Метод переключения состояния речи должен существовать."""
    assert hasattr(ZeusUI, "update_assistant_state"), \
        "ZeusUI.update_assistant_state не найден"


def test_update_assistant_state_scales_container():
    """update_assistant_state должен менять scale контейнера визуализатора."""
    src = inspect.getsource(ZeusUI.update_assistant_state)
    assert "holo_container" in src, "должен использоваться self.holo_container"
    assert "scale" in src, "должен устанавливаться scale"
    assert "is_speaking" in src


def test_hologram_light_for_light_theme():
    """В светлой теме должен использоваться hologram_light.webp для контраста."""
    src = inspect.getsource(ZeusUI._build_holo_visualizer)
    assert "hologram_light.webp" in src, "hologram_light.webp должен использоваться для светлой темы"
    assert "hologram.webp" in src, "hologram.webp должен использоваться для тёмной темы"
    assert "ThemeMode.LIGHT" in src, "Должна проверяться светлая тема"


def test_old_visualizer_code_removed():
    """Мёртвый код старого визуализатора (_update_visualizer, _on_amplitude) удалён."""
    src = inspect.getsource(ZeusUI)
    assert "_update_visualizer" not in src, "_update_visualizer не удалён"
    assert "_start_visualizer" not in src, "_start_visualizer не удалён"
    assert "_apply_viz_frame" not in src, "_apply_viz_frame не удалён"
    assert "_viz_targets" not in src, "_viz_targets не удалён"
    assert "_viz_listening" not in src, "_viz_listening не удалён"
