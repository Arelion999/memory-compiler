"""Детерминированная агрегация версий: структурная экстракция ключа + max(serial).

Чистый модуль (без I/O, без импортов из storage) — единственный дом сравнения и
агрегации версий. storage.py делегирует сюда исторические хелперы
(_semver_key/_max_semver/_looks_like_date), сохраняя их контракт байт-в-байт.
Research (arXiv 2606.01435): детерминизм на сборке/чтении бьёт LLM/temporal-суждение
на версионных конфликтах.
"""
import re
from typing import Iterable, Optional


def version_key(v) -> tuple:
    """Числовой ключ версии для сравнения. Pre-release (-rc/-beta/-alpha) сортируется
    НИЖЕ одноимённого финального релиза: 1.8.0-rc1 < 1.8.0 < 1.8.1.
      '1.7.16'      → ((1, 7, 16), 1, ())      # релиз: маркер 1 (выше)
      '1.8.0-rc1'   → ((1, 8, 0), 0, (1,))     # pre-release: маркер 0 (ниже) + номер rc
      '8.3.24.1234' → ((8, 3, 24, 1234), 1, ()) # 4-part (1С) сравнивается корректно
    Без маркера '1.8.0-rc1' давал (1,8,0,1) > (1,8,0) — pre-release выигрывал у финала.
    """
    base, _, suffix = str(v).partition("-")
    nums = tuple(int(x) for x in re.findall(r'\d+', base))
    if suffix:
        suf_nums = tuple(int(x) for x in re.findall(r'\d+', suffix))
        return (nums, 0, suf_nums)
    return (nums, 1, ())


def is_date_like(v) -> bool:
    """X.Y.Z, похожее на календарную дату (2024.06.25): год 2000-2099, месяц 1-12,
    день 1-31. Такие строки — даты, не версии (иначе год >> мажор и дата «максимальна»).
    """
    parts = str(v).split(".")
    if len(parts) != 3:
        return False
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return False
    return 2000 <= y <= 2099 and 1 <= m <= 12 and 1 <= d <= 31


def is_version_like(v) -> bool:
    """True, если строка выглядит версией: >= 2 числовых компонента И не дата.
    Фильтр для resolve() — отсекает даты и мусор из агрегации на чтении."""
    if is_date_like(v):
        return False
    base = str(v).partition("-")[0]
    return len(re.findall(r'\d+', base)) >= 2


def max_version(values: Iterable[str]) -> Optional[str]:
    """Максимальная версия по version_key (1.7.16 > 1.7.11 > 1.7.9; финал > pre-release).
    НЕ фильтрует даты — как исторический _max_semver (чистку дат делает экстрактор фактов
    на записи, не здесь). Пустой вход → None (историческая версия падала, но вызывалась
    только при len>1)."""
    values = list(values)
    if not values:
        return None
    try:
        return max(values, key=version_key)
    except Exception:
        return values[0]
