from typing import List
import os
from openai import OpenAI


class OpenAIClient:
    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in .env")
        self.client = OpenAI(api_key=api_key)
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        # чуть теплее, чтобы стиль был живее
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.9"))

    def generate_post_from_text(self, text: str) -> str:
        """Генерирует короткий пост в стиле менеджера команды из произвольного текста."""
        system_prompt = (
            "Ты — менеджер симрейсинг-команды и пишешь короткие, живые посты для Telegram. "
            "Пиши простым языком, 2–4 предложения, без канцелярита и пафоса, 0–2 эмодзи. "
            "Фокус: команда, трасса, формат/инфо из сообщения. Не придумывай факты."
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Создай короткий пост по информации:\n\n{text}"},
            ],
            n=1,
        )
        return (resp.choices[0].message.content or "").strip()

    def generate_post_in_style(self, text: str, style: str) -> str:
        """Перегенерирует пост в выбранном стиле: classic|funny|report."""
        styles = {
            "classic": "классический спортивный стиль",
            "funny": "шуточный, но уместный, без сарказма",
            "report": "сдержанный репортажный стиль",
        }
        style_desc = styles.get(style, "классический спортивный стиль")
        system_prompt = (
            f"Ты — менеджер симрейсинг-команды. Стиль: {style_desc}. "
            "Пиши коротко (2–4 предложения), по-человечески, без пафоса."
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

