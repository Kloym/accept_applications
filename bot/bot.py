from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)
import uuid
from dotenv import load_dotenv
import os


load_dotenv()
os.makedirs('photos', exist_ok=True)
TOKEN = os.getenv("TOKEN")

media_groups = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Здравствуйте! Отправьте заявку, и я её запишу.')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.media_group_id:
        group_id = update.message.media_group_id
        if group_id not in media_groups:
            ticket_id = str(uuid.uuid4())
            media_groups[group_id] = {
                'ticket_id': ticket_id,
                'photos': [],
                'caption': update.message.caption
            }
        else:
            ticket_id = media_groups[group_id]['ticket_id']
        photo_file = update.message.photo[-1].get_file()
        file_path = f'photos/{ticket_id}_{len(media_groups[group_id]['photos'])+1}.jpg'
        photo_file.download(file_path)
        media_groups[group_id]['photos'].append(file_path)
        if update.message.caption:
            update.message.reply_text(f'Ваши фото получены! ID: {ticket_id}')
            print(
                f"Получена заявка {ticket_id}: {media_groups[group_id]['caption']} "
                f"(фото: {media_groups[group_id]['photos']})"
            )
    elif update.message.photo:
        ticket_id = str(uuid.uuid4())
        photo_file = update.message.photo[-1].get_file()
        file_path = f"photos/{ticket_id}.jpg"
        photo_file.download(file_path)
        caption = update.message.caption
        print(f"Получено фото {ticket_id}: {file_path} (caption: {caption})")
        update.message.reply_text(f'Ваше фото получено! ID: {ticket_id}')
    elif update.message.text:
        ticket_id = str(uuid.uuid4())
        print(f"Получена заявка {ticket_id}: {update.message.text}")
        update.message.reply_text(f'Ваша заявка получена! ID: {ticket_id}')
    else:
        update.message.reply_text('Пожалуйста, отправьте текст или фото.')

# Основная функция запуска бота
def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__':
    main()