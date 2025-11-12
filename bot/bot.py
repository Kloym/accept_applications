import logging
import base64
import aiohttp
import sqlite3
import uuid
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from dotenv import load_dotenv
import os
from op import DEPARTMENTS, DEPARTMENTS_PER_PAGE

load_dotenv()

# Включение логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._application").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

SERVER_URL = 'http://127.0.0.1:5000/applications'
TOKEN = os.getenv('TOKEN')
DEPARTMENTS = sorted(DEPARTMENTS)
NOTIFY_CHAT_IDS = [413685748, 681519443]
USERNAME_TO_FIO = {
    "pasheug": "Пашков Евгений Олегович",
    "NRiskin": "Рискин Никита Дмитриевич"
}


def get_db():
    conn = sqlite3.connect('applications.db')
    conn.row_factory = sqlite3.Row
    return conn

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("Start")],
        [KeyboardButton("Обновить фото")],
        [KeyboardButton("Дополнить заявку")],
        [KeyboardButton("Проверить статус заявки")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "<b>Что умеет этот бот?</b>\n\n"
        "Этот бот предназначен для быстрой и удобной подачи заявок в техническую поддержку КИС ЕМИАС в нашей организации.\n\n"
        "<b>Возможности:</b>\n"
        "📋 Оформление новой заявки с указанием ФИО, отделения, IP-адреса и описанием проблемы.\n"
        "📸 Прикрепление фотографий или скриншотов к заявке.\n"
        "📝 Возможность дополнить уже отправленную заявку дополнительной информацией.\n"
        "🔄 Обновление фотографии по уже созданной заявке.\n"
        "📊 Получение уникального идентификатора для отслеживания статуса заявки.\n"
        "✅ Уведомление о выполнении вашей заявки.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Нажмите кнопку <b>Start</b> для создания новой заявки.\n"
        "2. Следуйте инструкциям бота: введите ФИО, пароль от ЕМИАС, IP-адрес, выберите отделение и опишите проблему.\n"
        "3. При необходимости прикрепите фото.\n"
        "4. После отправки заявки вы получите уникальный ID, по которому сможете обновлять фото или дополнять заявку. При нажатии на ID вы удобно можете его скопировать.\n"
        "5. Для обновления фото или дополнения заявки используйте соответствующие кнопки в меню.",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

def mark_application_done(app_id, done_by):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    chat_id, name = row['chat_id'], row['name']
    archived_at = datetime.now().strftime('%Y-%m-%d %H:%M')
    cursor.execute(
        "UPDATE applications SET status = 'done', archived_at = ?, done_by = ? WHERE application_id = ?",
        (archived_at, done_by, app_id)
    )
    conn.commit()
    conn.close()
    return chat_id, name

def filter_departments_by_letter(departments, letter):
    return [dep for dep in departments if dep.upper().startswith(letter.upper())]

def get_departments_inline_keyboard(page=0, filter_letter=None):
    if filter_letter:
        depatments = filter_departments_by_letter(DEPARTMENTS, filter_letter)
    else:
        depatments = DEPARTMENTS
    start = page * DEPARTMENTS_PER_PAGE
    end = start + DEPARTMENTS_PER_PAGE
    departments_page = depatments[start:end]
    keyboard = [
        [InlineKeyboardButton(dep, callback_data=f"dep_idx:{i}")]
        for i, dep in enumerate(departments_page, start=start)
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"dep_page:{page-1}"))
    if end < len(DEPARTMENTS):
        nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"dep_page:{page+1}"))
    if nav:
        keyboard.append(nav)
    used_letters = sorted(set(dep[0].upper() for dep in DEPARTMENTS))
    letters_per_row = 7
    letter_buttons = [InlineKeyboardButton(letter, callback_data=f"dep_letter:{letter}") for letter in used_letters]
    letter_rows = [letter_buttons[i:i+letters_per_row] for i in range(0, len(letter_buttons), letters_per_row)]
    keyboard.extend(letter_rows)
    keyboard.append([InlineKeyboardButton("Все", callback_data="dep_letter:all")])
    return InlineKeyboardMarkup(keyboard)


async def finish_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args[0]) != 8:
        await update.message.reply_text("Укажите id заявки, например: /done 1234abcd")
        return
    app_id = args[0]
    username = update.effective_user.username
    done_by = USERNAME_TO_FIO.get(username, username)
    result = mark_application_done(app_id, done_by)
    if not result:
        await update.message.reply_text(f'Заявка {app_id} не найдена в базе данных')
        return
    chat_id, name = result
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{name.title()}, ваша заявка <code>{app_id}</code> выполнена!",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text("Ошибка при отправке уведомления.")

async def whisper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Используйте: /w <id> <сообщение>")
        return
    app_id = args[0]
    message_text = " ".join(args[1:])
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(f"Заявка {app_id} не найдена.")
        return
    chat_id, name = row['chat_id'], row['name']
    sender = update.effective_user.username or "Сотрудник"
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Сообщение по вашей заявке <code>{app_id}</code> от @{sender}:\n\nРешение: {message_text}",
            parse_mode='HTML'
        )
        await update.message.reply_text("Сообщение отправлено.")
    except Exception as e:
        await update.message.reply_text("Ошибка при отправке сообщения.")
        
async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args[0]) != 8:
        await update.message.reply_text("Укажите id заявки, например: /e 1234abcd")
        return
    app_id = args[0]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, name, status FROM applications WHERE application_id = ?", (app_id,))
    row = cursor.fetchone()
    if not row:
        await update.message.reply_text(f'Заявка {app_id} не найдена в базе данных')
        conn.close()
        return
    chat_id, name, status = row['chat_id'], row['name'], row['status']
    if status == 'active':
        await update.message.reply_text(f'Заявка {app_id} уже активна')
        conn.close()
        return
    cursor.execute(
        "UPDATE applications SET status = 'active', archived_at = NULL, done_by = NULL WHERE application_id = ?",
        (app_id,)
    )
    conn.commit()
    conn.close()
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{name}, ваша заявка <code>{app_id}</code> возвращена в работу!",
            parse_mode='HTML'
        )
        await update.message.reply_text(f'Заявка {app_id} возвращена в работу и пользователь уведомлён.')
    except Exception as e:
        await update.message.reply_text("Ошибка при отправке уведомления пользователю.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    state = context.user_data.get("state")

    if text.lower() == 'обновить фото':
        context.user_data['state'] = 'wait_update_id'
        await update.message.reply_text(
            '<b>🔄 Обновление фото</b>\nВведите уникальный идентификатор заявки:',
            parse_mode='HTML'
        )
        return
    if state == 'wait_update_id':
        context.user_data['update_id'] = text.lower()
        context.user_data['state'] = 'wait_update_photo'
        await update.message.reply_text(
            '📸 <b>Отправьте новое фото для заявки.</b>',
            parse_mode='HTML'
        )
        return
    if text.lower() == 'дополнить заявку':
        context.user_data.clear()
        context.user_data['state'] = 'wait_append_id'
        await update.message.reply_text(
            '<b>📝 Дополнение заявки</b>\nВведите уникальный идентификатор заявки, которую хотите дополнить:',
            parse_mode='HTML'
        )
        return
    if text.lower() == 'проверить статус заявки':
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT application_id, department, details, status FROM applications WHERE chat_id=? AND status='active'",
            (update.effective_user.id,)
        )
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("У вас нет активных заявок.")
            return
        keyboard = [
            [InlineKeyboardButton(
                f"{row['application_id']} | {row['details']}",
                callback_data=f"check_status:{row['application_id']}"
            )] for row in rows
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Выберите заявку для проверки статуса:",
            reply_markup=reply_markup
        )
        return

    if state == 'wait_append_id':
        context.user_data['append_id'] = text.lower()
        context.user_data['state'] = 'wait_append_text'
        await update.message.reply_text(
            '✍️ <b>Введите дополнительный текст для заявки:</b>',
            parse_mode='HTML'
        )
        return
    if state == 'wait_append_text':
        application_id = context.user_data['append_id']
        username = update.effective_user.username
        extra_text = text
        data = {
            "application_id": application_id,
            "username": username,
            "extra_text": extra_text
        }
        server_url = "http://127.0.0.1:5000/append_details"
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(server_url, json=data) as response:
                    if response.status == 200:
                        await update.message.reply_text(
                            '<b>✅ Текст успешно добавлен к заявке!</b>',
                            parse_mode='HTML'
                        )
                    else:
                        await update.message.reply_text(
                            '<b>❌ Ошибка: заявка не найдена или не принадлежит вам.</b>',
                            parse_mode='HTML'
                        )
        except Exception as e:
            await update.message.reply_text("Ошибка при дополнении заявки. Попробуйте позже.")
        context.user_data.clear()
        return

    if text.lower() == 'start':
        context.user_data.clear()
        context.user_data['state'] = 'wait_name'
        await update.message.reply_text(
            '<b>👋 Введите ФИО:</b>',
            parse_mode='HTML'
        )
        return
    
    if state == 'wait_name':
        fio = text
        parts = fio.split()
        if len(parts) != 3 or any(len(part) < 3 for part in parts):
            await update.message.reply_text(
                "Пожалуйста, введите ФИО полностью (например: Сергеев Алексей Андреевич) без сокращений."
            )
            return
        context.user_data['name'] = fio
        context.user_data['state'] = 'wait_emiac_password'
        await update.message.reply_text(
            '<b>🔑 Теперь введите пароль от ЕМИАС:</b>',
            parse_mode='HTML'
        )
        return
    if state == 'wait_emiac_password':
        context.user_data['emiac_password'] = text
        context.user_data['state'] = 'wait_ip'
        await update.message.reply_text(
            '<b>🌐 Теперь введите IP адрес компьютера:</b>',
            parse_mode='HTML'
        )
        return
    if state == 'wait_ip':
        context.user_data['ip_address'] = text
        context.user_data['state'] = 'wait_department'
        context.user_data['dep_page'] = 0
        await update.message.reply_text(
            "<b>🗂️ Выберите отделение:</b>",
            reply_markup=get_departments_inline_keyboard(0),
            parse_mode='HTML'
        )
        return
    if state == 'wait_details':
        context.user_data['details'] = text
        context.user_data['state'] = 'wait_photos'
        keyboard = [[KeyboardButton('✔️ Done')]]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            "<b>🖼️ Если хотите добавить скриншот(фото) ошибки, отправьте их сейчас. Когда всё готово, нажмите Done.</b>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return
    if state == "wait_photos" and text.lower() in ["done", "✔️ done"]:
        await done(update, context)
        return
    
async def check_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if data.startswith("check_status:"):
        app_id = data.split(":", 1)[1]
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, details FROM applications WHERE application_id=? AND chat_id=?",
            (app_id, query.from_user.id)
        )
        row = cursor.fetchone()
        if not row:
            await query.answer("Заявка не найдена.", show_alert=True)
            conn.close()
            return
        status = row['status']
        details = row['details']
        if status == 'done':
            await query.edit_message_text(
                f"Заявка <code>{app_id}</code> уже выполнена и больше не отображается в списке.",
                parse_mode='HTML'
            )
        else:
            await query.edit_message_text(
                f"Заявка <code>{app_id}</code>\n"
                f"Описание: {details}\n"
                f"Статус: В работе",
                parse_mode='HTML'
            )
        await query.answer()
        cursor.execute(
            "SELECT application_id, details FROM applications WHERE chat_id=? AND status='active'",
            (query.from_user.id,)
        )
        rows = cursor.fetchall()
        conn.close()
        if rows:
            keyboard = [
                [InlineKeyboardButton(
                    f"{row['application_id']} | {row['details']}",
                    callback_data=f"check_status:{row['application_id']}"
                )] for row in rows
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="Выберите заявку для проверки статуса:",
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="У вас нет активных заявок."
            )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    if state == "wait_update_photo":
        application_id = context.user_data.get("update_id")
        username = update.effective_user.username
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        photo_b64 = base64.b64encode(file_bytes).decode('utf-8')
        data = {
            "application_id": application_id,
            "username": username,
            "photo": photo_b64
        }
        server_url = "http://127.0.0.1:5000/update_photo"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(server_url, json=data) as response:
                    if response.status == 200:
                        await update.message.reply_text("Фото для заявки успешно обновлено!")
                    else:
                        await update.message.reply_text("Ошибка: заявка не найдена или не принадлежит вам.")
        except Exception as e:
            await update.message.reply_text("Ошибка при обновлении фото. Попробуйте позже.")

        context.user_data.clear()
        return
    if state != "wait_photos":
        await update.message.reply_text("Сначала заполните заявку по форме!")
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    photo_b64 = base64.b64encode(file_bytes).decode('utf-8')

    if 'photos' not in context.user_data:
        context.user_data['photos'] = []
    context.user_data['photos'].append(photo_b64)
    await update.message.reply_text("Фото добавлено. Можете отправить ещё или нажмите кнопку Done для отправки заявки.")

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    application_id = str(uuid.uuid4())[:8]
    name = context.user_data.get('name', '')
    ip = context.user_data.get('ip_address', '')
    emiac = context.user_data.get('emiac_password', '')
    department = context.user_data.get('department', '')
    details = context.user_data.get('details', '')
    photos = context.user_data.get('photos', [])
    username = update.effective_user.username or ""

    data = {
    'name': name,
    'ip': ip,
    'emiac': emiac,
    'department': department,
    'details': details,
    'photos': photos,
    'chat_id': update.effective_user.id,
    'username': username,
    'application_id': application_id,
    'status': 'active'
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SERVER_URL, json=data) as response:
                response.raise_for_status()
        await update.message.reply_text(
            "<b>✅ Ваша заявка принята в работу!</b>\n"
            "Уникальный идентификатор заявки: <code>{}</code>".format(application_id),
            parse_mode='HTML'
        )
        for notify_id in NOTIFY_CHAT_IDS:
            try:
                await context.bot.send_message(
                    chat_id=notify_id,
                    text='Вам поступила новая заявка'
                )
            except Exception as e:
                print(f"Ошибка при отправке уведомления: {e}")
        keyboard = [
            [KeyboardButton("Start")],
            [KeyboardButton("Обновить фото")],
            [KeyboardButton("Дополнить заявку")],
            [KeyboardButton("Проверить статус заявки")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            "Чтобы подать новую заявку, нажмите Start.\n"
            "Для обновления скриншота(фото) по заявке — Обновить фото.\n"
            "Для дополнения текста — Дополнить заявку.\n"
            "Для проверки статуса — Проверьте статус заявки.",
            reply_markup=reply_markup
        )
        context.user_data.clear()
    except Exception as e:
        await update.message.reply_text("Ошибка при отправке заявки. Попробуйте позже.")

async def department_callback(update: Update, context):
    query = update.callback_query
    data = query.data
    page = context.user_data.get('dep_page', 0)
    filter_letter = context.user_data.get('dep_filter')
    if filter_letter:
        departments = filter_departments_by_letter(DEPARTMENTS, filter_letter)
    else:
        departments = DEPARTMENTS

    if data.startswith("dep_page:"):
        page = int(data.split(":")[1])
        context.user_data['dep_page'] = page
        await query.edit_message_reply_markup(reply_markup=get_departments_inline_keyboard(page, filter_letter=filter_letter))
        await query.answer()
        return

    if data.startswith("dep_idx:"):
        dep_index = int(data.split(":", 1)[1])
        department = departments[dep_index]
        context.user_data['department'] = department
        context.user_data['state'] = 'wait_details'
        await query.edit_message_text(f"Вы выбрали: {department}")
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="Теперь опишите проблему:"
        )
        await query.answer()
        return
    if data.startswith('dep_letter:'):
        letter = data.split(':', 1)[1]
        if letter == 'all':
            context.user_data.pop('dep_filter', None)
            context.user_data['dep_page'] = 0
            await query.edit_message_reply_markup(
                reply_markup=get_departments_inline_keyboard(0)
            )
        else:
            context.user_data['dep_filter'] = letter
            context.user_data['dep_page'] = 0
            await query.edit_message_reply_markup(
                reply_markup=get_departments_inline_keyboard(0, filter_letter=letter)
            )
        await query.answer()
        return

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler('done', done))
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('q', finish_command))
    application.add_handler(CommandHandler('w', whisper_command))
    application.add_handler(CommandHandler('e', restore_command))
    # application.add_handler(MessageHandler(
    # filters.Regex(r'Заявка\s+[a-f0-9]{8}\s+выполнена'), handle_group_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(check_status_callback, pattern="^check_status:"))
    application.add_handler(CallbackQueryHandler(department_callback))
    application.run_polling()

if __name__ == '__main__':
    main()