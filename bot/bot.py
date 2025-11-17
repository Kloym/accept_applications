import logging
import base64
import aiohttp
import sqlite3
import uuid
import re
import asyncio
from enum import Enum
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, 
    filters, CallbackQueryHandler, ConversationHandler
)
from dotenv import load_dotenv
import os
from op import DEPARTMENTS, DEPARTMENTS_PER_PAGE


load_dotenv()
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._application").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('TOKEN')
BASE_SERVER_URL = 'http://127.0.0.1:5000'
DB_FILE = 'applications.db'
DEPARTMENTS = sorted(DEPARTMENTS)
NOTIFY_CHAT_IDS = [308035415]
USERNAME_TO_FIO = {
    "pasheug": "Пашков Евгений Олегович",
    "NRiskin": "Рискин Никита Дмитриевич"
}


class States(Enum):
    START_ROUTES = 0
    
    START_WAIT_NAME = 1
    START_WAIT_IP = 3
    START_WAIT_DEPARTMENT = 4
    START_WAIT_DETAILS = 5
    START_WAIT_PHOTOS = 6
    START_CONFIRMATION = 7 
    
    UPDATE_WAIT_ID = 10
    UPDATE_WAIT_PHOTO = 11
    
    APPEND_WAIT_ID = 20
    APPEND_WAIT_TEXT = 21
    
    RETURN_WAIT_ID = 30
    RETURN_WAIT_REASON = 31

    PASSWORD_REQUEST_WAIT_PASSWORD = 40


class ApiService:
    """
    Отвечает за ВСЕ запросы к Flask API.
    """
    def __init__(self, base_url):
        self.base_url = base_url
        
    async def _post_json(self, endpoint, data):
        url = f"{self.base_url}/{endpoint}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=data) as response:
                    return response.status, await response.json()
            except aiohttp.ClientError as e:
                logger.error(f"API ClientError: {e}")
                return 500, {"error": str(e)}
            except Exception as e:
                logger.error(f"API Generic Error: {e}")
                return 500, {"error": str(e)}

    async def post_new_application(self, data):
        status, _ = await self._post_json('applications', data)
        return status == 201

    async def update_photos(self, application_id, username, photos_b64_list):
        data = {
            "application_id": application_id,
            "username": username,
            "photos": photos_b64_list
        }
        status, _ = await self._post_json('update_photos', data)
        return status == 200

    async def append_details(self, application_id, username, extra_text):
        data = {
            "application_id": application_id,
            "username": username,
            "extra_text": extra_text
        }
        status, _ = await self._post_json('append_details', data)
        return status == 200

    async def restore_application(self, application_id):
        status, data = await self._post_json(f'restore/{application_id}', {})
        if status == 200:
            return data.get('chat_id'), data.get('name')
        else:
            return None


class DbService:
    def __init__(self, db_file):
        self.db_file = db_file

    def _get_db(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

    def mark_application_done(self, app_id, done_by):
        conn = self._get_db()
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

    def get_app_for_whisper(self, app_id):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,))
        row = cursor.fetchone()
        conn.close()
        return (row['chat_id'], row['name']) if row else None

    def get_done_apps_by_chat_id(self, chat_id, limit=5):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT application_id, details FROM applications WHERE chat_id=? AND status='done' ORDER BY archived_at DESC LIMIT ?",
            (chat_id, limit)
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_active_apps_by_chat_id(self, chat_id):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT application_id, department, details, status FROM applications WHERE chat_id=? AND status='active'",
            (chat_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_app_status_for_user(self, app_id, chat_id):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, details FROM applications WHERE application_id=? AND chat_id=?",
            (app_id, chat_id)
        )
        row = cursor.fetchone()
        conn.close()
        return (row['status'], row['details']) if row else None

    def get_user_info_for_app(self, app_id: str):
        """Получает chat_id и name по ID заявки"""
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,))
        row = cursor.fetchone()
        conn.close()
        return (row['chat_id'], row['name']) if row else None

    def get_app_ids_for_user(self, chat_id: int) -> list:
        """Получает список всех ID заявок для конкретного пользователя"""
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT application_id FROM applications WHERE chat_id = ?", (chat_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row['application_id'] for row in rows]

api_client = ApiService(BASE_SERVER_URL)
db_service = DbService(DB_FILE)


MAIN_KEYBOARD = ReplyKeyboardMarkup([
        [KeyboardButton("Start")],
        [KeyboardButton("Добавить фото")],
        [KeyboardButton("Дополнить заявку")],
        [KeyboardButton("Проверить статус заявки")],
        [KeyboardButton("Вернуть заявку в работу")]
    ], resize_keyboard=True)

DONE_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton('✔️ Done')]], resize_keyboard=True)

CONFIRM_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("✅ Отправить", callback_data="confirm_send"),
        InlineKeyboardButton("❌ Отменить", callback_data="confirm_cancel")
    ]
])

def is_valid_fio(fio):
    parts = fio.split()
    return len(parts) >= 2 and all(len(part) >= 2 for part in parts)

def is_valid_ip(ip):
    return re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip)

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню /start или /help"""
    
    start_text = (
        "<b>Что умеет этот бот?</b>\n\n"
        "Этот бот предназначен для быстрой и удобной подачи заявок в техническую поддержку КИС ЕМИАС в нашей организации.\n\n"
        "<b>Возможности:</b>\n"
        "📋 <b>Start</b> - Оформление новой заявки.\n"
        "📸 <b>Добавить фото</b> - Добавление фотографий к существующей заявке.\n"
        "📝 <b>Дополнить заявку</b> - Добавление текста к существующей заявке.\n"
        "📊 <b>Проверить статус заявки</b> - Просмотр статуса ваших активных заявок.\n"
        "✅ Уведомление о выполнении вашей заявки.\n"
        "♻️ <b>Вернуть заявку в работу</b> - Если проблема не решена.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Нажмите кнопку <b>Start</b> для создания новой заявки.\n"
        "2. Следуйте инструкциям бота: введите ФИО, IP-адрес, выберите отделение и опишите проблему.\n"
        "3. При необходимости прикрепите фото.\n"
        "4. После отправки заявки вы получите уникальный ID. При нажатии на ID вы удобно можете его скопировать.\n\n"
        "Вы можете отменить любое действие в любой момент, отправив команду /cancel."
    )
    
    await update.message.reply_text(
        text=start_text,
        reply_markup=MAIN_KEYBOARD,
        parse_mode=ParseMode.HTML
    )
    
    return ConversationHandler.END

async def finish_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: Завершить заявку (/q)"""
    args = context.args
    if not args or len(args[0]) != 8:
        await update.message.reply_text("Укажите id заявки, например: /q 1234abcd")
        return
    
    app_id = args[0]
    username = update.effective_user.username
    done_by = USERNAME_TO_FIO.get(username, username)

    result = await asyncio.to_thread(db_service.mark_application_done, app_id, done_by)
    
    if not result:
        await update.message.reply_text(f'Заявка {app_id} не найдена в базе данных')
        return
    
    chat_id, name = result
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{name.title()}, ваша заявка <code>{app_id}</code> выполнена!",
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text(f"Заявка {app_id} отмечена как выполненная.")
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления о выполнении: {e}")
        await update.message.reply_text("Ошибка при отправке уведомления.")

async def whisper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: Отправить сообщение пользователю (/w <id> <текст>)"""
    args = context.args
    if not args or len(args) < 1:
        await update.message.reply_text("Используйте: /w <id> <сообщение>\n\n"
                                        "Чтобы отправить фото, ответьте (Reply) на фотографию или документ этой командой (текст сообщения не обязателен).")
        return
    
    app_id = args[0]
    message_text = " ".join(args[1:]) if len(args) > 1 else ""
    
    file_id = None
    file_type = None
    
    reply_message = update.message.reply_to_message
    if reply_message:
        if reply_message.photo:
            file_id = reply_message.photo[-1].file_id
            file_type = 'photo'
        elif reply_message.document and reply_message.document.mime_type and reply_message.document.mime_type.startswith('image/'):
            file_id = reply_message.document.file_id
            file_type = 'document'
    
    result = await asyncio.to_thread(db_service.get_app_for_whisper, app_id)
    
    if not result:
        await update.message.reply_text(f"Заявка {app_id} не найдена.")
        return
    
    chat_id, name = result
    sender = update.effective_user.username or "Сотрудник"

    solution_text = ""
    if message_text:
        solution_text = f"\n\n<b>Решение:</b>\n{message_text.capitalize()}"

    full_caption = (
        f"Сообщение по вашей заявке <code>{app_id}</code> от @{sender}:"
        f"{solution_text}"
    )

    try:
        if file_type == 'photo':
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=full_caption,
                parse_mode=ParseMode.HTML
            )
            await update.message.reply_text("Сообщение с фото отправлено.")
            
        elif file_type == 'document':
            await context.bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=full_caption,
                parse_mode=ParseMode.HTML
            )
            await update.message.reply_text("Сообщение с документом отправлено.")
        
        elif message_text:
            await context.bot.send_message(
                chat_id=chat_id,
                text=full_caption,
                parse_mode=ParseMode.HTML
            )
            await update.message.reply_text("Сообщение отправлено.")
        else:
             await update.message.reply_text("Нечего отправлять. Либо напишите текст, либо ответьте на фото/документ.")
            
    except Exception as e:
        logger.error(f"Ошибка отправки /w: {e}")
        await update.message.reply_text(f"Ошибка при отправке сообщения: {e}")

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: Восстановить заявку (/e)"""
    args = context.args
    if not args or len(args[0]) != 8:
        await update.message.reply_text("Укажите id заявки, например: /e 1234abcd")
        return
    
    app_id = args[0]
    
    result = await api_client.restore_application(app_id)
    
    if not result:
        await update.message.reply_text(f'Заявка {app_id} не найдена или уже активна.')
        return

    chat_id, name = result
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{name.title()}, ваша заявка <code>{app_id}</code> возвращена в работу!",
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text(f'Заявка {app_id} возвращена в работу и пользователь уведомлён.')
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления о восстановлении: {e}")
        await update.message.reply_text("Ошибка при отправке уведомления пользователю.")


async def check_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажатие кнопки в 'Проверить статус заявки'"""
    query = update.callback_query
    data = query.data
    await query.answer()

    if data.startswith("check_status:"):
        app_id = data.split(":", 1)[1]
        
        result = await asyncio.to_thread(db_service.get_app_status_for_user, app_id, query.from_user.id)
        
        if not result:
            await query.answer("Заявка не найдена.", show_alert=True)
            return

        status, details = result
        if status == 'done':
            await query.edit_message_text(
                f"<b>Заявка:</b> <code>{app_id}</code>\n"
                f"<b>Статус:</b> ✅ Выполнена\n\n"
                f"(Эта заявка скоро пропадет из этого списка)",
                parse_mode=ParseMode.HTML
            )
        else:
            await query.edit_message_text(
                f"<b>Заявка:</b> <code>{app_id}</code>\n"
                f"<b>Описание:</b> {details}\n"
                f"<b>Статус:</b> ⏳ В работе",
                parse_mode=ParseMode.HTML
            )
        
        await conv_ask_check_status(update, context, from_callback=True)

async def department_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажатие кнопок в меню выбора Отделения"""
    query = update.callback_query
    data = query.data
    await query.answer()

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
        return States.START_WAIT_DEPARTMENT

    if data.startswith("dep_idx:"):
        dep_index = int(data.split(":", 1)[1])
        department = departments[dep_index]
        context.user_data['department'] = department
        await query.edit_message_text(f"Вы выбрали: {department}")
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="<b>✍️ Теперь опишите проблему:</b>",
            parse_mode=ParseMode.HTML
        )
        return States.START_WAIT_DETAILS

    if data.startswith('dep_letter:'):
        letter = data.split(':', 1)[1]
        if letter == 'all':
            context.user_data.pop('dep_filter', None)
        else:
            context.user_data['dep_filter'] = letter
        
        context.user_data['dep_page'] = 0
        await query.edit_message_reply_markup(
            reply_markup=get_departments_inline_keyboard(0, filter_letter=context.user_data.get('dep_filter'))
        )
        await query.answer()
        return States.START_WAIT_DEPARTMENT

async def conv_ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа: Нажата кнопка 'Start'"""
    context.user_data.clear()
    await update.message.reply_text('<b>👋 Введите ФИО:</b>\n\n(Или /cancel для отмены)', parse_mode=ParseMode.HTML)
    return States.START_WAIT_NAME

async def conv_ask_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили ФИО, спрашиваем IP"""
    fio = update.message.text.strip()
    if not is_valid_fio(fio):
        await update.message.reply_text(
            "Пожалуйста, введите ФИО полностью (например: Сергеев Алексей Андреевич)."
        )
        return States.START_WAIT_NAME
    
    context.user_data['name'] = fio
    await update.message.reply_text('<b>🌐 Теперь введите IP адрес компьютера:</b>', parse_mode=ParseMode.HTML)
    return States.START_WAIT_IP

async def conv_ask_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили IP, показываем меню отделений"""
    ip_address = update.message.text.strip()
    if not is_valid_ip(ip_address):
        await update.message.reply_text(
            "<b>❌ Неверный формат IP.</b>\nПожалуйста, введите IP-адрес в формате 123.123.123.123",
             parse_mode=ParseMode.HTML
        )
        return States.START_WAIT_IP

    context.user_data['ip_address'] = ip_address
    context.user_data['dep_page'] = 0
    await update.message.reply_text(
        "<b>🗂️ Выберите отделение:</b>",
        reply_markup=get_departments_inline_keyboard(0),
        parse_mode=ParseMode.HTML
    )
    return States.START_WAIT_DEPARTMENT

async def conv_ask_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили Описание, просим фото"""
    context.user_data['details'] = update.message.text.strip()
    context.user_data['photos'] = []
    await update.message.reply_text(
        "<b>🖼️ Если хотите добавить скриншот(фото) ошибки, отправьте их сейчас.</b>\n"
        "Можно отправить несколько. Когда всё готово, нажмите <b>Done</b>.",
        reply_markup=DONE_KEYBOARD,
        parse_mode=ParseMode.HTML
    )
    return States.START_WAIT_PHOTOS

async def conv_add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловим фото или документ в состоянии START_WAIT_PHOTOS"""
    photo_b64 = None
    try:
        if update.message.photo:
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            file_bytes = await file.download_as_bytearray()
            photo_b64 = base64.b64encode(file_bytes).decode('utf-8')
        
        elif update.message.document and update.message.document.mime_type.startswith('image/'):
            document = update.message.document
            file = await context.bot.get_file(document.file_id)
            file_bytes = await file.download_as_bytearray()
            photo_b64 = base64.b64encode(file_bytes).decode('utf-8')
        else:
            await update.message.reply_text("Это не фото. Пришлите фото или нажмите 'Done'.")
            return States.START_WAIT_PHOTOS

        if 'photos' not in context.user_data:
            context.user_data['photos'] = []
        context.user_data['photos'].append(photo_b64)
        
        count = len(context.user_data['photos'])
        await update.message.reply_text(f"Фото {count} добавлено. Можете отправить ещё или нажмите 'Done'.")
    
    except Exception as e:
        logger.error(f"Ошибка при обработке фото: {e}")
        await update.message.reply_text("Не удалось обработать файл. Попробуйте еще раз.")
        
    return States.START_WAIT_PHOTOS

async def conv_show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показываем сводку перед отправкой."""
    
    data = context.user_data
    name = data.get('name', 'Не указано').title()
    ip = data.get('ip_address', 'Не указан')
    department = data.get('department', 'Не указано')
    details = data.get('details', 'Нет описания')
    photos_count = len(data.get('photos', []))

    summary_text = (
        "<b>Пожалуйста, проверьте данные заявки:</b>\n\n"
        f"<b>ФИО:</b> {name}\n"
        f"<b>IP-адрес:</b> {ip}\n"
        f"<b>Отделение:</b> {department}\n"
        f"<b>Описание:</b> {details}\n"
        f"<b>Фото:</b> {photos_count} шт.\n\n"
        "Всё верно?"
    )
    
    await update.message.reply_text(
        summary_text,
        parse_mode=ParseMode.HTML,
        reply_markup=CONFIRM_KEYBOARD
    )
    
    return States.START_CONFIRMATION

async def conv_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка '✅ Отправить', отправляем заявку"""
    
    query = update.callback_query
    if query:
        await query.answer("Отправка...")
        await query.edit_message_text("⏳ Отправляю заявку на сервер...", reply_markup=None)
    
    application_id = str(uuid.uuid4())[:8]
    data = {
        'name': context.user_data.get('name'),
        'ip': context.user_data.get('ip_address'),
        'department': context.user_data.get('department'),
        'details': context.user_data.get('details'),
        'photos': context.user_data.get('photos', []),
        'chat_id': update.effective_user.id,
        'username': update.effective_user.username or "",
        'application_id': application_id,
        'status': 'active'
    }
    
    success = await api_client.post_new_application(data)
    
    chat_id = update.effective_user.id

    if not success:
        await context.bot.send_message(
            chat_id,
            "❌ Ошибка при отправке заявки на сервер. Попробуйте позже.", 
            reply_markup=MAIN_KEYBOARD
        )
        context.user_data.clear()
        return ConversationHandler.END

    final_text = (
        f"<b>✅ Ваша заявка принята в работу!</b>\n"
        f"Уникальный идентификатор заявки: <code>{application_id}</code>\n\n"
        "Чтобы подать новую заявку, нажмите Start.\n"
        "Для добавления скриншота(фото) по заявке — Добавить фото.\n"
        "Для дополнения текста — Дополнить заявку.\n"
        "Для проверки статуса — Проверить статус заявки.\n"
        "Для возврата заявки - вернуть заявку в работу."
    )
    
    await context.bot.send_message(
        chat_id,
        final_text,
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD
    )
    
    details_text = data.get('details', '')
    if len(details_text) > 40:
        truncated_details = details_text[:40] + "..."
    else:
        truncated_details = details_text

    message_for_admins = (
        f"🔔 <b>Новая заявка!</b> 🔔\n\n"
        f"<b>ID:</b> <code>{application_id}</code>\n"
        f"<b>От:</b> {data['name']}\n"
        f"<b>Фото:</b> {len(data['photos'])} шт.\n\n"
        f"<b>Описание:</b>\n{truncated_details}"
    )

    for notify_id in NOTIFY_CHAT_IDS:
        try:
            await context.bot.send_message(
                chat_id=notify_id, 
                text=message_for_admins,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Ошибка отправки уведомления админу {notify_id}: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def conv_ask_update_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа: Нажата кнопка 'Добавить фото'"""
    context.user_data.clear()
    await update.message.reply_text(
        '<b>🔄 Добавление фото</b>\nВведите уникальный идентификатор заявки (8 символов)',
        parse_mode=ParseMode.HTML
    )
    return States.UPDATE_WAIT_ID

async def conv_ask_update_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили ID, просим фото"""
    app_id = update.message.text.strip().lower()

    if len(app_id) != 8:
        await update.message.reply_text(
            '<b>❌ Неверный формат ID.</b>\n'
            'ID должен состоять ровно из 8 символов. Попробуйте еще раз:',
            parse_mode=ParseMode.HTML
        )
        return States.UPDATE_WAIT_ID

    context.user_data['update_id'] = app_id
    context.user_data['update_photos'] = [] 
    await update.message.reply_text(
        f'📸 <b>Отправьте фото для добавления к заявке <code>{app_id}</code>.</b>\n'
        'Вы можете отправить несколько. Когда закончите, нажмите Done.',
        reply_markup=DONE_KEYBOARD,
        parse_mode=ParseMode.HTML
    )
    return States.UPDATE_WAIT_PHOTO

async def conv_process_update_photo_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловим фото для обновления и добавляем в список"""
    photo_b64 = None
    
    try:
        if update.message.photo:
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            file_bytes = await file.download_as_bytearray()
            photo_b64 = base64.b64encode(file_bytes).decode('utf-8')
        elif update.message.document and update.message.document.mime_type.startswith('image/'):
            document = update.message.document
            file = await context.bot.get_file(document.file_id)
            file_bytes = await file.download_as_bytearray()
            photo_b64 = base64.b64encode(file_bytes).decode('utf-8')
        else:
            await update.message.reply_text("Это не фото. Пришлите фото или нажмите 'Done'.")
            return States.UPDATE_WAIT_PHOTO

        if 'update_photos' not in context.user_data:
             context.user_data['update_photos'] = []
        
        context.user_data['update_photos'].append(photo_b64)
        count = len(context.user_data['update_photos'])
        await update.message.reply_text(f"Фото {count} добавлено. Можете отправить ещё или нажмите 'Done'.")

    except Exception as e:
        logger.error(f"Ошибка при добавлении фото для обновления: {e}")
        await update.message.reply_text("Ошибка при обработке фото. Попробуйте еще раз.")
        
    return States.UPDATE_WAIT_PHOTO

async def conv_process_update_photo_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка 'Done' при обновлении фото. Отправляем ВСЕ фото ОДНИМ запросом."""
    application_id = context.user_data.get("update_id")
    username = update.effective_user.username
    photos_b64_list = context.user_data.get('update_photos', [])
    
    if not photos_b64_list:
        await update.message.reply_text("Вы не отправили ни одного фото. Действие отменено.", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(f"Начинаю загрузку {len(photos_b64_list)} фото для заявки <code>{application_id}</code>...", parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)

    try:
        success = await api_client.update_photos(application_id, username, photos_b64_list)
        
        if success:
            await update.message.reply_text(f"✅ Все {len(photos_b64_list)} фото для заявки <code>{application_id}</code> успешно добавлены!", reply_markup=MAIN_KEYBOARD, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"❌ Ошибка: не удалось добавить фото. Заявка <code>{application_id}</code> не найдена или не принадлежит вам.", reply_markup=MAIN_KEYBOARD, parse_mode=ParseMode.HTML)
    
    except Exception as e:
        logger.error(f"Ошибка при отправке пачки фото {application_id}: {e}")
        await update.message.reply_text(f"❌ Произошла ошибка на стороне сервера. Попробуйте позже.", reply_markup=MAIN_KEYBOARD)
            
    context.user_data.clear()
    return ConversationHandler.END

async def conv_ask_append_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа: Нажата кнопка 'Дополнить заявку'"""
    context.user_data.clear()
    await update.message.reply_text(
        '<b>📝 Дополнение заявки</b>\nВведите уникальный идентификатор заявки (8 символов):\n\n(Или /cancel для отмены)',
        parse_mode=ParseMode.HTML
    )
    return States.APPEND_WAIT_ID

async def conv_ask_append_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили ID, просим текст"""
    app_id = update.message.text.strip().lower()

    if len(app_id) != 8:
        await update.message.reply_text(
            '<b>❌ Неверный формат ID.</b>\n'
            'ID должен состоять ровно из 8 символов. Попробуйте еще раз:',
            parse_mode=ParseMode.HTML
        )
        return States.APPEND_WAIT_ID
        
    context.user_data['append_id'] = app_id
    await update.message.reply_text(
        f'✍️ <b>Введите дополнительный текст для заявки <code>{app_id}</code>:</b>\n\n(Или /cancel для отмены)',
        parse_mode=ParseMode.HTML
    )
    return States.APPEND_WAIT_TEXT

async def conv_process_append_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили текст, отправляем в API"""
    application_id = context.user_data.get('append_id')
    username = update.effective_user.username
    extra_text = update.message.text.strip()
    
    success = await api_client.append_details(application_id, username, extra_text)
    
    if success:
        await update.message.reply_text(
            f'<b>✅ Текст успешно добавлен к заявке <code>{application_id}</code>!</b>',
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD
        )
    else:
        await update.message.reply_text(
            '<b>❌ Ошибка: заявка не найдена или не принадлежит вам.</b>',
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD
        )
        
    context.user_data.clear()
    return ConversationHandler.END


async def conv_ask_check_status(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback=False):
    """Точка входа: Нажата кнопка 'Проверить статус заявки'"""
    
    rows = await asyncio.to_thread(db_service.get_active_apps_by_chat_id, update.effective_user.id)
    
    if not rows:
        await update.message.reply_text("У вас нет активных заявок.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    keyboard = []
    for row in rows:
        details = row['details'].replace('\n', ' ')
        truncated_details = (details[:50] + '...') if len(details) > 50 else details
        
        keyboard.append([
            InlineKeyboardButton(
                f"{row['application_id']} | {truncated_details}",
                callback_data=f"check_status:{row['application_id']}"
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = "<b>Активные заявки:</b>\n(Нажмите на заявку для просмотра статуса)"
    
    if from_callback:
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    return ConversationHandler.END


async def conv_ask_return_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа: Нажата кнопка 'Вернуть заявку в работу'"""
    context.user_data.clear()
    
    rows = await asyncio.to_thread(db_service.get_done_apps_by_chat_id, update.effective_user.id, limit=5)
    
    message_text = '<b>♻️ Возврат заявки</b>\n\n'
    if not rows:
        message_text += "У вас пока нет выполненных заявок, которые можно было бы вернуть.\n\n"
    else:
        message_text += "Вот 5 ваших последних выполненных заявок:\n\n"
        for row in rows:
            details = row['details'].replace('\n', ' ')
            truncated_details = (details[:60] + '...') if len(details) > 60 else details
            message_text += (
                f"<b>ID:</b> <code>{row['application_id']}</code>\n"
                f"<b>Проблема:</b> {truncated_details}\n"
                f"---\n"
            )
        message_text += "\n"

    message_text += '<b>Введите ID заявки</b> (8 символов), которую нужно вернуть в работу:\n\n(Или /cancel для отмены)'
    
    await update.message.reply_text(message_text, parse_mode=ParseMode.HTML)
    return States.RETURN_WAIT_ID

async def conv_ask_return_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили ID, просим причину"""
    application_id = update.message.text.strip().lower()
    if len(application_id) != 8:
         await update.message.reply_text(
            '<b>❌ Неверный формат ID.</b>\n'
            'ID должен состоять ровно из 8 символов. Попробуйте еще раз:',
            parse_mode=ParseMode.HTML
        )
         return States.RETURN_WAIT_ID
             
    context.user_data['return_id'] = application_id
    await update.message.reply_text(
        '📝 <b>Опишите причину</b>\n'
        'Что вам не понравилось в выполненной заявке или какая проблема осталась?\n\n(Или /cancel для отмены)',
        parse_mode=ParseMode.HTML
    )
    return States.RETURN_WAIT_REASON

async def conv_process_return_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили причину, уведомляем админов"""
    reason = update.message.text.strip()
    application_id = context.user_data.get('return_id')
    username = update.effective_user.username or f"user_id_{update.effective_user.id}"

    message_for_admins = (
        f"⚠️ <b>Запрос на возврат заявки в работу!</b> ⚠️\n\n"
        f"<b>Пользователь:</b> @{username}\n"
        f"<b>Заявка ID:</b> <code>{application_id}</code>\n\n"
        f"<b>Причина возврата:</b>\n{reason.capitalize()}\n\n"
        f"<i>(Для восстановления заявки используйте: /e {application_id})</i>"
    )

    for chat_id in NOTIFY_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message_for_admins, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление о возврате админу {chat_id}: {e}")
    
    await update.message.reply_text(
        f"✅ <b>Ваш запрос на возврат заявки <code>{application_id}</code> отправлен специалистам.</b>\n\n"
        f"Они рассмотрят причину и, при необходимости, вернут заявку в работу. Вы получите отдельное уведомление.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD
    )
    
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выход из любого диалога"""
    context.user_data.clear()
    
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text('Действие отменено.', reply_markup=None)
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="Вы в главном меню.",
            reply_markup=MAIN_KEYBOARD
        )
    else:
        await update.message.reply_text(
            'Действие отменено.',
            reply_markup=MAIN_KEYBOARD
        )
        
    return ConversationHandler.END

async def request_password_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: Запросить пароль (/r <id>)"""
    employee_chat_id = update.effective_chat.id
    
    if employee_chat_id not in NOTIFY_CHAT_IDS:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return

    if not context.args or len(context.args[0]) != 8:
        await update.message.reply_text("Используйте: /r <id_заявки>")
        return
        
    app_id = context.args[0]
    
    user_info = await asyncio.to_thread(db_service.get_user_info_for_app, app_id)
    
    if not user_info:
        await update.message.reply_text(f"Заявка с ID <code>{app_id}</code> не найдена.", parse_mode=ParseMode.HTML)
        return
        
    user_chat_id, user_name = user_info
    
    context.bot_data[app_id] = employee_chat_id
    
    try:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Нажмите, чтобы отправить пароль для {app_id}", callback_data=f"pwd_start:{app_id}")]
        ])
        
        await context.bot.send_message(
            chat_id=user_chat_id,
            text=(
                f"Здравствуйте, {user_name}!\n"
                f"Сотруднику техподдержки требуется пароль от ЕМИАС для работы по вашей заявке <code>{app_id}</code>.\n\n"
                f"<b>Пожалуйста, нажмите кнопку ниже, чтобы начать:</b>"
            ),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        
        await update.message.reply_text(f"Запрос пароля для <code>{app_id}</code> успешно отправлен пользователю {user_name}.", parse_mode=ParseMode.HTML)
        
    except Exception as e:
        logger.error(f"Не удалось отправить запрос пароля пользователю {user_chat_id}: {e}")
        await update.message.reply_text(f"Не удалось отправить запрос пользователю (ID: {user_chat_id}). Ошибка: {e}")
        context.bot_data.pop(app_id, None)

async def conv_password_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_chat_id = update.effective_chat.id
    
    if not context.args or len(context.args[0]) != 8:
        await update.message.reply_text("Используйте: /submit_password <id_заявки>")
        return ConversationHandler.END
        
    app_id = context.args[0]
    
    if app_id not in context.bot_data:
        await update.message.reply_text("Пароль для этой заявки не запрашивался или уже был отправлен.")
        return ConversationHandler.END
        
    user_app_ids = await asyncio.to_thread(db_service.get_app_ids_for_user, user_chat_id)
    if app_id not in user_app_ids:
        await update.message.reply_text("Ошибка: эта заявка не принадлежит вам.")
        return ConversationHandler.END
        
    context.user_data['app_id_for_password'] = app_id
    await update.message.reply_text(f"<b>Пожалуйста, введите пароль от ЕМИАС для заявки <code>{app_id}</code>:</b>", parse_mode=ParseMode.HTML)
    
    return States.PASSWORD_REQUEST_WAIT_PASSWORD

async def conv_password_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь: Нажал кнопку 'Отправить пароль'"""
    query = update.callback_query
    await query.answer()
    user_chat_id = update.effective_chat.id
    
    app_id = query.data.split(":", 1)[1]
    
    if app_id not in context.bot_data:
        await query.edit_message_text("Пароль для этой заявки не запрашивался или уже был отправлен.")
        return ConversationHandler.END
        
    user_app_ids = await asyncio.to_thread(db_service.get_app_ids_for_user, user_chat_id)
    if app_id not in user_app_ids:
        await query.edit_message_text("Ошибка: эта заявка не принадлежит вам.")
        return ConversationHandler.END
        
    context.user_data['app_id_for_password'] = app_id
    
    await query.edit_message_text(
        f"<b>Заявка <code>{app_id}</code>:</b>\nПожалуйста, введите пароль от ЕМИАС:", 
        parse_mode=ParseMode.HTML
    )
    
    return States.PASSWORD_REQUEST_WAIT_PASSWORD

async def conv_password_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь: ввел пароль"""
    password = update.message.text
    app_id = context.user_data.pop('app_id_for_password', None)
    
    if not app_id:
        return ConversationHandler.END

    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение с паролем: {e}")

    await update.message.reply_text("✅ Спасибо, пароль принят и будет немедленно доставлен сотруднику.")

    employee_chat_id = context.bot_data.pop(app_id, None)
    
    if not employee_chat_id:
        logger.error(f"Не найден chat_id сотрудника для заявки {app_id}")
        return ConversationHandler.END
        
    try:
        await context.bot.send_message(
            chat_id=employee_chat_id,
            text=(
                f"🔐 <b>Пароль для заявки <code>{app_id}</code> получен:</b>\n\n"
                f"<code>{password}</code>"
            ),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Не удалось доставить пароль сотруднику {employee_chat_id}: {e}")
        await update.message.reply_text("❌ Произошла ошибка при доставке пароля. Пожалуйста, попробуйте снова, нажав кнопку в предыдущем сообщении.")
        context.bot_data[app_id] = employee_chat_id

    return ConversationHandler.END


def main():
    application = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text("Start"), conv_ask_name),
            MessageHandler(filters.Text("Добавить фото"), conv_ask_update_id),
            MessageHandler(filters.Text("Дополнить заявку"), conv_ask_append_id),
            MessageHandler(filters.Text("Вернуть заявку в работу"), conv_ask_return_id),
            MessageHandler(filters.Text("Проверить статус заявки"), conv_ask_check_status),
        ],
        states={
            States.START_WAIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_ip)],
            States.START_WAIT_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_department)],
            States.START_WAIT_DEPARTMENT: [CallbackQueryHandler(department_callback, pattern="^dep_")],
            States.START_WAIT_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_photos)],
            States.START_WAIT_PHOTOS: [
                MessageHandler(filters.Text("✔️ Done"), conv_show_confirmation),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, conv_add_photo)
            ],
            States.START_CONFIRMATION: [
                CallbackQueryHandler(conv_done, pattern="^confirm_send$"),
                CallbackQueryHandler(cancel, pattern="^confirm_cancel$")
            ],
            
            States.UPDATE_WAIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_update_photo)],
            States.UPDATE_WAIT_PHOTO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, conv_process_update_photo_add),
                MessageHandler(filters.Text("✔️ Done"), conv_process_update_photo_done)
            ],
            
            States.APPEND_WAIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_append_text)],
            States.APPEND_WAIT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_process_append_text)],
            
            States.RETURN_WAIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_return_reason)],
            States.RETURN_WAIT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_process_return_reason)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel)
        ],
    )
    
    password_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('submit_password', conv_password_start_cmd),
            CallbackQueryHandler(conv_password_start_cb, pattern="^pwd_start:")
        ],
        states={
            States.PASSWORD_REQUEST_WAIT_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_password_receive)
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel)
        ]
    )

    application.add_handler(conv_handler)
    application.add_handler(password_conv_handler)
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', start))
    application.add_handler(CommandHandler('q', finish_command))
    application.add_handler(CommandHandler('w', whisper_command))
    application.add_handler(CommandHandler('e', restore_command))
    application.add_handler(CommandHandler('r', request_password_command))
    
    application.add_handler(CallbackQueryHandler(check_status_callback, pattern="^check_status:"))
    
    application.run_polling()


if __name__ == '__main__':
    main()