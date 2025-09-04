from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)
import requests
import asyncio
import uuid
from dotenv import load_dotenv
import os


load_dotenv()
os.makedirs('photos', exist_ok=True)
TOKEN = os.getenv("TOKEN")

media_groups = {}
media_group_timers = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Здравствуйте! Отправьте заявку, и я её запишу.')

async def send_album_to_server(group_id, context):
    album = media_groups.pop(group_id, None)
    if not album:
        return
    ticket_id = album['ticket_id']
    caption = album['caption']
    files = []
    for idx, file_path in enumerate(album['photos']):
        files.append(('photos', (os.path.basename(file_path), open(file_path, 'rb'), 'image/jpeg')))
    data = {'ticket_id': ticket_id, 'caption': caption}
    requests.post(
        "http://127.0.0.1:5000/add_ticket",
        data=data,
        files=files
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.media_group_id:
        group_id = update.message.media_group_id
        if group_id not in media_groups:
            ticket_id = str(uuid.uuid4())
            media_groups[group_id] = {
                'ticket_id': ticket_id,
                'photos': [],
                'caption': update.message.caption,
                'chat_id': update.message.chat_id
            }
        else:
            ticket_id = media_groups[group_id]['ticket_id']
        photo_file = await update.message.photo[-1].get_file()
        file_path = f'photos/{ticket_id}_{len(media_groups[group_id]["photos"])+1}.jpg'
        await photo_file.download_to_drive(file_path)
        media_groups[group_id]['photos'].append(file_path)
        if group_id in media_group_timers:
            media_group_timers[group_id].cancel()
        media_group_timers[group_id] = asyncio.get_event_loop().call_later(
            4, lambda: asyncio.create_task(send_album_to_server(group_id, context))
        )

    elif update.message.photo:
        ticket_id = str(uuid.uuid4())
        photo_file = await update.message.photo[-1].get_file()
        file_path = f"photos/{ticket_id}.jpg"
        await photo_file.download_to_drive(file_path)
        caption = update.message.caption
        print(f"Получено фото {ticket_id}: {file_path} (caption: {caption})")
        with open(file_path, "rb") as f:
            requests.post(
                "http://127.0.0.1:5000/add_ticket",
                data={"ticket_id": ticket_id, "caption": caption},
                files={"photo": f}
        )
        await update.message.reply_text(f'Ваше фото получено! ID: {ticket_id}')
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