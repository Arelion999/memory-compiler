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
