# -*- coding: utf-8 -*-
"""Автоматический перевод поисковых запросов на китайский для Taobao.

Внутренний поиск Taobao по кириллице выдаёт пустую или нерелевантную
выдачу, поэтому запрос перед формированием URL переводится на zh-CN.

Приоритет перевода:
  1) Офлайн-словарь частых товаров (мгновенно, без сети).
  2) deep_translator.GoogleTranslator (если пакет установлен).
  3) Оригинальный запрос (graceful degradation).
"""
import logging

log = logging.getLogger(__name__)

# --- Офлайн-словарь частых товаров (кириллица -> zh-CN) -------------------
_LOCAL_DICT = {
    "пуховик": "羽绒服",
    "куртка": "夹克",
    "кроссовки": "运动鞋",
    "обувь": "鞋子",
    "футболка": "T恤",
    "рубашка": "衬衫",
    "джинсы": "牛仔裤",
    "штаны": "裤子",
    "шапка": "帽子",
    "перчатки": "手套",
    "шарф": "围巾",
    "свитер": "毛衣",
    "худи": "连帽衫",
    "платье": "连衣裙",
    "юбка": "裙子",
    "нос": "袜子",
    "носки": "袜子",
    "ремень": "腰带",
    "рюкзак": "双肩包",
    "сумка": "包",
    "чемодан": "行李箱",
    "кошелек": "钱包",
    "часы": "手表",
    "очки": "眼镜",
    "зонт": "雨伞",
    "телефон": "手机",
    "наушники": "耳机",
    "зарядка": "充电器",
    "кабель": "数据线",
    "повербанк": "充电宝",
    "клавиатура": "键盘",
    "мышь": "鼠标",
    "монитор": "显示器",
    "флешка": "U盘",
    "игра": "游戏",
    "джойстик": "游戏手柄",
    "геймпад": "游戏手柄",
    "вебкамера": "摄像头",
    "лампа": "台灯",
    "коврик": "地垫",
    "подушка": "枕头",
    "одеяло": "被子",
    "полотенце": "毛巾",
    "зубная щетка": "牙刷",
    "термос": "保温杯",
    "кружка": "马克杯",
    "бутылка": "水瓶",
    "зонт": "雨伞",
    "игрушка": "玩具",
    "книга": "书",
    "ручка": "笔",
    "карандаш": "铅笔",
    "тетрадь": "笔记本",
    "наклейки": "贴纸",
}

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None  # graceful degradation

# Кэш переводчика: результат онлайн-перевода кешируется по запросу, чтобы
# частые поиски одного и того же товара не обращались в сеть повторно.
_TRANSLATE_CACHE: dict[str, str] = {}
_TRANSLATE_CACHE_LOCK = None
try:
    import threading as _th
    _TRANSLATE_CACHE_LOCK = _th.Lock()
except Exception:  # noqa: BLE001
    pass


def clear_translation_cache() -> None:
    """Очищает кэш переводов (полезно в отладке/тестах)."""
    _TRANSLATE_CACHE.clear()


def translate_to_chinese(query: str) -> str:
    """Переводит запрос на zh-CN: кэш -> словарь -> GoogleTranslator -> оригинал."""
    if not query:
        return ""

    cleaned = query.strip().lower()

    # 0) Локальный кэш (мгновенно, без сети) — ТЗ «кеширование переводчика»
    hit = _TRANSLATE_CACHE.get(cleaned)
    if hit is not None:
        return hit

    # 1) Офлайн-словарь (точное совпадение) — мгновенно
    if cleaned in _LOCAL_DICT:
        result = _LOCAL_DICT[cleaned]
        _TRANSLATE_CACHE[cleaned] = result
        return result

    # Частичное совпадение: «тёплый пуховик» -> «тёплый 羽绒服»
    parts = []
    matched = False
    for word in cleaned.split():
        if word in _LOCAL_DICT:
            parts.append(_LOCAL_DICT[word])
            matched = True
        else:
            parts.append(word)
    if matched:
        result = " ".join(parts)
        _TRANSLATE_CACHE[cleaned] = result
        return result

    # 2) Онлайн-перевод через deep_translator
    if GoogleTranslator is not None:
        try:
            result = GoogleTranslator(source="auto", target="zh-CN").translate(
                query.strip()
            )
            if result:
                _TRANSLATE_CACHE[cleaned] = result
                return result
        except Exception as exc:  # noqa: BLE001 — сеть может быть недоступна
            log.warning("[Taobao] Ошибка онлайн-перевода: %s", exc)

    # 3) Fallback — оригинал
    _TRANSLATE_CACHE[cleaned] = query.strip()
    return query.strip()


def get_last_translation() -> str | None:
    """Возвращает последний перевод (для отладки)."""
    return getattr(translate_to_chinese, "_last", None)
