import logging
import requests
import uuid
import base64
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

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Приветствую, заполните заявку по форме:\n"
        "Имя\n"
        "Отделение\n"
        "Текст проблемы\n"
        "Фото (если есть) отправьте отдельными сообщениями после текста.\n"
        "Когда все фото отправлены, напишите /done для отправки заявки."
    )

# Обработчик текстовых сообщений (заявок)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['last_text'] = update.message.text
    context.user_data['photos'] = []
    await update.message.reply_text(
        "Если хотите добавить фото, отправьте их по одному. "
        "Когда закончите — напишите /done для отправки заявки."
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
    'chat_id': update.effective_user.id
    }
    try:
        response = requests.post(SERVER_URL, json=data)
        response.raise_for_status()
        await update.message.reply_text("Ваша заявка отправлена! Спасибо.")
        context.user_data.clear()
    except Exception as e:
        await update.message.reply_text("Ошибка при отправке заявки. Попробуйте позже.")

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler('done', done))
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.run_polling()

if __name__ == '__main__':
    main()