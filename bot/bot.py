import logging
import base64
import aiohttp
import sqlite3
import uuid
import logging
import re
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
import os

load_dotenv()

# Включение логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._application").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

SERVER_URL = 'http://127.0.0.1:5000/applications'
TOKEN = os.getenv('TOKEN')

def get_db():
    conn = sqlite3.connect('applications.db')
    conn.row_factory = sqlite3.Row
    return conn

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[KeyboardButton("Start")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Приветствую! Для подачи заявки нажмите Start.",
        reply_markup=reply_markup
    )

def delete_application_by_id(app_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM applications WHERE application_id = ?", (app_id,))
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return deleted > 0

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    match = re.match(r'Заявка\s+([a-f0-9]{8})\s+выполнена', text, re.IGNORECASE)
    if match:
        app_id = match.group(1)
        delete_application_by_id(app_id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text == "start":
        context.user_data.clear()
        keyboard = [[KeyboardButton("Done")]]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            "Заполните заявку по форме:\nИмя\nОтделение\nТекст проблемы\n(Фото отправьте отдельными сообщениями, если есть). Когда всё готово, нажмите Done.",
            reply_markup=reply_markup
        )
    elif text == "done":
        await done(update, context)
    else:
        context.user_data['last_text'] = update.message.text
        await update.message.reply_text(
            "Если хотите добавить фото, отправьте их сейчас. Когда всё готово, нажмите Done."
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'last_text' not in context.user_data:
        await update.message.reply_text("Сначала отправьте текст заявки по форме!")
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    photo_b64 = base64.b64encode(file_bytes).decode('utf-8')

    if 'photos' not in context.user_data:
        context.user_data['photos'] = []
    context.user_data['photos'].append(photo_b64)
    await update.message.reply_text("Фото добавлено. Можете отправить ещё или напишите /done для отправки заявки.")

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    application_id = str(uuid.uuid4())[:8]
    text = context.user_data.get('last_text', '')
    photos = context.user_data.get('photos', [])
    lines = text.strip().split('\n')
    name = lines[0] if len(lines) > 0 else ''
    department = lines[1] if len(lines) > 1 else ''
    details = '\n'.join(lines[2:]) if len(lines) > 2 else ''

    data = {
    'name': name,
    'department': department,
    'details': details,
    'photos': photos,
    'chat_id': update.effective_user.id,
    'application_id': application_id
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SERVER_URL, json=data) as response:
                response.raise_for_status()
        await update.message.reply_text(f"Ваш уникальный идентификатор заявки: {application_id}")
        keyboard = [[KeyboardButton("Start")]]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            "Чтобы подать новую, нажмите Start.",
            reply_markup=reply_markup
        )
        context.user_data.clear()
    except Exception as e:
        await update.message.reply_text("Ошибка при отправке заявки. Попробуйте позже.")

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler('done', done))
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(
    filters.Regex(r'Заявка\s+[a-f0-9]{8}\s+выполнена'), handle_group_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.run_polling()

if __name__ == '__main__':
    main()