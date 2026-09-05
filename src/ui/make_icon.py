# -*- coding: utf-8 -*-
"""Генерация zeus.ico (ТЗ v1.2 п.3.3): логотип-молния на тёмном фоне.

Multi-res: 256/128/64/48/32/16. Рисуется через PIL — без внешних ассетов.
"""
from PIL import Image, ImageDraw
import os

# Единое хранилище ассетов (ТЗ v1.2: централизация иконок）
ASSETS_ICONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "icons")

SS = 4  # суперсэмплинг для гладких краёв
S = 256 * SS


def rounded_bg(draw, s):
    """Тёмный закруглённый фон (Dark Theme #1E293B) с синим контуром."""
    m = 6 * SS
    r = 56 * SS
    draw.rounded_rectangle(
        [m, m, s - m, s - m], radius=r, fill="#1E293B", outline="#3B82F6", width=6 * SS
    )


def draw_bolt(draw, s, scale=1.0, glow_color=(59, 130, 246, 255)):
    """Стилизованная электрическая молния по центру."""
    cx, cy = s // 2, s // 2
    h = int(150 * SS) * 1  # высота молнии
    w = int(52 * SS)
    top = cy - h // 2
    # Классический зигзаг-болт
    pts = [
        (cx + int(w * 0.10), top),
        (cx - int(w * 0.55), top + int(h * 0.52)),
        (cx - int(w * 0.02), top + int(h * 0.52)),
        (cx - int(w * 0.16), top + h),
        (cx + int(w * 0.42), top + int(h * 0.42)),
        (cx - int(w * 0.06), top + int(h * 0.44)),
    ]
    # Свечение (несколько слоёв синим)
    for blur in (5, 3):
        off = blur_off = blur
        draw.polygon(
            [(x + blur_off, y) for x, y in pts],
            fill=(59, 130, 246, 90),
        )
    draw.polygon(pts, fill=(125, 211, 252, 255))  # светло-голубой градиент-заглушка


def main():
    os.makedirs(ASSETS_ICONS, exist_ok=True)
    sizes = [256, 128, 64, 48, 32, 16]
    imgs = []
    for sz in sizes:
        s = sz * SS
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        rounded_bg(d, s)
        draw_bolt(d, s)
        imgs.append(img.resize((sz, sz), Image.LANCZOS))
    imgs[0].save(
        os.path.join(ASSETS_ICONS, "app_icon.ico"),
        format="ICO",
        sizes=[(sz, sz) for sz in sizes],
        append_images=imgs[1:],
    )
    # Primary App Icon 1024x1024 (macOS/Linux — ТЗ §5.2)
    big = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    rounded_bg(d, 1024)
    draw_bolt(d, 1024)
    big.save(os.path.join(ASSETS_ICONS, "logo_1024.png"), format="PNG")
    print("OK: app_icon.ico + logo_1024.png созданы в assets/icons/")


if __name__ == "__main__":
    main()