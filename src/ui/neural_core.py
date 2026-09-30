"""Центральный Neural Core 420-500px (ТЗ §5): голограмма + статусный пилюля.

Состояния: Idle — медленное вращение + слабый glow;
Слушает — усиление свечения + волны; Думает — ускорение колец;
Говорит — пульс в такт речи (через update_assistant_state).
"""
from __future__ import annotations

import math
import os

import flet as ft

try:
    from ui.design_system import NEURAL_CORE_MAX, NEURAL_CORE_MIN
except ImportError:  # запуск из src/
    from design_system import NEURAL_CORE_MAX, NEURAL_CORE_MIN  # type: ignore[no-redef]


def build_neural_core(
    holo_container: ft.Container,
    status_text_value: str,
    sub_text_value: str,
    palette: dict[str, str],
    size: int = 440,
    globe: bool = False,
) -> ft.Column:
    """Центр главной: hologram.webp + стеклянный статус-пилюля.

    globe=True — оформить как светящуюся сферу с орбитальными кольцами
    и спутниками (референс Ref.jpg: яркое ядро по центру кадра).
    """
    size = max(NEURAL_CORE_MIN, min(NEURAL_CORE_MAX, int(size)))
    accent = palette.get("accent", "#2563EB")
    text = palette.get("text", "#E2E8F0")
    dim = palette.get("text_dim", "#64748B")

    inner: ft.Control
    if globe:
        inner = _build_globe(holo_container, palette, size)
    else:
        inner = ft.Container(content=holo_container, alignment=ft.Alignment.CENTER)

    pill = ft.Container(
        content=ft.Row(
            [
                ft.Container(
                    content=ft.Icon(ft.Icons.GRAPHIC_EQ_ROUNDED, size=20, color=accent),
                    width=38, height=38, border_radius=19,
                    bgcolor=ft.Colors.with_opacity(0.12, accent),
                    alignment=ft.Alignment.CENTER,
                ),
                ft.Column(
                    [
                        ft.Text(status_text_value, size=13.5, weight=ft.FontWeight.W_600, color=text),
                        ft.Text(sub_text_value, size=11, color=dim),
                    ],
                    spacing=1,
                ),
            ],
            spacing=12,
        ),
        padding=ft.Padding.symmetric(horizontal=18, vertical=12),
        bgcolor=ft.Colors.with_opacity(0.55, palette.get("surface", "#0F172A")),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.5, palette.get("card_border", "#1E293B"))),
        border_radius=16,
        shadow=ft.BoxShadow(blur_radius=24, color=ft.Colors.with_opacity(0.25, accent)),
    )
    column = ft.Column(
        [
            inner,
            ft.Container(content=pill, alignment=ft.Alignment.CENTER, margin=ft.Margin.only(top=-18)),
        ],
        spacing=0,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )
    # Прямые ссылки на тексты статус-пилюли: UI дергает их по имени,
    # а не по хрупким динамическим путям (Container.content...).
    column.status_text = pill.content.controls[1].controls[0]  # type: ignore[attr-defined]
    column.sub_text = pill.content.controls[1].controls[1]  # type: ignore[attr-defined]
    return column


def _build_globe(holo_container: ft.Container, palette: dict[str, str], size: int) -> ft.Stack:
    """Сфера Neural Core: радиальная аура + кольца-орбиты + спутники.

    Слои (снизу вверх):
      aura     — мягкое синее радиальное свечение (ядро референса #187ECD);
      ring_eye — тонкое кольцо вплотную к голограмме;
      orbit_a/b— два капсульных кольца, повёрнутых в разные стороны (эллипсы-орбиты);
      dots     — два светящихся спутника на внешней орбите;
      holo     — сама голограмма (анимированный hologram.webp).
    """
    globe_c = palette.get("globe", "#187ECD")
    ring_c = palette.get("globe_ring", "#4DD2FF")

    def _centered_offsets(w: int, h: int) -> tuple[int, int]:
        return (size - w) // 2, (size - h) // 2

    layers: list[ft.Control] = []

    # 1) Аура: мягкий синий радиальный градиент + тонкое внешнее кольцо
    aura_d = int(size * 1.06)
    lx, ly = _centered_offsets(aura_d, aura_d)
    layers.append(
        ft.Container(
            width=aura_d, height=aura_d,
            left=lx, top=ly,
            border_radius=aura_d // 2,
            border=ft.Border.all(2, ft.Colors.with_opacity(0.22, ring_c)),
            gradient=ft.RadialGradient(
                center=ft.Alignment(0, 0),
                radius=0.78,
                colors=[
                    ft.Colors.with_opacity(0.38, globe_c),
                    ft.Colors.with_opacity(0.12, globe_c),
                    ft.Colors.with_opacity(0.0, globe_c),
                ],
            ),
        )
    )

    # 2) Кольцо вплотную к голограмме (чуть больше самой голограммы)
    eye_d = int(size * 1.02)
    lx, ly = _centered_offsets(eye_d, eye_d)
    layers.append(
        ft.Container(
            width=eye_d, height=eye_d,
            left=lx, top=ly,
            border_radius=eye_d // 2,
            border=ft.Border.all(1, ft.Colors.with_opacity(0.5, ring_c)),
        )
    )

    # 3) Две орбиты-капсулы (эллипсы), повёрнутые в разные стороны
    orbit_w, orbit_h = int(size * 1.04), int(size * 0.32)
    lx, ly = _centered_offsets(orbit_w, orbit_h)
    for rot in (0.42, -0.42):
        layers.append(
            ft.Container(
                width=orbit_w, height=orbit_h,
                left=lx, top=ly,
                border_radius=orbit_h // 2,
                border=ft.Border.all(2, ft.Colors.with_opacity(0.42, ring_c)),
                rotate=rot,
            )
        )

    # 4) Спутники на внешнем круге (два светящихся огонька)
    for ang in (-0.9, 2.2):
        dx = math.cos(ang) * size * 0.46
        dy = math.sin(ang) * size * 0.46
        dot_d = 9
        layers.append(
            ft.Container(
                width=dot_d, height=dot_d,
                left=int(size / 2 + dx - dot_d / 2),
                top=int(size / 2 + dy - dot_d / 2),
                border_radius=dot_d // 2,
                bgcolor=globe_c,
                shadow=ft.BoxShadow(blur_radius=10, spread_radius=1, color=globe_c),
            )
        )

    # 5) Сама голограмма (центрируется Stack-ом)
    holo_wrap = ft.Container(
        content=holo_container,
        width=size, height=size,
        alignment=ft.Alignment.CENTER,
    )
    layers.append(holo_wrap)

    return ft.Stack(
        layers,
        width=size,
        height=size,
        clip_behavior=ft.ClipBehavior.NONE,
    )


def neural_core_image_path(is_light: bool, resource_path) -> str:
    """Путь hologram.webp / hologram_light.webp (для светлой темы — контраст)."""
    file_name = "hologram_light.webp" if is_light else "hologram.webp"
    return resource_path(os.path.join("assets", "animations", file_name))
