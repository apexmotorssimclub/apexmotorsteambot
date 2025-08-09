import re
from datetime import datetime
from typing import Optional, List
from zoneinfo import ZoneInfo
import os
from dateparser.search import search_dates


def parse_event_datetime(text: str, tz_name: Optional[str] = None) -> Optional[datetime]:
    """Ищет в русском тексте относительную/абсолютную дату и время и
    возвращает timezone-aware datetime. Игнорирует длительности (например, "2.4 часа").
    """
    tz = ZoneInfo(tz_name or os.getenv("TIMEZONE", "Europe/Moscow"))
    now = datetime.now(tz)

    settings = {
        'RELATIVE_BASE': now,
        'PREFER_DATES_FROM': 'future',
        'TIMEZONE': tz.key,
        'RETURN_AS_TIMEZONE_AWARE': True,
    }

    def is_duration(snippet: str) -> bool:
        return bool(re.search(r"\b\d+(?:[\.,]\d+)?\s*(?:час(?:а|ов)?|ч)\b", snippet, re.IGNORECASE))

    try:
        results = search_dates(text, languages=['ru'], settings=settings) or []
        candidates: List[datetime] = []
        for snippet, dt in results:
            # фильтруем длительности
            if is_duration(snippet):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            candidates.append(dt)

        if candidates:
            # Берём ближайшую (предпочтительно будущую)
            candidates.sort(key=lambda d: (d < now, abs((d - now).total_seconds())))
            return candidates[0]
    except Exception:
        pass
    return None


def format_dt_ru(dt: datetime) -> str:
    months = [
        'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
        'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'
    ]
    tz_abbr = dt.tzname() or 'МСК'
    return f"{dt.day} {months[dt.month - 1]} {dt.year}, {dt:%H:%M} {tz_abbr}"

