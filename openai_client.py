from typing import List, Optional
import os
from openai import OpenAI
import tempfile
from pathlib import Path


class OpenAIClient:
    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in .env")
        self.client = OpenAI(api_key=api_key)
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        # чуть теплее, чтобы стиль был живее
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.9"))

    def generate_post_from_text(self, text: str, verbosity: Optional[str] = None) -> str:
        """Генерирует пост в стиле менеджера команды. verbosity: short|medium|long."""
        verbosity_rules = {
            "short": "Сделай короткий пост: 1–2 предложения, без буллетов и без хэштегов.",
            "medium": "Сделай компактный пост: 2–4 предложения, без хэштегов.",
            "long": (
                "Сделай развернутый пост в стиле сжатого отчёта: 3–6 коротких строк. "
                "Разбей на строки по смыслу (например, как пункты), можно начать строки с уместных эмодзи, но без хэштегов."
            ),
        }
        length_hint = verbosity_rules.get(verbosity or "", "Подстрой длину поста под объём входного текста.")
        system_prompt = (
            "Ты — менеджер симрейсинг-команды и пишешь живые посты для Telegram. "
            "Пиши простым языком, без канцелярита и без пафоса, 0–2 эмодзи. "
            "Фокус: команда, трасса, формат/симулятор/авто/пилоты/результат — только если есть во входе. Не придумывай факты. "
            "Не добавляй в конце поста таймстампы/даты вида ‘9 августа, 13:37’. "
            f"{length_hint}"
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Создай пост по информации:\n\n{text}"},
            ],
            n=1,
        )
        return (resp.choices[0].message.content or "").strip()

    def generate_post_in_style(self, text: str, style: str, verbosity: Optional[str] = None) -> str:
        """Перегенерирует пост в выбранном стиле: classic|funny|report. verbosity: short|medium|long."""
        styles = {
            "classic": "классический спортивный стиль",
            "funny": "шуточный, но уместный, без сарказма",
            "report": "сдержанный репортажный стиль",
        }
        style_desc = styles.get(style, "классический спортивный стиль")
        verbosity_rules = {
            "short": "1–2 предложения, без буллетов.",
            "medium": "2–4 предложения.",
            "long": "3–6 коротких строк, допускаются строки с эмодзи в начале.",
        }
        length_hint = verbosity_rules.get(verbosity or "", "Подстрой длину под объём входного текста.")
        system_prompt = (
            f"Ты — менеджер симрейсинг-команды. Стиль: {style_desc}. "
            "Пиши по-человечески, без пафоса. "
            f"{length_hint} "
            "Не добавляй в конце поста таймстампы/даты вида ‘9 августа, 13:37’."
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Информация для поста:\n\n{text}"},
            ],
            n=1,
        )
        return (resp.choices[0].message.content or "").strip()

    def transcribe(self, file_bytes: bytes, filename: str = "audio.ogg", language: str = "ru") -> Optional[str]:
        """Транскрибирует аудио в текст (Whisper). Возвращает распознанный текст или None."""
        # Сохраняем во временный файл, так надёжнее для клиента
        suffix = Path(filename).suffix or ".ogg"
        with tempfile.NamedTemporaryFile(delete=True, suffix=suffix) as tmp:
            tmp.write(file_bytes)
            tmp.flush()
            try:
                resp = self.client.audio.transcriptions.create(
                    model=os.getenv("OPENAI_STT_MODEL", "whisper-1"),
                    file=Path(tmp.name),
                    language=language,
                    response_format="text",
                )
                # resp is str when response_format="text"
                text = str(resp).strip()
                return text or None
            except Exception:
                return None

