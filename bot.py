import asyncio
import logging
import signal
import sys
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv
from openai_client import OpenAIClient
from time_parser import parse_event_datetime, format_dt_ru

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHANNEL_ID_RAW = os.getenv('TELEGRAM_CHANNEL_ID')  # @channel_username или числовой id
if not BOT_TOKEN:
    raise RuntimeError('TELEGRAM_BOT_TOKEN is not set in .env')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
openai_client = OpenAIClient()

# Простейшее хранилище сессий в памяти
SESSIONS: dict[int, dict] = {}
MAX_IMAGES = int(os.getenv('MAX_IMAGES', '3'))


class PostStates(StatesGroup):
    waiting_for_edit = State()
    waiting_for_media = State()


def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Перегенерировать", callback_data="regenerate")],
        [InlineKeyboardButton(text="📝 Редактировать", callback_data="edit")],
        [InlineKeyboardButton(text="📎 Прикрепить медиа", callback_data="add_media")],
        [InlineKeyboardButton(text="📤 Опубликовать", callback_data="publish")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
    ])


def get_style_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏁 Классический", callback_data="style_classic")],
        [InlineKeyboardButton(text="😄 Шуточный", callback_data="style_funny")],
        [InlineKeyboardButton(text="📊 Репортаж", callback_data="style_report")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_main")],
    ])


def get_media_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готово", callback_data="media_done")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
    ])


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("🏁 Бот запущен. Напишите произвольный текст — отвечу эхо-сообщением. /help для справки")


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer("Отправьте любой текст — я повторю его. Это Шаг 1 из 5.")


@dp.message(F.text)
async def generate_post(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Отправьте текст.")
        return
    await message.answer("🤖 Генерирую пост…")
    try:
        # нормализуем дату/время и добавим в подсказку
        text_for_llm = message.text
        dt = parse_event_datetime(message.text)
        if dt:
            text_for_llm += f"\n\n[Дата/время]: {format_dt_ru(dt)}"
        post = openai_client.generate_post_from_text(text_for_llm)
        if not post:
            await message.answer("❌ Не удалось сгенерировать пост.")
            return
        # сохраняем в сессию
        SESSIONS[message.from_user.id] = {
            'original_text': message.text,
            'post_text': post,
            'media': [],  # список строк вида photo:<file_id>/video:<file_id>/audio:/voice:
        }
        await message.answer(post, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка генерации: {e}")


@dp.message(F.photo | F.video | F.audio | F.voice)
async def handle_media_anytime(message: types.Message, state: FSMContext):
    # Если сейчас ждём медиа — передаём в обработчик загрузки
    current_state = await state.get_state()
    if current_state == PostStates.waiting_for_media.state:
        await handle_media_upload(message, state)
        return
    # Иначе просим нажать кнопку
    await message.answer("Чтобы добавить медиа к посту, нажмите кнопку «Прикрепить медиа».")


@dp.callback_query(lambda c: c.data == 'regenerate')
async def handle_regenerate(callback: types.CallbackQuery):
    await callback.message.edit_text("🎨 Выберите стиль:", reply_markup=get_style_keyboard())


@dp.callback_query(lambda c: c.data.startswith('style_'))
async def handle_style(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    sess = SESSIONS.get(user_id)
    if not sess:
        await callback.answer("Сессия не найдена. Отправьте текст заново.", show_alert=True)
        return
    style = callback.data.split('_', 1)[1]
    await callback.message.edit_text("🤖 Генерирую пост в выбранном стиле…")
    text_source = sess.get('original_text') or sess.get('post_text', '')
    dt = parse_event_datetime(text_source)
    if dt:
        text_source += f"\n\n[Дата/время]: {format_dt_ru(dt)}"
    new_post = openai_client.generate_post_in_style(text_source, style)
    sess['post_text'] = new_post
    await callback.message.edit_text(new_post, reply_markup=get_main_keyboard())


@dp.callback_query(lambda c: c.data == 'edit')
async def handle_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostStates.waiting_for_edit)
    await callback.message.edit_text("✏️ Отправьте отредактированный текст поста:")


@dp.message(PostStates.waiting_for_edit)
async def handle_edit_text(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    sess = SESSIONS.get(user_id)
    if not sess:
        await state.clear()
        await message.answer("Сессия не найдена. Отправьте текст заново.")
        return
    sess['post_text'] = message.text
    await state.clear()
    await message.answer("✅ Текст обновлён.", reply_markup=get_main_keyboard())
    await message.answer(sess['post_text'])


@dp.callback_query(lambda c: c.data == 'add_media')
async def handle_add_media(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostStates.waiting_for_media)
    await callback.message.edit_text(
        "📎 Отправьте фото/видео/аудио (до 3 файлов). Можно отправить несколько сообщений. Нажмите «Готово», когда закончите.",
        reply_markup=get_media_keyboard(),
    )


@dp.message(PostStates.waiting_for_media)
async def handle_media_upload(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    sess = SESSIONS.get(user_id)
    if not sess:
        await state.clear()
        await message.answer("Сессия не найдена. Отправьте текст заново.")
        return

    media = sess.get('media', [])
    if message.photo:
        if len(media) >= MAX_IMAGES:
            await message.answer(f"⚠️ Достигнут лимит медиа: {MAX_IMAGES}")
        else:
            media.append(f"photo:{message.photo[-1].file_id}")
    elif message.video:
        if len(media) >= MAX_IMAGES:
            await message.answer(f"⚠️ Достигнут лимит медиа: {MAX_IMAGES}")
        else:
            media.append(f"video:{message.video.file_id}")
    elif message.audio:
        if len(media) >= MAX_IMAGES:
            await message.answer(f"⚠️ Достигнут лимит медиа: {MAX_IMAGES}")
        else:
            media.append(f"audio:{message.audio.file_id}")
    elif message.voice:
        if len(media) >= MAX_IMAGES:
            await message.answer(f"⚠️ Достигнут лимит медиа: {MAX_IMAGES}")
        else:
            media.append(f"voice:{message.voice.file_id}")
    else:
        await message.answer("Отправьте фото/видео/аудио.")
        return

    sess['media'] = media[:MAX_IMAGES]
    await message.answer(
        f"✅ Добавлено: {len(sess['media'])}/{MAX_IMAGES}. Можете отправить ещё или нажать «Готово».",
        reply_markup=get_media_keyboard(),
    )


@dp.callback_query(lambda c: c.data == 'media_done')
async def handle_media_done(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    sess = SESSIONS.get(user_id)
    text = (sess or {}).get('post_text', 'Текст отсутствует. Отправьте сообщение заново.')
    await callback.message.edit_text(
        f"{text}\n\nПрикреплено медиа: {len((sess or {}).get('media', []))}/{MAX_IMAGES}",
        reply_markup=get_main_keyboard(),
    )


@dp.callback_query(lambda c: c.data == 'publish')
async def handle_publish(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    sess = SESSIONS.get(user_id)
    if not sess or not sess.get('post_text'):
        await callback.answer("Пост не найден. Сгенерируйте заново.", show_alert=True)
        return
    if not CHANNEL_ID_RAW:
        await callback.answer("Не настроен TELEGRAM_CHANNEL_ID в .env", show_alert=True)
        return

    post_text = sess['post_text']
    media = list(sess.get('media', []))

    # Определяем chat_id
    chat_id: int | str
    try:
        chat_id = int(CHANNEL_ID_RAW)
    except Exception:
        chat_id = CHANNEL_ID_RAW  # @username

    try:
        # Публикация: одиночное медиа → send_*; несколько фото/видео → альбом; аудио/voice отдельно
        if not media:
            await bot.send_message(chat_id=chat_id, text=post_text)
        else:
            photos_videos = [m for m in media if m.startswith(('photo:', 'video:'))]
            audios = [m for m in media if m.startswith('audio:')]
            voices = [m for m in media if m.startswith('voice:')]

            if len(photos_videos) <= 1 and not audios and not voices:
                m = photos_videos[0]
                if m.startswith('photo:'):
                    await bot.send_photo(chat_id=chat_id, photo=m.split(':',1)[1], caption=post_text)
                elif m.startswith('video:'):
                    await bot.send_video(chat_id=chat_id, video=m.split(':',1)[1], caption=post_text)
            else:
                media_group: list[types.InputMedia] = []
                for i, m in enumerate(photos_videos):
                    if m.startswith('photo:'):
                        mid = m.split(':',1)[1]
                        media_group.append(types.InputMediaPhoto(media=mid, caption=post_text if i==0 else None))
                    elif m.startswith('video:'):
                        mid = m.split(':',1)[1]
                        media_group.append(types.InputMediaVideo(media=mid, caption=post_text if i==0 else None))
                if media_group:
                    await bot.send_media_group(chat_id=chat_id, media=media_group)
                # Аудио/войс отправляем отдельно (без альбома)
                for a in audios:
                    await bot.send_audio(chat_id=chat_id, audio=a.split(':',1)[1])
                for v in voices:
                    await bot.send_voice(chat_id=chat_id, voice=v.split(':',1)[1])

        await callback.message.edit_text("✅ Опубликовано!", reply_markup=None)
        # Очищаем сессию
        SESSIONS.pop(user_id, None)
    except Exception as e:
        await callback.answer(f"Ошибка публикации: {e}", show_alert=True)


@dp.callback_query(lambda c: c.data == 'back_main')
async def handle_back(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    sess = SESSIONS.get(user_id)
    text = (sess or {}).get('post_text', 'Текст отсутствует. Отправьте сообщение заново.')
    await callback.message.edit_text(text, reply_markup=get_main_keyboard())


@dp.callback_query(lambda c: c.data == 'cancel')
async def handle_cancel(callback: types.CallbackQuery, state: FSMContext):
    SESSIONS.pop(callback.from_user.id, None)
    await state.clear()
    await callback.message.edit_text("❌ Операция отменена. Отправьте текст заново.")


async def main():
    def signal_handler(signum, frame):
        logger.info("🛑 Завершение...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())

