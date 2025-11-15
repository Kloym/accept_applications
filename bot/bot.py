import logging
import base64
import aiohttp
import sqlite3
import uuid
import re
from enum import Enum
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
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
# NOTIFY_CHAT_IDS = [413685741, 681519441]
NOTIFY_CHAT_IDS = [308035415]
USERNAME_TO_FIO = {
    "pasheug": "Пашков Евгений Олегович",
    "NRiskin": "Рискин Никита Дмитриевич"
}


class States(Enum):
    START_ROUTES = 0
    
    START_WAIT_NAME = 1
    START_WAIT_EMIAC = 2
    START_WAIT_IP = 3
    START_WAIT_DEPARTMENT = 4
    START_WAIT_DETAILS = 5
    START_WAIT_PHOTOS = 6
    
    UPDATE_WAIT_ID = 10
    UPDATE_WAIT_PHOTO = 11
    
    APPEND_WAIT_ID = 20
    APPEND_WAIT_TEXT = 21
    

    RETURN_WAIT_ID = 30
    RETURN_WAIT_REASON = 31


class ApiService:
    """
    Отвечает за ВСЕ запросы к Flask API.
    Принцип (DIP) - хэндлеры зависят от этого класса, а не от aiohttp.
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

    async def update_photo(self, application_id, username, photo_b64):
        data = {
            "application_id": application_id,
            "username": username,
            "photo": photo_b64
        }
        status, _ = await self._post_json('update_photo', data)
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
    """
    Отвечает за ВСЕ прямые запросы к SQLite.
    (В идеале, все эти методы тоже должны стать эндпоинтами API)
    """
    def __init__(self, db_file):
        self.db_file = db_file

    def _get_db(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

    # TODO: Все эти синхронные вызовы блокируют event loop.
    # В идеале использовать `aiosqlite` или `asyncio.to_thread`
    
    def mark_application_done(self, app_id, done_by):
        # TODO: Эту логику нужно перенести на API сервер
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
        # TODO: Эту логику нужно перенести на API сервер
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

api_client = ApiService(BASE_SERVER_URL)
db_service = DbService(DB_FILE)


MAIN_KEYBOARD = ReplyKeyboardMarkup([
        [KeyboardButton("Start")],
        [KeyboardButton("Обновить фото")],
        [KeyboardButton("Дополнить заявку")],
        [KeyboardButton("Проверить статус заявки")],
        [KeyboardButton("Вернуть заявку в работу")]
    ], resize_keyboard=True)

DONE_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton('✔️ Done')]], resize_keyboard=True)

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
    """Главное меню /start"""
    
    start_text = (
        "<b>Что умеет этот бот?</b>\n\n"
        "Этот бот предназначен для быстрой и удобной подачи заявок в техническую поддержку КИС ЕМИАС в нашей организации.\n\n"
        "<b>Возможности:</b>\n"
        "📋 Оформление новой заявки с указанием ФИО, отделения, IP-адреса и описанием проблемы.\n"
        "📸 Прикрепление фотографий или скриншотов к заявке.\n"
        "📝 Возможность дополнить уже отправленную заявку дополнительной информацией.\n"
        "🔄 Обновление фотографии по уже созданной заявке.\n"
        "📊 Получение уникального идентификатора для отслеживания статуса заявки.\n"
        "✅ Уведомление о выполнении вашей заявки.\n"
        "♻️ <b>NEW:</b> Возможность отправить запрос на возврат заявки в работу, если проблема не решена.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Нажмите кнопку <b>Start</b> для создания новой заявки.\n"
        "2. Следуйте инструкциям бота: введите ФИО, пароль от ЕМИАС, IP-адрес, выберите отделение и опишите проблему.\n"
        "3. При необходимости прикрепите фото.\n"
        "4. После отправки заявки вы получите уникальный ID, по которому сможете обновлять фото или дополнять заявку. При нажатии на ID вы удобно можете его скопировать.\n"
        "5. Для обновления фото или дополнения заявки используйте соответствующие кнопки в меню.\n"
        "6. Если заявку закрыли, а проблема осталась, нажмите 'Вернуть заявку в работу'."
    )
    
    await update.message.reply_text(
        text=start_text,
        reply_markup=MAIN_KEYBOARD,
        parse_mode='HTML'
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

    result = db_service.mark_application_done(app_id, done_by)
    
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
        await update.message.reply_text(f"Заявка {app_id} отмечена как выполненная.")
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления о выполнении: {e}")
        await update.message.reply_text("Ошибка при отправке уведомления.")

async def whisper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Используйте: /w <id> <сообщение>")
        return
    
    app_id = args[0]
    message_text = " ".join(args[1:])
    
    result = db_service.get_app_for_whisper(app_id)
    
    if not result:
        await update.message.reply_text(f"Заявка {app_id} не найдена.")
        return
    
    chat_id, name = result
    sender = update.effective_user.username or "Сотрудник"
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Сообщение по вашей заявке <code>{app_id}</code> от @{sender}:\n\nРешение: {message_text}",
            parse_mode='HTML'
        )
        await update.message.reply_text("Сообщение отправлено.")
    except Exception as e:
        logger.error(f"Ошибка отправки /w: {e}")
        await update.message.reply_text("Ошибка при отправке сообщения.")

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: Восстановить заявку (/e) - ТЕПЕРЬ ЧЕРЕЗ API"""
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
            parse_mode='HTML'
        )
        await update.message.reply_text(f'Заявка {app_id} возвращена в работу и пользователь уведомлён.')
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления о восстановлении: {e}")
        await update.message.reply_text("Ошибка при отправке уведомления пользователю.")


async def check_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажатие кнопки 'Проверить статус'"""
    query = update.callback_query
    data = query.data
    await query.answer()

    if data.startswith("check_status:"):
        app_id = data.split(":", 1)[1]
        
        result = db_service.get_app_status_for_user(app_id, query.from_user.id)
        
        if not result:
            await query.answer("Заявка не найдена.", show_alert=True)
            return

        status, details = result
        if status == 'done':
            await query.edit_message_text(
                f"Заявка <code>{app_id}</code> уже выполнена.", parse_mode='HTML'
            )
        else:
            await query.edit_message_text(
                f"Заявка <code>{app_id}</code>\n"
                f"Описание: {details}\n"
                f"Статус: В работе",
                parse_mode='HTML'
            )
        
        await conv_ask_check_status(update, context, from_callback=True)

async def department_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажатие кнопок в меню выбора Отделения"""
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
        return States.START_WAIT_DEPARTMENT

    if data.startswith("dep_idx:"):
        dep_index = int(data.split(":", 1)[1])
        department = departments[dep_index]
        context.user_data['department'] = department
        await query.edit_message_text(f"Вы выбрали: {department}")
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="<b>✍️ Теперь опишите проблему:</b>",
            parse_mode='HTML'
        )
        await query.answer()
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
    await update.message.reply_text('<b>👋 Введите ФИО:</b>', parse_mode='HTML')
    return States.START_WAIT_NAME

async def conv_ask_emiac(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили ФИО, спрашиваем пароль ЕМИАС"""
    fio = update.message.text.strip()
    if not is_valid_fio(fio):
        await update.message.reply_text(
            "Пожалуйста, введите ФИО полностью (например: Сергеев Алексей Андреевич)."
        )
        return States.START_WAIT_NAME
    
    context.user_data['name'] = fio
    await update.message.reply_text('<b>🔑 Теперь введите пароль от ЕМИАС:</b>', parse_mode='HTML')
    return States.START_WAIT_EMIAC

async def conv_ask_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили пароль, спрашиваем IP"""
    context.user_data['emiac_password'] = update.message.text.strip()
    await update.message.reply_text('<b>🌐 Теперь введите IP адрес компьютера:</b>', parse_mode='HTML')
    return States.START_WAIT_IP

async def conv_ask_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили IP, показываем меню отделений"""
    ip_address = update.message.text.strip()
    if not is_valid_ip(ip_address):
        await update.message.reply_text(
            "<b>❌ Неверный формат IP.</b>\nПожалуйста, введите IP-адрес в формате 123.123.123.123",
             parse_mode='HTML'
        )
        return States.START_WAIT_IP

    context.user_data['ip_address'] = ip_address
    context.user_data['dep_page'] = 0
    await update.message.reply_text(
        "<b>🗂️ Выберите отделение:</b>",
        reply_markup=get_departments_inline_keyboard(0),
        parse_mode='HTML'
    )
    return States.START_WAIT_DEPARTMENT

async def conv_ask_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили Описание, просим фото"""
    context.user_data['details'] = update.message.text.strip()
    context.user_data['photos'] = []
    await update.message.reply_text(
        "<b>🖼️ Если хотите добавить скриншот(фото) ошибки, отправьте их сейчас. Когда всё готово, нажмите Done.</b>",
        reply_markup=DONE_KEYBOARD,
        parse_mode='HTML'
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
        await update.message.reply_text("Фото добавлено. Можете отправить ещё или нажмите 'Done'.")
    
    except Exception as e:
        logger.error(f"Ошибка при обработке фото: {e}")
        await update.message.reply_text("Не удалось обработать файл. Попробуйте еще раз.")
        
    return States.START_WAIT_PHOTOS

async def conv_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка 'Done', отправляем заявку"""
    application_id = str(uuid.uuid4())[:8]
    data = {
        'name': context.user_data.get('name'),
        'ip': context.user_data.get('ip_address'),
        'emiac': context.user_data.get('emiac_password'),
        'department': context.user_data.get('department'),
        'details': context.user_data.get('details'),
        'photos': context.user_data.get('photos', []),
        'chat_id': update.effective_user.id,
        'username': update.effective_user.username or "",
        'application_id': application_id,
        'status': 'active'
    }
    
    success = await api_client.post_new_application(data)

    if not success:
        await update.message.reply_text("Ошибка при отправке заявки на сервер. Попробуйте позже.", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear()
        return ConversationHandler.END

    final_text = (
        f"<b>✅ Ваша заявка принята в работу!</b>\n"
        f"Уникальный идентификатор заявки: <code>{application_id}</code>\n\n"
        "Чтобы подать новую заявку, нажмите Start.\n"
        "Для обновления скриншота(фото) по заявке — Обновить фото.\n"
        "Для дополнения текста — Дополнить заявку.\n"
        "Для проверки статуса — Проверьте статус заявки.\n"
        "Для возврата заявки - вернуть заявку в работу."
    )
    await update.message.reply_text(
        final_text,
        parse_mode='HTML',
        reply_markup=MAIN_KEYBOARD
    )
    for notify_id in NOTIFY_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=notify_id, text='Вам поступила новая заявка')
        except Exception as e:
            logger.warning(f"Ошибка отправки уведомления админу {notify_id}: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def conv_ask_update_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа: Нажата кнопка 'Обновить фото'"""
    context.user_data.clear()
    await update.message.reply_text(
        '<b>🔄 Обновление фото</b>\nВведите уникальный идентификатор заявки:',
        parse_mode='HTML'
    )
    return States.UPDATE_WAIT_ID

async def conv_ask_update_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили ID, просим фото"""
    context.user_data['update_id'] = update.message.text.strip().lower()
    context.user_data['update_photos'] = [] 
    await update.message.reply_text(
        '📸 <b>Отправьте новое фото для заявки (можно как файл или как фото).</b>\n'
        'Вы можете отправить несколько. Когда закончите, нажмите Done.',
        reply_markup=DONE_KEYBOARD,
        parse_mode='HTML'
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
        await update.message.reply_text("Фото добавлено. Можете отправить ещё или нажмите 'Done'.")

    except Exception as e:
        logger.error(f"Ошибка при добавлении фото для обновления: {e}")
        await update.message.reply_text("Ошибка при обработке фото. Попробуйте еще раз.")
        
    return States.UPDATE_WAIT_PHOTO


async def conv_process_update_photo_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка 'Done' при обновлении фото. Отправляем все фото из списка."""
    application_id = context.user_data.get("update_id")
    username = update.effective_user.username
    photos_b64_list = context.user_data.get('update_photos', [])
    
    if not photos_b64_list:
        await update.message.reply_text("Вы не отправили ни одного фото. Действие отменено.", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(f"Начинаю загрузку {len(photos_b64_list)} фото...")

    success_count = 0
    fail_count = 0
    
    for photo_b64 in photos_b64_list:
        try:
            success = await api_client.update_photo(application_id, username, photo_b64)
            if success:
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            logger.error(f"Ошибка при отправке фото {application_id}: {e}")
            fail_count += 1
            
    if success_count > 0 and fail_count == 0:
        await update.message.reply_text(f"✅ Все {success_count} фото для заявки <code>{application_id}</code> успешно обновлены!", reply_markup=MAIN_KEYBOARD, parse_mode='HTML')
    elif success_count > 0 and fail_count > 0:
        await update.message.reply_text(f"⚠️ Частично выполнено: {success_count} фото обновлено, {fail_count} не удалось отправить.", reply_markup=MAIN_KEYBOARD)
    elif success_count == 0 and fail_count > 0:
         await update.message.reply_text(f"❌ Ошибка: не удалось обновить фото. Заявка <code>{application_id}</code> не найдена или не принадлежит вам.", reply_markup=MAIN_KEYBOARD, parse_mode='HTML')
    else:
        pass

    context.user_data.clear()
    return ConversationHandler.END

async def conv_ask_append_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа: Нажата кнопка 'Дополнить заявку'"""
    context.user_data.clear()
    await update.message.reply_text(
        '<b>📝 Дополнение заявки</b>\nВведите уникальный идентификатор заявки:',
        parse_mode='HTML'
    )
    return States.APPEND_WAIT_ID

async def conv_ask_append_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили ID, просим текст"""
    context.user_data['append_id'] = update.message.text.strip().lower()
    await update.message.reply_text(
        '✍️ <b>Введите дополнительный текст для заявки:</b>',
        parse_mode='HTML'
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
            '<b>✅ Текст успешно добавлен к заявке!</b>',
            parse_mode='HTML',
            reply_markup=MAIN_KEYBOARD
        )
    else:
        await update.message.reply_text(
            '<b>❌ Ошибка: заявка не найдена или не принадлежит вам.</b>',
            parse_mode='HTML',
            reply_markup=MAIN_KEYBOARD
        )
        
    context.user_data.clear()
    return ConversationHandler.END


async def conv_ask_check_status(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback=False):
    """Точка входа: Нажата кнопка 'Проверить статус заявки'"""
    
    rows = db_service.get_active_apps_by_chat_id(update.effective_user.id)
    
    if not rows:
        await update.message.reply_text("У вас нет активных заявок.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(
            f"{row['application_id']} | {row['details']}",
            callback_data=f"check_status:{row['application_id']}"
        )] for row in rows
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if from_callback:
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="Выберите заявку для проверки статуса:",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "Выберите заявку для проверки статуса:",
            reply_markup=reply_markup
        )
    
    return ConversationHandler.END



async def conv_ask_return_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа: Нажата кнопка 'Вернуть заявку в работу'"""
    context.user_data.clear()
    
    rows = db_service.get_done_apps_by_chat_id(update.effective_user.id, limit=5)
    
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

    message_text += '<b>Введите ID заявки</b> (8 символов), которую нужно вернуть в работу:'
    
    await update.message.reply_text(message_text, parse_mode='HTML')
    return States.RETURN_WAIT_ID

async def conv_ask_return_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получили ID, просим причину"""
    application_id = update.message.text.strip().lower()
    if len(application_id) != 8:
         await update.message.reply_text(
            '<b>❌ Неверный формат ID.</b>\n'
            'ID должен состоять ровно из 8 символов. Попробуйте еще раз:',
            parse_mode='HTML'
        )
         return States.RETURN_WAIT_ID
             
    context.user_data['return_id'] = application_id
    await update.message.reply_text(
        '📝 <b>Опишите причину</b>\n'
        'Что вам не понравилось в выполненной заявке или какая проблема осталась?',
        parse_mode='HTML'
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
        f"<b>Причина возврата:</b>\n{reason}\n\n"
        f"<i>(Для восстановления заявки используйте: /e {application_id})</i>"
    )

    for chat_id in NOTIFY_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message_for_admins, parse_mode='HTML')
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление о возврате админу {chat_id}: {e}")
    
    await update.message.reply_text(
        f"✅ <b>Ваш запрос на возврат заявки <code>{application_id}</code> отправлен специалистам.</b>\n\n"
        f"Они рассмотрят причину и, при необходимости, вернут заявку в работу. Вы получите отдельное уведомление.",
        parse_mode='HTML',
        reply_markup=MAIN_KEYBOARD
    )
    
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """НОВАЯ ФУНКЦИЯ: Выход из любого диалога"""
    context.user_data.clear()
    await update.message.reply_text(
        'Действие отменено.',
        reply_markup=MAIN_KEYBOARD
    )
    return ConversationHandler.END



def main():
    application = Application.builder().token(TOKEN).build()
    
    
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text("Start"), conv_ask_name),
            MessageHandler(filters.Text("Обновить фото"), conv_ask_update_id),
            MessageHandler(filters.Text("Дополнить заявку"), conv_ask_append_id),
            MessageHandler(filters.Text("Вернуть заявку в работу"), conv_ask_return_id),
            MessageHandler(filters.Text("Проверить статус заявки"), conv_ask_check_status),
        ],
        states={
            States.START_WAIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_emiac)],
            States.START_WAIT_EMIAC: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_ip)],
            States.START_WAIT_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_department)],
            States.START_WAIT_DEPARTMENT: [CallbackQueryHandler(department_callback)], # Ловим нажатия кнопок
            States.START_WAIT_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_photos)],
            States.START_WAIT_PHOTOS: [
                MessageHandler(filters.Text("✔️ Done"), conv_done),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, conv_add_photo)
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
    
    application.add_handler(conv_handler)
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('q', finish_command))
    application.add_handler(CommandHandler('w', whisper_command))
    application.add_handler(CommandHandler('e', restore_command))
    
    application.add_handler(CallbackQueryHandler(check_status_callback, pattern="^check_status:"))
    
    application.run_polling()


if __name__ == '__main__':
    main()