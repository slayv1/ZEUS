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
import re
import threading

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
# ВАЖНО: кешируются только успешные переводы. Ответы, похожие на ошибку
# ("Error 500 ..."), и сам текст ошибки НЕ кешируются — иначе отравленный
# кэш заставит Taobao искать текст ошибки вместо товара (выдача по слову
# "again" из фразы "try again later").
_TRANSLATE_CACHE: dict[str, str] = {}
_TRANSLATE_CACHE_LOCK = threading.Lock()
_TRANSLATE_CACHE_MAX_SIZE = 1000

# Эвристика «ответ похож на ошибку, а не на перевод».
_ERROR_LIKE_RE = re.compile(
    r"error\s*\d{3}|traceback|exception|<!doctype|<html|that['’]s an error",
    re.IGNORECASE,
)


def _cache_get(key: str) -> str | None:
    with _TRANSLATE_CACHE_LOCK:
        return _TRANSLATE_CACHE.get(key)


def _cache_put(key: str, value: str) -> None:
    """Кладёт перевод в кэш с простым ограничением размера (FIFO-вытеснение)."""
    with _TRANSLATE_CACHE_LOCK:
        if key not in _TRANSLATE_CACHE and len(_TRANSLATE_CACHE) >= _TRANSLATE_CACHE_MAX_SIZE:
            _TRANSLATE_CACHE.pop(next(iter(_TRANSLATE_CACHE)), None)
        _TRANSLATE_CACHE[key] = value


def clear_translation_cache() -> None:
    """Очищает кэш переводов (полезно в отладке/тестах)."""
    with _TRANSLATE_CACHE_LOCK:
        _TRANSLATE_CACHE.clear()


def _looks_like_error(text: str) -> bool:
    """Грубая проверка: «перевод» похож на текст ошибки/HTML, а не на товар."""
    if not text:
        return True
    t = text.strip()
    if len(t) > 200:
        return True
    if _ERROR_LIKE_RE.search(t):
        return True
    # Онлайн-переводчик иногда возвращает исходник с префиксом сервиса.
    return False


def translate_to_chinese(query: str) -> str:
    """Переводит запрос на zh-CN: кэш -> словарь -> GoogleTranslator -> оригинал.

    ВАЖНО: в кэш пишутся только успешные переводы. Ответы, похожие на ошибку,
    и fallback-возврат оригинала НЕ кешируются (см. _TRANSLATE_CACHE выше).
    """
    if not query:
        return ""

    cleaned = query.strip().lower().replace("ё", "е")

    # 0) Локальный кэш (мгновенно, без сети) — ТЗ «кеширование переводчика»
    hit = _cache_get(cleaned)
    if hit is not None:
        return hit

    # 1) Офлайн-словарь (точное совпадение) — мгновенно
    if cleaned in _LOCAL_DICT:
        result = _LOCAL_DICT[cleaned]
        _cache_put(cleaned, result)
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
        _cache_put(cleaned, result)
        return result

    # 2) Онлайн-перевод через deep_translator
    if GoogleTranslator is not None:
        try:
            result = GoogleTranslator(source="auto", target="zh-CN").translate(
                query.strip()
            )
            if result and not _looks_like_error(result):
                result = result.strip()
                _cache_put(cleaned, result)
                return result
            log.warning("[Taobao] Подозрительный ответ переводчика, игнорирую: %r", result)
        except Exception as exc:  # noqa: BLE001 — сеть может быть недоступна
            log.warning("[Taobao] Ошибка онлайн-перевода: %s", exc)

    # 3) Fallback — оригинал (НЕ кешируем: повторный вызов попробует онлайн снова)
    return query.strip()


def get_last_translation() -> str | None:
    """Возвращает последний УСПЕШНЫЙ перевод (для отладки)."""
    with _TRANSLATE_CACHE_LOCK:
        if not _TRANSLATE_CACHE:
            return None
        return next(reversed(_TRANSLATE_CACHE.values()))
