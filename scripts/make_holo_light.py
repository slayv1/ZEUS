"""Создаёт hologram_light.webp для светлой темы — усиливает контраст линий на белом фоне."""
import os
from PIL import Image, ImageEnhance

INPUT = "assets/animations/hologram.webp"
OUTPUT = "assets/animations/hologram_light.webp"

def process_frame(frame: Image.Image) -> Image.Image:
    """Усиливает контраст для светлого фона: затемняет полупрозрачные линии."""
    # Разделяем каналы
    r, g, b, a = frame.split()
    
    # Для непрозрачных пикселей усиливаем цвет (делаем линии насыщеннее/темнее)
    # Для полупрозрачных (бледных) линий — снижаем прозрачность, чтобы они стали заметнее
    # Инвертируем логику: в светлой теме бледный голубой сливается с белым
    # Решение: затемняем RGB каналы, сохраняя альфу
    
    # Затемняем цветовые каналы (множитель < 1 делает линии темнее)
    r = r.point(lambda x: max(0, int(x * 0.45)))
    g = g.point(lambda x: max(0, int(x * 0.45)))
    b = b.point(lambda x: max(0, int(x * 0.55)))
    
    # Для полупрозрачных пикселей повышаем непрозрачность, чтобы линии были заметнее
    # Порог: если пиксель был хоть немного виден — делаем его более непрозрачным
    a = a.point(lambda x: min(255, int(x * 1.8)) if x > 10 else 0)
    
    return Image.merge("RGBA", (r, g, b, a))


img = Image.open(INPUT)
frames = []
durations = []

try:
    while True:
        frame = img.copy().convert("RGBA")
        processed = process_frame(frame)
        frames.append(processed)
        # Извлекаем длительность кадра
        dur = img.info.get("duration", 50)
        durations.append(dur)
        img.seek(img.tell() + 1)
except EOFError:
    pass

if frames:
    frames[0].save(
        OUTPUT,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        lossless=False,
        quality=80,
        method=4,
    )
    orig_size = os.path.getsize(INPUT)
    new_size = os.path.getsize(OUTPUT)
    print(f"OK: {len(frames)} кадров, {orig_size//1024}KB -> {new_size//1024}KB")
else:
    print("ERROR: кадры не извлечены")
