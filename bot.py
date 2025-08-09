import asyncio
import logging
import signal
import sys
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
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
ADMIN_IDS_RAW = os.getenv('ADMIN_USER_IDS', '')  # запятая: 12345,67890
ADMIN_USERNAMES_RAW = os.getenv('ADMIN_USERNAMES', '')  # запятая: user1,user2 (без @)
if not BOT_TOKEN:
    raise RuntimeError('TELEGRAM_BOT_TOKEN is not set in .env')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
openai_client = OpenAIClient()

# Простейшее хранилище сессий в памяти
SESSIONS: dict[int, dict] = {}
MAX_IMAGES = int(os.getenv('MAX_IMAGES', '3'))

# Разбор админов из .env
def _parse_admin_ids(raw: str) -> set[int]:
    ids = set()
    for part in (raw or '').replace(' ', '').split(','):
        if not part:
            continue
        try:
            ids.add(int(part))
        except Exception:
            pass
    return ids

def _parse_admin_usernames(raw: str) -> set[str]:
    names = set()
    for part in (raw or '').split(','):
        uname = part.strip().lstrip('@').lower()
        if uname:
            names.add(uname)
    return names

ADMIN_IDS = _parse_admin_ids(ADMIN_IDS_RAW)
ADMIN_USERNAMES = _parse_admin_usernames(ADMIN_USERNAMES_RAW)

def is_admin_user(user: types.User) -> bool:
    # Если список админов пуст — разрешаем всем (для удобства разработки)
    if not ADMIN_IDS and not ADMIN_USERNAMES:
        return True
    if user.id in ADMIN_IDS:
        return True
    uname = (user.username or '').lower()
    return uname in ADMIN_USERNAMES

async def guard_message(message: types.Message) -> bool:
    if not is_admin_user(message.from_user):
        await message.answer('⛔ Доступ запрещен')
        return False
    return True

async def guard_callback(callback: types.CallbackQuery) -> bool:
    if not is_admin_user(callback.from_user):
        await callback.answer('⛔ Доступ запрещен', show_alert=True)
        return False
    return True


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
    if not await guard_message(message):
        return
    await message.answer("🏁 Бот запущен. Напишите произвольный текст — отвечу эхо-сообщением. /help для справки")


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if not await guard_message(message):
        return
    await message.answer("Отправьте любой текст — я повторю его. Это Шаг 1 из 5.")


# Обрабатываем обычный текст только вне состояний (state=None), чтобы не перехватывать редактирование
@dp.message(StateFilter(None), F.text)
async def generate_post(message: types.Message, state: FSMContext):
    if not await guard_message(message):
        return
    await handle_text_to_post(message, state, message.text)


def _detect_verbosity(text: str) -> str:
    # Эвристика: чем длиннее исходный текст, тем длиннее пост
    length = len(text or "")
    if length < 160:
        return "short"
    if length < 600:
        return "medium"
    return "long"


async def handle_text_to_post(message: types.Message, state: FSMContext, input_text: str):
    if not input_text:
        await message.answer("Отправьте текст.")
        return
    await message.answer("🤖 Генерирую пост…")
    try:
        # Не добавляем явные даты во вход — пусть модель не вставляет таймштампы
        verbosity = _detect_verbosity(input_text)
        post = openai_client.generate_post_from_text(input_text, verbosity=verbosity)
        if not post:
            await message.answer("❌ Не удалось сгенерировать пост.")
            return
        SESSIONS[message.from_user.id] = {
            'original_text': input_text,
            'post_text': post,
            'media': [],
        }
        await message.answer(post, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка генерации: {e}")


# Фото/видео вне режима добавления медиа подсказывают нажать кнопку. Голос/аудио обрабатываются отдельным хендлером ниже.
@dp.message(F.photo | F.video)
async def handle_media_anytime(message: types.Message, state: FSMContext):
    if not await guard_message(message):
        return
    # Если сейчас ждём медиа — передаём в обработчик загрузки
    current_state = await state.get_state()
    if current_state == PostStates.waiting_for_media.state:
        await handle_media_upload(message, state)
        return
    # Иначе просим нажать кнопку
    await message.answer("Чтобы добавить медиа к посту, нажмите кнопку «Прикрепить медиа».")


@dp.message(F.voice | F.audio)
async def handle_voice_to_text(message: types.Message, state: FSMContext):
    if not await guard_message(message):
        return
    # Если активен режим ожидания медиа — обрабатываем как медиа
    current_state = await state.get_state()
    if current_state == PostStates.waiting_for_media.state:
        await handle_media_upload(message, state)
        return

    await message.answer("🎙️ Распознаю голос…")
    try:
        if message.voice:
            file_id = message.voice.file_id
            file_info = await bot.get_file(file_id)
            file = await bot.download_file(file_info.file_path)
            audio_bytes = file.read()
            text = openai_client.transcribe(audio_bytes, filename="voice.ogg", language="ru")
        else:
            file_id = message.audio.file_id
            file_info = await bot.get_file(file_id)
            file = await bot.download_file(file_info.file_path)
            audio_bytes = file.read()
            # используем имя для подсказки формата
            text = openai_client.transcribe(audio_bytes, filename=(message.audio.file_name or "audio.mp3"), language="ru")

        if not text:
            await message.answer("❌ Не удалось распознать голос. Попробуйте ещё раз.")
            return

        # Пускаем распознанный текст в обычный генератор, используя оригинальное message
        await handle_text_to_post(message, state, text)
    except Exception as e:
        await message.answer(f"❌ Ошибка распознавания: {e}")


@dp.callback_query(lambda c: c.data == 'regenerate')
async def handle_regenerate(callback: types.CallbackQuery):
    if not await guard_callback(callback):
        return
    await callback.message.edit_text("🎨 Выберите стиль:", reply_markup=get_style_keyboard())


@dp.callback_query(lambda c: c.data.startswith('style_'))
async def handle_style(callback: types.CallbackQuery):
    if not await guard_callback(callback):
        return
    user_id = callback.from_user.id
    sess = SESSIONS.get(user_id)
    if not sess:
        await callback.answer("Сессия не найдена. Отправьте текст заново.", show_alert=True)
        return
    style = callback.data.split('_', 1)[1]
    await callback.message.edit_text("🤖 Генерирую пост в выбранном стиле…")
    text_source = sess.get('original_text') or sess.get('post_text', '')
    # Сохраняем длину от исходного запроса, если есть; иначе — от текущего поста
    seed_text = sess.get('original_text') or text_source
    verbosity = _detect_verbosity(seed_text)
    new_post = openai_client.generate_post_in_style(text_source, style, verbosity=verbosity)
    sess['post_text'] = new_post
    await callback.message.edit_text(new_post, reply_markup=get_main_keyboard())


@dp.callback_query(lambda c: c.data == 'edit')
async def handle_edit(callback: types.CallbackQuery, state: FSMContext):
    if not await guard_callback(callback):
        return
    await state.set_state(PostStates.waiting_for_edit)
    user_id = callback.from_user.id
    sess = SESSIONS.get(user_id)
    current_text = (sess or {}).get('post_text', 'Текст отсутствует. Отправьте сообщение заново.')
    # Показываем текущий текст отдельным сообщением, чтобы было удобно редактировать
    await callback.message.answer("Текущий текст поста:")
    await callback.message.answer(current_text)
    await callback.message.answer("✏️ Отправьте отредактированный текст поста:")


@dp.message(PostStates.waiting_for_edit)
async def handle_edit_text(message: types.Message, state: FSMContext):
    if not await guard_message(message):
        return
    user_id = message.from_user.id
    sess = SESSIONS.get(user_id)
    if not sess:
        await state.clear()
        await message.answer("Сессия не найдена. Отправьте текст заново.")
        return
    # При ручном редактировании сохраняем текст как есть, без повторной генерации
    sess['post_text'] = message.text
    await state.clear()
    await message.answer("✅ Текст обновлён.")
    await message.answer(sess['post_text'], reply_markup=get_main_keyboard())


@dp.callback_query(lambda c: c.data == 'add_media')
async def handle_add_media(callback: types.CallbackQuery, state: FSMContext):
    if not await guard_callback(callback):
        return
    await state.set_state(PostStates.waiting_for_media)
    await callback.message.edit_text(
        "📎 Отправьте фото/видео/аудио (до 3 файлов). Можно отправить несколько сообщений. Нажмите «Готово», когда закончите.",
        reply_markup=get_media_keyboard(),
    )


@dp.message(PostStates.waiting_for_media)
async def handle_media_upload(message: types.Message, state: FSMContext):
    if not await guard_message(message):
        return
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
    if not await guard_callback(callback):
        return
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
    if not await guard_callback(callback):
        return
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
    if not await guard_callback(callback):
        return
    user_id = callback.from_user.id
    sess = SESSIONS.get(user_id)
    text = (sess or {}).get('post_text', 'Текст отсутствует. Отправьте сообщение заново.')
    await callback.message.edit_text(text, reply_markup=get_main_keyboard())


@dp.callback_query(lambda c: c.data == 'cancel')
async def handle_cancel(callback: types.CallbackQuery, state: FSMContext):
    if not await guard_callback(callback):
        return
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

