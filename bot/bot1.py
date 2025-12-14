import logging
import base64
import aiohttp
import sqlite3
import uuid
import re
import asyncio
from enum import Enum
from datetime import datetime
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
    PicklePersistence,
)
from dotenv import load_dotenv
import os
from op import DEPARTMENTS, DEPARTMENTS_PER_PAGE
import io
import matplotlib.pyplot as plt
from collections import Counter
import matplotlib
matplotlib.use('Agg')
from concurrent.futures import ProcessPoolExecutor


process_pool = None
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._application").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

API_TOKEN = os.getenv("API_TOKEN", "hospital_secret_2025")
TOKEN = os.getenv("TOKEN")
BASE_SERVER_URL = "http://127.0.0.1:5000"
DB_FILE = "applications.db"
DEPARTMENTS = sorted(DEPARTMENTS)
raw_admin_ids = os.getenv("ADMIN_IDS", "")
if raw_admin_ids:
    NOTIFY_CHAT_IDS = [int(x.strip()) for x in raw_admin_ids.split(",") if x.strip().isdigit()]
else:
    NOTIFY_CHAT_IDS = []
    logger.warning("⚠️ В .env не указаны ADMIN_IDS!")

raw_boss_ids = os.getenv("BOSS_IDS", "")
if raw_boss_ids:
    BOSS_CHAT_IDS = [int(x.strip()) for x in raw_boss_ids.split(",") if x.strip().isdigit()]
else:
    BOSS_CHAT_IDS = []

ALL_PERMITTED_IDS = set(NOTIFY_CHAT_IDS + BOSS_CHAT_IDS)

USERNAME_TO_FIO = {
    "pasheug": "Пашков Евгений Олегович",
    "NRiskin": "Рискин Никита Дмитриевич",
    "kloym": "Сергеев Алексей Андреевич",
}

def generate_time_plot_sync(hours: list, counts: list) -> io.BytesIO:
    """Рисует график активности по часам."""
    plt.figure(figsize=(10, 6))
    plt.plot(hours, counts, marker='o', color='#3b82f6', linewidth=2, label='Заявки')
    plt.fill_between(hours, counts, color='#3b82f6', alpha=0.3)
    
    plt.title('Активность подачи заявок по времени суток')
    plt.xlabel('Часы (00-23)')
    plt.ylabel('Количество заявок')
    plt.grid(True, linestyle='--', alpha=0.6)
    plt.xticks(hours)
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    buf.seek(0)
    plt.close()
    return buf

def generate_rating_plot_sync(labels: list, sizes: list) -> io.BytesIO:
    """Рисует круговую диаграмму (Pie Chart)."""
    plt.figure(figsize=(8, 8))
    colors_map = {
        '5': '#22c55e', '4': '#3b82f6', '3': '#eab308', 
        '2': '#f97316', '1': '#ef4444'
    }
    colors = [colors_map.get(str(l), '#94a3b8') for l in labels]

    plt.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=140, colors=colors, 
            textprops={'fontsize': 12, 'fontweight': 'bold'})
    
    plt.title('Распределение оценок пользователей')
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    buf.seek(0)
    plt.close()
    return buf


def generate_plot_sync(labels: list, values: list) -> io.BytesIO:
    """
    Эта функция выполняется в отдельном процессе.
    Она берет списки данных и возвращает байты картинки.
    """
    plt.figure(figsize=(10, 6))

    bars = plt.barh(labels, values, color='#3b82f6', height=0.6)
    
    plt.xlabel('Количество заявок')
    plt.title('Топ загруженных отделений')
    plt.grid(axis='x', linestyle='--', alpha=0.5)
    plt.tight_layout()

    for bar in bars:
        width = bar.get_width()
        plt.text(width + 0.1, bar.get_y() + bar.get_height()/2, 
                 f'{int(width)}', ha='left', va='center', fontweight='bold')

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    buf.seek(0)

    plt.close()
    
    return buf

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
    REPLY_WAIT_TEXT = 60
    EDIT_CHOICE = 100
    EDIT_WAIT_NAME = 101
    EDIT_WAIT_IP = 102
    EDIT_WAIT_DETAILS = 103


class ApiService:
    def __init__(self, base_url):
        self.base_url = base_url

    async def _post_json(self, endpoint, data):
        url = f"{self.base_url}/{endpoint}"
        headers = {"Authorization": f"Bearer hospital_secret_2025"} 
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=data, headers=headers) as response:
                    return response.status, await response.json()
            except aiohttp.ClientError as e:
                logger.error(f"API ClientError: {e}")
                return 500, {"error": str(e)}
            except Exception as e:
                logger.error(f"API Generic Error: {e}")
                return 500, {"error": str(e)}
            
    async def assign_application(self, application_id, admin_name):
        data = {
            "application_id": application_id,
            "admin_name": admin_name
        }
        status, response_json = await self._post_json("api/assign", data)
        
        if status == 200:
            return "success", response_json 
        elif status == 409: 
            return "already_taken", response_json
        else:
            return "error", None

    async def post_new_application(self, data):
        status, _ = await self._post_json("applications", data)
        return status == 201

    async def update_photos(self, application_id, username, attachments_list):
        data = {
            "application_id": application_id,
            "username": username,
            "attachments": attachments_list,
        }
        status, _ = await self._post_json("update_photos", data)
        return status == 200

    async def append_details(self, application_id, username, extra_text):
        data = {
            "application_id": application_id,
            "username": username,
            "extra_text": extra_text,
        }
        status, _ = await self._post_json("append_details", data)
        return status == 200

    async def send_rating(self, application_id, rating):
        data = {
            "application_id": application_id,
            "rating": int(rating)
        }
        status, _ = await self._post_json("rate_application", data)
        return status == 200

    async def restore_application(self, application_id):
        status, data = await self._post_json(f"restore/{application_id}", {})
        if status == 200:
            return data.get("chat_id"), data.get("name")
        else:
            return None

    async def add_message(self, application_id, sender, text):
        data = {"application_id": application_id, "sender": sender, "text": text}
        status, _ = await self._post_json("api/add_message", data)
        return status == 200


class DbService:
    def __init__(self, db_file):
        self.db_file = db_file

    def _get_db(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

    def mark_application_done(self, app_id, done_by, manual_solution=None):
        conn = self._get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT solution FROM applications LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE applications ADD COLUMN solution TEXT")
            conn.commit()

        cursor.execute(
            "SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,)
        )
        row = cursor.fetchone()

        if not row:
            conn.close()
            return None

        chat_id, name = row["chat_id"], row["name"]
        archived_at = datetime.now().strftime("%Y-%m-%d %H:%M")

        if manual_solution:
            query = """
                UPDATE applications 
                SET status = 'done', archived_at = ?, done_by = ?, solution = ? 
                WHERE application_id = ?
            """
            params = (archived_at, done_by, manual_solution, app_id)
            cursor.execute(query, params)
        else:
            query = """
                UPDATE applications 
                SET status = 'done', 
                    archived_at = ?, 
                    done_by = ?,
                    solution = (
                        SELECT message_text FROM messages 
                        WHERE application_id = ? 
                        AND message_text NOT LIKE '%[ОТВЕТ ПОЛЬЗОВАТЕЛЯ]%'
                        ORDER BY id DESC LIMIT 1
                    )
                WHERE application_id = ?
            """
            params = (archived_at, done_by, app_id, app_id)
            cursor.execute(query, params)

        conn.commit()
        conn.close()
        return chat_id, name

    def get_last_user_data(self, chat_id):
        """Получает данные последней заявки пользователя для автозаполнения."""
        conn = self._get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT name, ip, department FROM applications WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1",
                (chat_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                return {
                    "name": row["name"],
                    "ip": row["ip"],
                    "department": row["department"]
                }
            return None
        except Exception as e:
            logger.error(f"Ошибка получения данных пользователя: {e}")
            conn.close()
            return None
        
    def get_statistics_data(self):
        """Собирает данные для отчета."""
        conn = self._get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT status, department, archived_at, created_at FROM applications")
            rows = cursor.fetchall()
        except Exception as e:
            logger.error(f"Ошибка чтения статистики: {e}")
            rows = []
        
        conn.close()
        return rows

    def get_app_for_whisper(self, app_id):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return (row["chat_id"], row["name"]) if row else None

    def get_all_chat_ids(self):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT chat_id FROM applications")
        rows = cursor.fetchall()
        conn.close()
        return [row["chat_id"] for row in rows]

    def get_done_apps_by_chat_id(self, chat_id, limit=5):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT application_id, details FROM applications WHERE chat_id=? AND status='done' ORDER BY archived_at DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_active_apps_by_chat_id(self, chat_id):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT application_id, department, details, status FROM applications WHERE chat_id=? AND status='active'",
            (chat_id,),
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_app_status_for_user(self, app_id, chat_id):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, details, difficulty, name, department FROM applications WHERE application_id=? AND chat_id=?",
            (app_id, chat_id),
        )
        row = cursor.fetchone()
        conn.close()
        return (
            (
                row["status"],
                row["details"],
                row["difficulty"],
                row["name"],
                row["department"],
            )
            if row
            else None
        )

    def get_user_info_for_app(self, app_id: str):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return (row["chat_id"], row["name"]) if row else None

    def get_app_ids_for_user(self, chat_id: int) -> list:
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT application_id FROM applications WHERE chat_id = ?", (chat_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [row["application_id"] for row in rows]
    
    def check_and_add_assignee_column(self):
        """Безопасно добавляет колонку assignee, если её нет."""
        conn = self._get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT assignee FROM applications LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE applications ADD COLUMN assignee TEXT")
            conn.commit()
        conn.close()

    def set_assignee(self, app_id, admin_name):
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE applications 
            SET assignee = ? 
            WHERE application_id = ? AND (assignee IS NULL OR assignee = '')
            """, 
            (admin_name, app_id)
        )
        if cursor.rowcount > 0:
            conn.commit()
            cursor.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,))
            row = cursor.fetchone()
            conn.close()
            return "success", row["chat_id"], row["name"], None
        else:
            cursor.execute("SELECT assignee FROM applications WHERE application_id = ?", (app_id,))
            row = cursor.fetchone()
            conn.close()
            
            if row and row["assignee"]:
                return "already_taken", None, None, row["assignee"]
            else:
                return "not_found", None, None, None

    def get_time_stats(self):
        """Данные для графика по часам."""
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT strftime('%H', created_at) as h, COUNT(*) FROM applications WHERE created_at IS NOT NULL GROUP BY h ORDER BY h")
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_rating_stats(self):
        """Данные для пирога с оценками."""
        conn = self._get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT rating, COUNT(*) FROM applications WHERE rating IS NOT NULL AND rating > 0 GROUP BY rating")
        rows = cursor.fetchall()
        conn.close()
        return rows


api_client = ApiService(BASE_SERVER_URL)
db_service = DbService(DB_FILE)

BTN_CANCEL = "❌ Отменить действие"
BTN_BACK = "⬅️ Вернуться к предыдущему шагу"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📝 Новая заявка"), KeyboardButton("🔄 Повторить последнюю заявку")],
        [
            KeyboardButton("Проверить статус заявки"),
            KeyboardButton("Вернуть заявку в работу"),
            KeyboardButton("Дополнить заявку"),
        ],
        [KeyboardButton("Добавить фото"), KeyboardButton("📚 Инструкция")],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_CANCEL)]], resize_keyboard=True, is_persistent=True
)

DONE_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("✔️ Готово"), KeyboardButton("❌ Отменить все")]],
    resize_keyboard=True,
    is_persistent=True,
)

CONFIRM_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Отправить", callback_data="confirm_send"),
            InlineKeyboardButton("❌ Отменить", callback_data="confirm_cancel"),
        ]
    ]
)


def is_valid_fio(fio):
    parts = fio.split()
    return len(parts) >= 2 and all(len(part) >= 2 for part in parts)


def is_valid_ip(ip):
    return re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip)


def filter_departments_by_letter(departments, letter):
    return [dep for dep in departments if dep.upper().startswith(letter.upper())]


def get_departments_inline_keyboard(page=0, filter_letter=None, saved_dep=None):
    keyboard = []

    if saved_dep:
        # Добавляем эмодзи звезды для заметности
        btn_text = f"⭐ {saved_dep}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data="use_saved_dep")])

    if filter_letter:
        depatments = filter_departments_by_letter(DEPARTMENTS, filter_letter)
    else:
        depatments = DEPARTMENTS

    start = page * DEPARTMENTS_PER_PAGE
    end = start + DEPARTMENTS_PER_PAGE
    departments_page = depatments[start:end]

    keyboard.extend(
        [
            [InlineKeyboardButton(dep, callback_data=f"dep_idx:{i}")]
            for i, dep in enumerate(departments_page, start=start)
        ]
    )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"dep_page:{page-1}"))
    if end < len(DEPARTMENTS):
        nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"dep_page:{page+1}"))
    if nav:
        keyboard.append(nav)

    used_letters = sorted(set(dep[0].upper() for dep in DEPARTMENTS))
    letters_per_row = 7
    letter_buttons = [
        InlineKeyboardButton(letter, callback_data=f"dep_letter:{letter}")
        for letter in used_letters
    ]
    letter_rows = [
        letter_buttons[i : i + letters_per_row]
        for i in range(0, len(letter_buttons), letters_per_row)
    ]
    keyboard.extend(letter_rows)
    keyboard.append([InlineKeyboardButton("Все", callback_data="dep_letter:all")])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])

    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_text = (
        "<b>Что умеет этот бот?</b>\n\n"
        "Этот бот предназначен для быстрой и удобной подачи заявок в техническую поддержку КИС ЕМИАС.\n\n"
        "<b>Возможности:</b>\n"
        "📋 <b>Старт</b> - Оформление новой заявки.\n"
        "📸 <b>Добавить фото</b> - Добавление фотографий к существующей заявке.\n"
        "📝 <b>Дополнить заявку</b> - Добавление текста к существующей заявке.\n"
        "📊 <b>Проверить статус заявки</b> - Просмотр статуса ваших активных заявок.\n"
        "✅ Уведомление о выполнении вашей заявки.\n"
        "♻️ <b>Вернуть заявку в работу</b> - Если проблема не решена.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Нажмите кнопку <b>Новая заявка</b> для создания новой заявки.\n"
        "2. Следуйте инструкциям бота: введите ФИО, IP-адрес, выберите отделение и опишите проблему.\n"
        "3. При необходимости прикрепите фото.\n"
        "4. После отправки заявки вы получите уникальный ID. При нажатии на ID вы удобно можете его скопировать.\n\n"
        "Вы можете отменить любое действие в любой момент, отправив команду /cancel или нажав кнопку Отменить.\n\n"
        "Если где-то при заполнении заявки возникает ошибка пропишите в чат /start"
    )
    await update.message.reply_text(
        start_text, reply_markup=MAIN_KEYBOARD, parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END


async def send_instruction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filename = "manual.docx"

    current_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(current_dir, filename)

    if not os.path.exists(file_path):
        await update.message.reply_text("Файл инструкции временно недоступен.")
        logger.error(f"Файл не найден по пути: {file_path}")
        return ConversationHandler.END

    try:
        await update.message.reply_document(
            document=open(file_path, "rb"),
            caption="📄 <b>Инструкция по использованию бота</b>",
            parse_mode=ParseMode.HTML,
            filename="Инструкция_по_использованию_бота.docx",
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке инструкции: {e}")
        await update.message.reply_text("Произошла ошибка при отправке файла.")

    return ConversationHandler.END


async def finish_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args[0]) != 8:
        await update.message.reply_text(
            "Укажите id заявки, например: /q 1234abcd [решение]"
        )
        return

    app_id = args[0]
    additional_text = " ".join(args[1:]) if len(args) > 1 else None

    file_id = None
    file_type = None
    reply_message = update.message.reply_to_message

    if reply_message:
        if reply_message.photo:
            file_id = reply_message.photo[-1].file_id
            file_type = "photo"
        elif (
            reply_message.document
            and reply_message.document.mime_type
            and reply_message.document.mime_type.startswith("image/")
        ):
            file_id = reply_message.document.file_id
            file_type = "document"

    username = update.effective_user.username
    done_by = USERNAME_TO_FIO.get(username, username)

    db_solution = additional_text
    if not db_solution and file_id:
        db_solution = "[Отправлено фото решения]"

    result = await asyncio.to_thread(
        db_service.mark_application_done, app_id, done_by, db_solution
    )

    if not result:
        await update.message.reply_text(f"Заявка {app_id} не найдена")
        return

    chat_id, name = result

    msg_user = f"{name.title()}, ваша заявка <code>{app_id}</code> выполнена!"
    if additional_text:
        msg_user += f"\n\n<b>Дополнительное сообщение:</b>\n{additional_text.capitalize()}"

    rating_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ 1", callback_data=f"rate:{app_id}:1"),
            InlineKeyboardButton("⭐ 2", callback_data=f"rate:{app_id}:2"),
            InlineKeyboardButton("⭐ 3", callback_data=f"rate:{app_id}:3"),
            InlineKeyboardButton("⭐ 4", callback_data=f"rate:{app_id}:4"),
            InlineKeyboardButton("⭐ 5", callback_data=f"rate:{app_id}:5"),
        ]
    ])
    
    confirm_msg = ""

    try:
        if file_type == "photo":
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=msg_user + "\n\n👇 <b>Оцените качество работы:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=rating_keyboard
            )
            confirm_msg = f"Заявка {app_id} закрыта (фото отправлено + запрос оценки)."

        elif file_type == "document":
            await context.bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=msg_user + "\n\n👇 <b>Оцените качество работы:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=rating_keyboard
            )
            confirm_msg = f"Заявка {app_id} закрыта"

        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg_user + "\n\n👇 <b>Оцените качество работы:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=rating_keyboard
            )
            confirm_msg = f"Заявка {app_id} закрыта."
        
        await update.message.reply_text(confirm_msg)

    except Exception as e:
        logger.error(f"Ошибка finish_command: {e}")
        await update.message.reply_text("Заявка закрыта, но возникла ошибка при отправке уведомления пользователю.")

async def whisper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args) < 1:
        await update.message.reply_text(
            "Используйте: /w <id> <сообщение> (или ответом на фото)"
        )
        return
    app_id = args[0]
    message_text = " ".join(args[1:]) if len(args) > 1 else ""

    file_id = None
    file_type = None
    reply_message = update.message.reply_to_message
    if reply_message:
        if reply_message.photo:
            file_id = reply_message.photo[-1].file_id
            file_type = "photo"
        elif (
            reply_message.document
            and reply_message.document.mime_type
            and reply_message.document.mime_type.startswith("image/")
        ):
            file_id = reply_message.document.file_id
            file_type = "document"

    result = await asyncio.to_thread(db_service.get_app_for_whisper, app_id)
    if not result:
        await update.message.reply_text(f"Заявка {app_id} не найдена.")
        return

    chat_id, name = result
    sender = update.effective_user.username or "Сотрудник"
    solution_text = (
        f"\n\n<b>Решение:</b>\n{message_text.capitalize()}" if message_text else ""
    )
    full_caption = (
        f"🔔 Сообщение по заявке <code>{app_id}</code> от @{sender}:{solution_text}"
    )

    reply_markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✍️ Ответить сотруднику", callback_data=f"reply_admin:{app_id}"
                )
            ]
        ]
    )

    try:
        if file_type == "photo":
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=full_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            await api_client.add_message(
                app_id, sender, f"[Фото отправлено: {message_text}]"
            )
        elif file_type == "document":
            await context.bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=full_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            await api_client.add_message(
                app_id, sender, f"[Документ отправлен: {message_text}]"
            )
        elif message_text:
            await context.bot.send_message(
                chat_id=chat_id,
                text=full_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            await api_client.add_message(app_id, sender, message_text)
        else:
            await update.message.reply_text("Нечего отправлять.")
            return
        await update.message.reply_text("Сообщение отправлено.")
    except Exception as e:
        logger.error(f"Ошибка отправки /w: {e}")
        await update.message.reply_text(f"Ошибка при отправке: {e}")


async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args[0]) != 8:
        await update.message.reply_text("Укажите id: /e 1234abcd")
        return
    app_id = args[0]
    result = await api_client.restore_application(app_id)
    if not result:
        await update.message.reply_text(f"Заявка {app_id} не найдена.")
        return
    chat_id, name = result
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{name.title()}, ваша заявка <code>{app_id}</code> возвращена в работу!",
            parse_mode=ParseMode.HTML,
        )
        await update.message.reply_text(f"Заявка {app_id} восстановлена.")
    except Exception:
        await update.message.reply_text("Ошибка при уведомлении.")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ALL_PERMITTED_IDS:
        await update.message.reply_text("⛔ У вас нет прав для просмотра статистики.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Введите текст рассылки.\nПример: <code>/b Внимание! Работы.</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    message_to_send = update.message.text.split(None, 1)[1]

    chat_ids = await asyncio.to_thread(db_service.get_all_chat_ids)

    if not chat_ids:
        await update.message.reply_text("В базе нет пользователей для рассылки.")
        return

    await update.message.reply_text(
        f"🚀 Начинаю рассылку для {len(chat_ids)} пользователей..."
    )

    success_count = 0
    block_count = 0
    error_count = 0

    for chat_id in chat_ids:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📢 <b>ОБЪЯВЛЕНИЕ</b>\n\n{message_to_send}",
                parse_mode=ParseMode.HTML,
            )
            success_count += 1
            await asyncio.sleep(0.05)

        except Forbidden:
            block_count += 1
        except Exception as e:
            logger.error(f"Ошибка рассылки для {chat_id}: {e}")
            error_count += 1

    await update.message.reply_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"Успешно: {success_count}\n"
        f"Заблокировали бота: {block_count}\n"
        f"Ошибки: {error_count}",
        parse_mode=ParseMode.HTML,
    )


async def conv_back_to_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к вводу имени."""
    saved_name = context.user_data.get("saved_name")

    text = '<b>👋 Введите ФИО:</b>\n\n(Или нажмите кнопку "Отменить действие")'
    buttons = [[KeyboardButton(BTN_CANCEL)]]

    if saved_name:
        text += f"\n\nРанее вы вводили: <b>{saved_name}</b>"
        buttons.insert(0, [KeyboardButton(saved_name)])

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(
            buttons, resize_keyboard=True, one_time_keyboard=True
        ),
    )
    return States.START_WAIT_NAME


async def conv_back_to_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к вводу IP."""
    saved_ip = context.user_data.get("saved_ip")
    text = "<b>🌐 Введите IP адрес компьютера:</b>"

    buttons = [[KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)]]

    if saved_ip:
        text += f"\n\nРанее вы вводили: <b>{saved_ip}</b>"
        buttons.insert(0, [KeyboardButton(saved_ip)])

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(
            buttons, resize_keyboard=True, one_time_keyboard=True
        ),
    )
    return States.START_WAIT_IP


async def conv_back_to_department_bridge(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Возврат к кнопке выбора отделения."""
    bridge_keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("📂 Выбрать отделение")],
            [KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Вернулись к выбору отделения.\nНажмите кнопку ниже, чтобы открыть список.",
        reply_markup=bridge_keyboard,
    )
    return States.START_WAIT_DEPARTMENT


async def conv_back_to_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к вводу описания проблемы."""
    prev_details = context.user_data.get("details", "")
    text = "<b>✍️ Опишите проблему:</b>"
    if prev_details:
        text += f"\n\nБыло введено: {prev_details}"

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)]],
            resize_keyboard=True,
        ),
    )
    return States.START_WAIT_DETAILS


async def conv_back_to_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к добавлению фото."""

    query = update.callback_query

    photo_kb = ReplyKeyboardMarkup(
        [
            [KeyboardButton("✔️ Готово"), KeyboardButton(BTN_BACK)],
            [KeyboardButton("❌ Отменить все")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

    text = (
        f"<b>🖼️ Вы вернулись к фото.</b>\n"
        f"Загружено файлов: {len(context.user_data.get('attachments', []))}.\n"
        "Отправьте еще или нажмите <b>Готово</b>."
    )

    if query:
        await query.answer()
        await query.delete_message()

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=photo_kb,
            parse_mode=ParseMode.HTML,
        )

    else:
        await update.message.reply_text(
            text,
            reply_markup=photo_kb,
            parse_mode=ParseMode.HTML,
        )

    return States.START_WAIT_PHOTOS


async def conv_ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saved_name = context.user_data.get("saved_name")
    saved_ip = context.user_data.get("saved_ip")
    saved_dep = context.user_data.get("saved_department")

    context.user_data.clear()

    if saved_name:
        context.user_data["saved_name"] = saved_name
    if saved_ip:
        context.user_data["saved_ip"] = saved_ip
    if saved_dep:
        context.user_data["saved_department"] = saved_dep

    text = (
        '1️⃣⬜⬜⬜ <b>Шаг 1 из 4</b>\n'
        '<b>👋 Введите ФИО:</b>\n\n(Или нажмите кнопку "Отменить действие")'
    )
    buttons = [[KeyboardButton(BTN_CANCEL)]]

    if saved_name:
        text += f"\n\nРанее вы использовали: <b>{saved_name}</b>. Нажмите кнопку ниже, чтобы использовать его снова."
        buttons.insert(0, [KeyboardButton(saved_name)])

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(
            buttons, resize_keyboard=True, one_time_keyboard=True
        ),
    )
    return States.START_WAIT_NAME


async def conv_ask_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fio = update.message.text.strip()
    if not is_valid_fio(fio):
        await update.message.reply_text(
            "Пожалуйста, введите ФИО полностью (например: Сергеев Алексей Андреевич).",
            reply_markup=CANCEL_KEYBOARD,
        )
        return States.START_WAIT_NAME

    context.user_data["name"] = fio
    context.user_data["saved_name"] = fio

    saved_ip = context.user_data.get("saved_ip")
    text = "1️⃣2️⃣⬜⬜ <b>Шаг 2 из 4</b>\n<b>🌐 Теперь введите IP адрес компьютера:</b>"
    buttons = [[KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)]]

    if saved_ip:
        text += f"\n\nРанее вы вводили: <b>{saved_ip}</b>"
        buttons.insert(0, [KeyboardButton(saved_ip)])

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(
            buttons, resize_keyboard=True, one_time_keyboard=True
        ),
    )
    return States.START_WAIT_IP


async def conv_ask_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip_address = update.message.text.strip()
    if not is_valid_ip(ip_address):
        await update.message.reply_text(
            "<b>❌ Неверный формат IP.</b>\nПожалуйста, введите IP-адрес в формате 123.123.123.123",
            parse_mode=ParseMode.HTML,
            reply_markup=CANCEL_KEYBOARD,
        )
        return States.START_WAIT_IP

    context.user_data["ip_address"] = ip_address
    context.user_data["saved_ip"] = ip_address
    context.user_data["dep_page"] = 0

    bridge_keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("📂 Выбрать отделение")],
            [KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    msg = await update.message.reply_text(
        "IP принят ✔️\nНажмите кнопку ниже, чтобы открыть список.",
        reply_markup=bridge_keyboard,
    )

    context.user_data["msg_to_delete"] = msg.message_id

    return States.START_WAIT_DEPARTMENT


async def conv_show_departments_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if "msg_to_delete" in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data["msg_to_delete"],
            )
        except Exception:
            pass
        del context.user_data["msg_to_delete"]

    try:
        await update.message.delete()
    except Exception:
        pass

    waiting_msg = await update.message.reply_text(
        "Загрузка списка...", reply_markup=ReplyKeyboardRemove()
    )

    saved_dep = context.user_data.get("saved_department")

    await update.message.reply_text(
        "1️⃣2️⃣3️⃣⬜ <b>Шаг 3 из 4</b>\n<b>🗂️ Выберите отделение:</b>",
        reply_markup=get_departments_inline_keyboard(0, saved_dep=saved_dep),
        parse_mode=ParseMode.HTML,
    )

    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id, message_id=waiting_msg.message_id
        )
    except:
        pass

    return States.START_WAIT_DEPARTMENT


async def department_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    saved_dep = context.user_data.get("saved_department")

    filter_letter = context.user_data.get("dep_filter")
    departments = (
        filter_departments_by_letter(DEPARTMENTS, filter_letter)
        if filter_letter
        else DEPARTMENTS
    )

    if data == "use_saved_dep":
        await query.answer()
        if saved_dep:
            context.user_data["department"] = saved_dep
            await query.edit_message_text(f"Вы выбрали: {saved_dep} (из сохраненных)")

            details_kb = ReplyKeyboardMarkup(
                [[KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)]],
                resize_keyboard=True,
            )

            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text="<b>✍️ Опишите проблему:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=details_kb,
            )
            return States.START_WAIT_DETAILS
        else:
            await query.answer("Сохраненное отделение не найдено", show_alert=True)
            return States.START_WAIT_DEPARTMENT

    if data.startswith("dep_page:") or data.startswith("dep_letter:"):
        await query.answer()

        if data.startswith("dep_page:"):
            page = int(data.split(":")[1])
            context.user_data["dep_page"] = page

        elif data.startswith("dep_letter:"):
            letter = data.split(":", 1)[1]
            if letter == "all":
                context.user_data.pop("dep_filter", None)
            else:
                context.user_data["dep_filter"] = letter
            context.user_data["dep_page"] = 0

        try:
            await query.edit_message_reply_markup(
                reply_markup=get_departments_inline_keyboard(
                    context.user_data.get("dep_page", 0),
                    context.user_data.get("dep_filter"),
                    saved_dep,
                )
            )
        except BadRequest:
            pass

        return States.START_WAIT_DEPARTMENT

    if data.startswith("dep_idx:"):
        await query.answer()
        idx = int(data.split(":", 1)[1])
        dep = departments[idx]
        context.user_data["department"] = dep
        if context.user_data.get("editing_mode"):
            await query.delete_message()
            return await conv_show_confirmation(update, context)

        context.user_data["saved_department"] = dep

        await query.edit_message_text(f"Вы выбрали: {dep}")

        details_kb = ReplyKeyboardMarkup(
            [[KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)]],
            resize_keyboard=True,
        )

        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="1️⃣2️⃣3️⃣4️⃣ <b>Шаг 4 из 4</b>\n<b>✍️ Опишите проблему:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=details_kb,
        )
        return States.START_WAIT_DETAILS


async def conv_ask_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["details"] = update.message.text.strip()
    context.user_data["photos"] = []

    photo_kb = ReplyKeyboardMarkup(
        [
            [KeyboardButton("✔️ Готово"), KeyboardButton(BTN_BACK)],
            [KeyboardButton("❌ Отменить все")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

    await update.message.reply_text(
        "<b>🖼️ Если хотите добавить скриншот(фото) ошибки или документ(Word, Excel, PDF, ZIP), отправьте их сейчас.</b>\n"
        "Можно отправить несколько. Когда всё готово, нажмите <b>Готово</b>.",
        reply_markup=photo_kb,
        parse_mode=ParseMode.HTML,
    )
    return States.START_WAIT_PHOTOS


async def conv_add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_data = {"b64": None, "extension": "jpg"}

    photo_kb = ReplyKeyboardMarkup(
        [
            [KeyboardButton("✔️ Готово"), KeyboardButton(BTN_BACK)],
            [KeyboardButton("❌ Отменить все")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

    try:
        file_obj = None

        if update.message.photo:
            file_obj = await context.bot.get_file(update.message.photo[-1].file_id)
            file_data["extension"] = "jpg"

        elif update.message.video:
            video = update.message.video

            if video.file_size and video.file_size > 20 * 1024 * 1024:
                await update.message.reply_text(
                    "⚠️ <b>Видео слишком большое!</b>\n"
                    "Телеграм позволяет ботам скачивать файлы только до 20 МБ.\n"
                    "Пожалуйста, сожмите видео или отправьте скриншот.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=photo_kb
                )
                return States.START_WAIT_PHOTOS

            file_obj = await context.bot.get_file(video.file_id)
            file_data["extension"] = "mp4" 

        elif update.message.document:
            doc = update.message.document

            if doc.file_size and doc.file_size > 20 * 1024 * 1024:
                await update.message.reply_text(
                    "⚠️ Файл слишком большой (лимит 20 МБ).",
                    reply_markup=photo_kb
                )
                return States.START_WAIT_PHOTOS

            file_obj = await context.bot.get_file(doc.file_id)
            if doc.file_name and "." in doc.file_name:
                file_data["extension"] = doc.file_name.split(".")[-1].lower()
            else:
                file_data["extension"] = "bin"

        else:
            await update.message.reply_text(
                "Пожалуйста, пришлите фото, видео или файл.",
                reply_markup=photo_kb,
            )
            return States.START_WAIT_PHOTOS

        if file_obj:
            status_msg = await update.message.reply_text("⏳ Обработка файла...")
            
            downloaded_bytes = await file_obj.download_as_bytearray()
            file_data["b64"] = base64.b64encode(downloaded_bytes).decode("utf-8")

            try:
                await status_msg.delete()
            except:
                pass

        if "attachments" not in context.user_data:
            context.user_data["attachments"] = []

        context.user_data["attachments"].append(file_data)

        await update.message.reply_text(
            f"Файл {len(context.user_data['attachments'])} добавлен ({file_data['extension']}).",
            reply_markup=DONE_KEYBOARD,
        )

    except Exception as e:
        logger.error(f"File upload error: {e}")
        await update.message.reply_text(
            "Ошибка обработки файла. Возможно, он слишком большой или поврежден.", 
            reply_markup=DONE_KEYBOARD
        )
    return States.START_WAIT_PHOTOS


async def conv_show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    files_count = len(d.get("attachments", []))
    chat_id = update.effective_chat.id

    context.user_data.pop("editing_mode", None)
    processing_msg = None
    try:
        processing_msg = await context.bot.send_message(
            chat_id=chat_id, text="⏳", reply_markup=ReplyKeyboardRemove()
        )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение удаления клавиатуры: {e}")

    summary = (
        f"<b>Проверьте данные:</b>\n\n"
        f"👤 <b>ФИО:</b> {d.get('name')}\n"
        f"🌐 <b>IP:</b> {d.get('ip_address')}\n"
        f"🏥 <b>Отделение:</b> {d.get('department')}\n"
        f"📝 <b>Описание:</b> {d.get('details')}\n"
        f"📎 <b>Вложений:</b> {files_count} шт.\n\n"
        f"Всё верно?"
    )

    confirm_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Отправить", callback_data="confirm_send")],
            [
                InlineKeyboardButton("✏️ ФИО", callback_data="edit:name"),
                InlineKeyboardButton("✏️ IP", callback_data="edit:ip"),
            ],
            [
                InlineKeyboardButton("✏️ Отделение", callback_data="edit:dep"),
                InlineKeyboardButton("✏️ Описание", callback_data="edit:details"),
            ],
            [
                InlineKeyboardButton("⬅️ Назад к фото", callback_data="confirm_back"),
                InlineKeyboardButton("❌ Отменить всё", callback_data="confirm_cancel"),
            ],
        ]
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=summary,
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard,
    )
    if processing_msg:
        try:
            await context.bot.delete_message(
                chat_id=chat_id, message_id=processing_msg.message_id
            )
        except Exception:
            pass

    return States.START_CONFIRMATION


async def conv_edit_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие кнопок '✏️ Изменить ...'"""
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")[1]

    cancel_edit_kb = ReplyKeyboardMarkup(
        [[KeyboardButton("🔙 Вернуться без изменений")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    if data == "name":
        await query.edit_message_text(
            f"Текущее ФИО: {context.user_data.get('name')}\n<b>Введите новое ФИО:</b>",
            parse_mode=ParseMode.HTML,
        )
        await context.bot.send_message(
            update.effective_chat.id, "⌨️", reply_markup=cancel_edit_kb
        )
        return States.EDIT_WAIT_NAME

    elif data == "ip":
        await query.edit_message_text(
            f"Текущий IP: {context.user_data.get('ip_address')}\n<b>Введите новый IP:</b>",
            parse_mode=ParseMode.HTML,
        )
        await context.bot.send_message(
            update.effective_chat.id, "⌨️", reply_markup=cancel_edit_kb
        )
        return States.EDIT_WAIT_IP

    elif data == "details":
        await query.edit_message_text(
            f"Текущее описание: {context.user_data.get('details')}\n<b>Введите новое описание:</b>",
            parse_mode=ParseMode.HTML,
        )
        await context.bot.send_message(
            update.effective_chat.id, "⌨️", reply_markup=cancel_edit_kb
        )
        return States.EDIT_WAIT_DETAILS

    elif data == "dep":
        context.user_data["editing_mode"] = True
        context.user_data["dep_page"] = 0
        await query.delete_message()
        await context.bot.send_message(
            update.effective_chat.id,
            "<b>🗂️ Выберите новое отделение:</b>",
            reply_markup=get_departments_inline_keyboard(0),
            parse_mode=ParseMode.HTML,
        )
        return States.START_WAIT_DEPARTMENT


async def conv_save_edited_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "🔙 Вернуться без изменений":
        return await conv_show_confirmation(update, context)

    if not is_valid_fio(text):
        await update.message.reply_text(
            "Пожалуйста, введите корректное ФИО (минимум 2 слова)."
        )
        return States.EDIT_WAIT_NAME

    context.user_data["name"] = text
    return await conv_show_confirmation(update, context)


async def conv_save_edited_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "🔙 Вернуться без изменений":
        return await conv_show_confirmation(update, context)

    if not is_valid_ip(text):
        await update.message.reply_text(
            "Неверный формат IP (123.123.123.123). Попробуйте снова."
        )
        return States.EDIT_WAIT_IP

    context.user_data["ip_address"] = text
    return await conv_show_confirmation(update, context)


async def conv_save_edited_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "🔙 Вернуться без изменений":
        return await conv_show_confirmation(update, context)

    context.user_data["details"] = text
    return await conv_show_confirmation(update, context)


async def conv_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "⏳ Отправка заявки на сервер...", reply_markup=None
        )

    app_id = str(uuid.uuid4())[:8]
    data = {
        "name": context.user_data.get("name"),
        "ip": context.user_data.get("ip_address"),
        "department": context.user_data.get("department"),
        "details": context.user_data.get("details"),
        "attachments": context.user_data.get("attachments", []),
        "chat_id": update.effective_user.id,
        "username": update.effective_user.username or "",
        "application_id": app_id,
        "status": "active",
    }

    success = await api_client.post_new_application(data)

    msg_admin = (
        f"🔔 <b>Новая заявка!</b> 🔔\n\n"
        f"<b>ID:</b> <code>{app_id}</code>\n"
        f"<b>От:</b> {data['name']}\n"
        f"<b>Файлов:</b> {len(data['attachments'])} шт.\n"
        f"<b>Описание:</b>\n{data['details'][:40]}..."
    )

    saved_name = context.user_data.get("name")
    saved_ip = context.user_data.get("ip_address")
    saved_dep = context.user_data.get("department")

    context.user_data.clear()

    if saved_name:
        context.user_data["saved_name"] = saved_name
    if saved_ip:
        context.user_data["saved_ip"] = saved_ip
    if saved_dep:
        context.user_data["saved_department"] = saved_dep

    chat_id = update.effective_user.id

    if not success:
        await context.bot.send_message(
            chat_id,
            "❌ Ошибка при отправке заявки на сервер. Попробуйте позже.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    final_text = (
        f"<b>✅ Ваша заявка принята в работу!</b>\n"
        f"Уникальный идентификатор заявки: <code>{app_id}</code>\n\n"
        "Чтобы подать новую заявку, нажмите Новая Заявка."
    )

    await context.bot.send_message(
        chat_id, final_text, parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD
    )

    admin_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🙋‍♂️ Взять в работу", callback_data=f"assign:{app_id}")]
    ])

    for nid in NOTIFY_CHAT_IDS:
        try:
            await context.bot.send_message(
                nid, 
                msg_admin, 
                parse_mode=ParseMode.HTML,
                reply_markup=admin_markup
            )
        except:
            pass

    return ConversationHandler.END


async def conv_ask_update_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user_id = update.effective_user.id
    rows = await asyncio.to_thread(db_service.get_active_apps_by_chat_id, user_id)

    text = "<b>🔄 Добавление фото</b>\n"
    inline_keyboard = []
    if rows:
        text += "Выберите заявку из списка или введите ID (8 симв.):"
        for row in rows:
            details = row["details"].replace("\n", " ")
            trunc = (details[:40] + "...") if len(details) > 40 else details
            inline_keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{row['application_id']} | {trunc}",
                        callback_data=f"up_sel:{row['application_id']}",
                    )
                ]
            )
    else:
        text += "Введите ID заявки (8 символов):"

    markup = InlineKeyboardMarkup(inline_keyboard) if inline_keyboard else None
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=CANCEL_KEYBOARD
    )
    if markup:
        await update.message.reply_text("Активные заявки:", reply_markup=markup)
    return States.UPDATE_WAIT_ID


async def conv_update_id_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    app_id = query.data.split(":")[1]
    context.user_data["update_id"] = app_id
    context.user_data["update_photos"] = []
    await query.edit_message_text(
        f"Выбрана заявка: <code>{app_id}</code>", parse_mode=ParseMode.HTML
    )
    await context.bot.send_message(
        update.effective_chat.id,
        f"📸 <b>Отправьте фото для добавления к заявке <code>{app_id}</code>.</b>\n"
        "Вы можете отправить несколько. Когда закончите, нажмите Готово.",
        reply_markup=DONE_KEYBOARD,
        parse_mode=ParseMode.HTML,
    )
    return States.UPDATE_WAIT_PHOTO


async def conv_ask_update_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_id = update.message.text.strip().lower()
    if len(app_id) != 8:
        await update.message.reply_text(
            "ID должен быть 8 символов.", reply_markup=CANCEL_KEYBOARD
        )
        return States.UPDATE_WAIT_ID
    context.user_data["update_id"] = app_id
    context.user_data["update_photos"] = []
    await update.message.reply_text(
        f"📸 <b>Отправьте фото для <code>{app_id}</code>.</b>",
        reply_markup=DONE_KEYBOARD,
        parse_mode=ParseMode.HTML,
    )
    return States.UPDATE_WAIT_PHOTO


async def conv_process_update_photo_add(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    file_data = {"b64": None, "extension": "jpg"}
    try:
        file_obj = None
        if update.message.photo:
            file_obj = await context.bot.get_file(update.message.photo[-1].file_id)
            file_data["extension"] = "jpg"
        elif update.message.document:
            doc = update.message.document
            file_obj = await context.bot.get_file(doc.file_id)
            if doc.file_name and "." in doc.file_name:
                file_data["extension"] = doc.file_name.split(".")[-1].lower()
            else:
                file_data["extension"] = "bin"
        else:
            await update.message.reply_text(
                "Это не фото и не файл.", reply_markup=DONE_KEYBOARD
            )
            return States.UPDATE_WAIT_PHOTO

        if file_obj:
            downloaded_bytes = await file_obj.download_as_bytearray()
            file_data["b64"] = base64.b64encode(downloaded_bytes).decode("utf-8")

            if "update_attachments" not in context.user_data:
                context.user_data["update_attachments"] = []
            context.user_data["update_attachments"].append(file_data)

            await update.message.reply_text(
                f"Файл {len(context.user_data['update_attachments'])} добавлен.",
                reply_markup=DONE_KEYBOARD,
            )
    except Exception as e:
        logger.error(f"Update upload error: {e}")
        await update.message.reply_text("Ошибка загрузки.", reply_markup=DONE_KEYBOARD)
    return States.UPDATE_WAIT_PHOTO


async def conv_process_update_photo_done(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    app_id = context.user_data.get("update_id")
    attachments = context.user_data.get("update_attachments", [])

    if not attachments:
        await update.message.reply_text(
            "Файлы не были отправлены.", reply_markup=MAIN_KEYBOARD
        )
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text("Загрузка...", reply_markup=MAIN_KEYBOARD)
    if await api_client.update_photos(
        app_id, update.effective_user.username, attachments
    ):
        await update.message.reply_text("✅ Файлы добавлены!")
    else:
        await update.message.reply_text("❌ Ошибка (заявка не найдена).")
    context.user_data.clear()
    return ConversationHandler.END


async def conv_ask_append_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user_id = update.effective_user.id
    rows = await asyncio.to_thread(db_service.get_active_apps_by_chat_id, user_id)

    text = "<b>📝 Дополнение заявки</b>\n"
    inline_keyboard = []
    if rows:
        text += "Выберите заявку или введите ID:"
        for row in rows:
            details = row["details"].replace("\n", " ")
            trunc = (details[:40] + "...") if len(details) > 40 else details
            inline_keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{row['application_id']} | {trunc}",
                        callback_data=f"ap_sel:{row['application_id']}",
                    )
                ]
            )
    else:
        text += "Введите ID (8 символов):"

    markup = InlineKeyboardMarkup(inline_keyboard) if inline_keyboard else None
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=CANCEL_KEYBOARD
    )
    if markup:
        await update.message.reply_text("Активные заявки:", reply_markup=markup)
    return States.APPEND_WAIT_ID


async def conv_append_id_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    app_id = query.data.split(":")[1]
    context.user_data["append_id"] = app_id
    await query.edit_message_text(
        f"Выбрана заявка: <code>{app_id}</code>", parse_mode=ParseMode.HTML
    )
    await context.bot.send_message(
        update.effective_chat.id,
        f"✍️ <b>Введите текст для <code>{app_id}</code>:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=CANCEL_KEYBOARD,
    )
    return States.APPEND_WAIT_TEXT


async def conv_ask_append_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_id = update.message.text.strip().lower()
    if len(app_id) != 8:
        await update.message.reply_text(
            "ID должен быть 8 символов.", reply_markup=CANCEL_KEYBOARD
        )
        return States.APPEND_WAIT_ID
    context.user_data["append_id"] = app_id
    await update.message.reply_text(
        f"✍️ <b>Введите дополнительный текст для заявки <code>{app_id}</code>:</b>\n\n(Или нажмите кнопку 'Отменить действие')",
        parse_mode=ParseMode.HTML,
        reply_markup=CANCEL_KEYBOARD,
    )
    return States.APPEND_WAIT_TEXT


async def conv_process_append_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_id = context.user_data.get("append_id")
    text = update.message.text.strip()
    if await api_client.append_details(app_id, update.effective_user.username, text):
        await update.message.reply_text(
            f"<b>✅ Текст успешно добавлен к заявке <code>{app_id}</code>!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await update.message.reply_text(
            "❌ Ошибка (заявка не найдена).", reply_markup=MAIN_KEYBOARD
        )
    context.user_data.clear()
    return ConversationHandler.END


async def conv_ask_check_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE, should_edit=False
):
    user_id = update.effective_user.id
    rows = await asyncio.to_thread(db_service.get_active_apps_by_chat_id, user_id)

    if not rows:
        text = "У вас нет активных заявок."
        if should_edit:
            await update.callback_query.edit_message_text(text, reply_markup=None)
        else:
            await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    keyboard = []
    for row in rows:
        details = row["details"].replace("\n", " ")
        trunc = (details[:50] + "...") if len(details) > 50 else details
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{row['application_id']} | {trunc}",
                    callback_data=f"check_status:{row['application_id']}",
                )
            ]
        )

    markup = InlineKeyboardMarkup(keyboard)
    text = "<b>Активные заявки:</b>\n(Нажмите для просмотра)"

    if should_edit:
        await update.callback_query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    else:
        await context.bot.send_message(
            user_id, text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    return ConversationHandler.END


async def back_to_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await conv_ask_check_status(update, context, should_edit=True)


async def check_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data.startswith("check_status:"):
        app_id = data.split(":", 1)[1]

        result = await asyncio.to_thread(
            db_service.get_app_status_for_user, app_id, query.from_user.id
        )

        back_btn = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Назад к списку", callback_data="back_to_active_list"
                    )
                ]
            ]
        )

        if not result:
            await query.edit_message_text("Заявка не найдена.", reply_markup=back_btn)
            return

        status, details, difficulty, name, department = result

        MAX_LEN = 200
        details_show = details[:MAX_LEN] + "..." if len(details) > MAX_LEN else details

        time_estimates = {
            "low": "1 день",
            "medium": "3-5 дней",
            "high": "7 и более дней",
            "naumen": "Передано разработчикам",
        }
        est_time = time_estimates.get(difficulty, "1 день")

        if status == "done":
            text = (
                f"<b>Заявка:</b> <code>{app_id}</code>\n"
                f"<b>Статус:</b> ✅ Выполнена\n\n"
                f"(Эта заявка скоро пропадет из этого списка)"
            )
        else:
            text = (
                f"🎫 <b>ЗАЯВКА</b> <code>{app_id}</code>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Отправитель:</b> {name}\n"
                f"⏳ <b>Статус:</b> В работе\n"
                f"🕒 <b>Примерное время ожидания:</b> {est_time}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📝 <b>Описание:</b>\n<i>{details_show}</i>"
            )

        await query.edit_message_text(
            text=text, parse_mode=ParseMode.HTML, reply_markup=back_btn
        )


async def conv_ask_return_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    rows = await asyncio.to_thread(
        db_service.get_done_apps_by_chat_id, update.effective_user.id, limit=5
    )
    text = "<b>♻️ Возврат заявки</b>\n\n"
    if rows:
        text += "Вот 5 ваших последних выполненных заявок:\n"
        for row in rows:
            details = row["details"].replace("\n", " ")
            trunc = (details[:60] + "...") if len(details) > 60 else details
            text += f"<b>ID:</b> <code>{row['application_id']}</code> | {trunc}\n"
    text += "\n<b>Введите ID заявки</b> (8 символов):"
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=CANCEL_KEYBOARD
    )
    return States.RETURN_WAIT_ID


async def conv_ask_return_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_id = update.message.text.strip().lower()
    if len(app_id) != 8:
        await update.message.reply_text(
            "ID должен быть 8 символов.", reply_markup=CANCEL_KEYBOARD
        )
        return States.RETURN_WAIT_ID
    context.user_data["return_id"] = app_id
    await update.message.reply_text(
        "📝 <b>Опишите причину</b>\n"
        'Что вам не понравилось в выполненной заявке или какая проблема осталась?\n\n(Или нажмите кнопку "Отменить действие")',
        parse_mode=ParseMode.HTML,
        reply_markup=CANCEL_KEYBOARD,
    )
    return States.RETURN_WAIT_REASON


async def conv_process_return_reason(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    reason = update.message.text.strip()
    app_id = context.user_data.get("return_id")
    username = update.effective_user.username or f"user_{update.effective_user.id}"
    user_info = await asyncio.to_thread(db_service.get_user_info_for_app, app_id)

    if user_info:
        _, name_from_db = user_info
    else:
        name_from_db = "Неизвестно"

    msg_admin = (
        f"⚠️ <b>Запрос на возврат!</b> ⚠️\n\n"
        f"<b>Пользователь:</b> @{username}\n"
        f"<b>Имя:</b> {name_from_db}\n"
        f"<b>ID:</b> <code>{app_id}</code>\n"
        f"<b>Причина:</b> {reason}\n\n"
        f"Восстановить: /e {app_id}"
    )

    for chat_id in NOTIFY_CHAT_IDS:
        try:
            await context.bot.send_message(
                chat_id, msg_admin, parse_mode=ParseMode.HTML
            )
        except:
            pass

    await update.message.reply_text(
        f"✅ <b>Ваш запрос на возврат заявки <code>{app_id}</code> отправлен специалистам.</b>\n\n"
        f"Они рассмотрят причину и, при необходимости, вернут заявку в работу. Вы получите отдельное уведомление.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )

    context.user_data.clear()
    return ConversationHandler.END


async def reply_to_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    app_id = query.data.split(":")[1]
    context.user_data["reply_app_id"] = app_id
    context.user_data["reply_message_id"] = query.message.message_id

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✍️ <b>Введите ваш ответ по заявке <code>{app_id}</code>:</b>\n\n"
            f"⚠️ <i>Отправляйте всё одним сообщением. Если прикладываете фото или документ — пишите текст в описании к нему!!!</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=CANCEL_KEYBOARD,
    )
    return States.REPLY_WAIT_TEXT


async def process_reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_id = context.user_data.get("reply_app_id")
    username = update.effective_user.username or "Пользователь"

    message_text = ""
    file_id = None
    file_type = "text"

    if update.message.text:
        message_text = update.message.text
    elif update.message.caption:
        message_text = update.message.caption

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_type = "document"

    api_text_prefix = ""
    if file_type == "photo":
        api_text_prefix = "[Прикреплено фото] "
    elif file_type == "document":
        api_text_prefix = "[Прикреплен файл] "

    formatted_text = f"[ОТВЕТ ПОЛЬЗОВАТЕЛЯ]: {api_text_prefix}{message_text}"

    await api_client.add_message(app_id, username, formatted_text)

    caption_admin = f"📨 <b>Ответ от пользователя!</b>\nID: <code>{app_id}</code>\n"
    if message_text:
        caption_admin += f"{message_text}"

    for admin_id in NOTIFY_CHAT_IDS:
        try:
            if file_type == "photo":
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=caption_admin,
                    parse_mode=ParseMode.HTML
                )
            elif file_type == "document":
                await context.bot.send_document(
                    chat_id=admin_id,
                    document=file_id,
                    caption=caption_admin,
                    parse_mode=ParseMode.HTML
                )
            else:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=caption_admin,
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            logger.error(f"Не удалось переслать ответ админу {admin_id}: {e}")

    await update.message.reply_text(
        "✅ Ваш ответ отправлен.", reply_markup=MAIN_KEYBOARD
    )

    try:
        msg_id = context.user_data.get("reply_message_id")
        if msg_id:
            await context.bot.edit_message_reply_markup(
                chat_id=update.effective_chat.id, message_id=msg_id, reply_markup=None
            )
    except Exception as e:
        logger.error(f"Cant remove button: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saved_name = context.user_data.get("saved_name")
    saved_ip = context.user_data.get("saved_ip")
    saved_dep = context.user_data.get("saved_department")

    context.user_data.clear()

    if saved_name:
        context.user_data["saved_name"] = saved_name
    if saved_ip:
        context.user_data["saved_ip"] = saved_ip
    if saved_dep:
        context.user_data["saved_department"] = saved_dep

    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Действие отменено.", reply_markup=None)
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="Вы в главном меню.",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await update.message.reply_text(
            "Действие отменено.", reply_markup=MAIN_KEYBOARD
        )

    return ConversationHandler.END


async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type

    if user_id in NOTIFY_CHAT_IDS:
        return

    if chat_type in ["group", "supergroup"]:
        return

    await update.message.reply_text(
        "⚠️ <b>Я не понимаю это сообщение.</b>\n\n"
        "Пожалуйста, используйте кнопки меню для работы с ботом.\n"
        "Если меню пропало, отправьте команду <i>/start</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


async def request_password_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in NOTIFY_CHAT_IDS:
        await update.message.reply_text("У вас нет прав на выполнение этой команды.")
        return
    if not context.args:
        return
    app_id = context.args[0]
    user_info = await asyncio.to_thread(db_service.get_user_info_for_app, app_id)
    if not user_info:
        await update.message.reply_text("Не найдено.")
        return
    chat_id, name = user_info
    context.bot_data[app_id] = update.effective_chat.id
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Отправить пароль для {app_id}",
                    callback_data=f"pwd_start:{app_id}",
                )
            ]
        ]
    )
    await context.bot.send_message(
        chat_id,
        text=(
            f"Здравствуйте, {name}!\n"
            f"Сотруднику техподдержки требуется пароль от ЕМИАС для работы по вашей заявке <code>{app_id}</code>.\n\n"
            f"<b>Пожалуйста, нажмите кнопку ниже, чтобы начать:</b>"
        ),
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )
    await update.message.reply_text("Запрос отправлен.")


async def conv_password_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    app_id = query.data.split(":")[1]
    context.user_data["app_id_for_password"] = app_id
    await query.edit_message_text("Введите пароль:", parse_mode=ParseMode.HTML)
    return States.PASSWORD_REQUEST_WAIT_PASSWORD


async def conv_password_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text
    app_id = context.user_data.pop("app_id_for_password", None)
    try:
        await update.message.delete()
    except:
        pass
    await update.message.reply_text(
        "✅ Спасибо, пароль принят и будет немедленно доставлен сотруднику.",
        reply_markup=MAIN_KEYBOARD,
    )
    emp_id = context.bot_data.pop(app_id, None)
    if emp_id:
        await context.bot.send_message(
            emp_id,
            f"🔐 Пароль для заявки <code>{app_id}</code>:\n<code>{pwd}</code>",
            parse_mode=ParseMode.HTML,
        )
    return ConversationHandler.END

async def rate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data_parts = query.data.split(":")
    if len(data_parts) != 3:
        return

    app_id = data_parts[1]
    stars = data_parts[2]

    success = await api_client.send_rating(app_id, stars)

    if success:
        new_text = f"✅ Спасибо! Вы поставили оценку: {stars} ⭐"
    else:
        new_text = f"Спасибо! Ваша оценка: {stars} ⭐ (не удалось сохранить на сервере)"

    try:
        await query.edit_message_caption(caption=new_text) if query.message.caption else await query.edit_message_text(text=new_text)
    except BadRequest:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=new_text)

async def conv_repeat_last_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    last_data = await asyncio.to_thread(db_service.get_last_user_data, user_id)
    
    if not last_data:
        await update.message.reply_text(
            "⚠️ <b>История не найдена.</b>\n"
            "Вы еще не подавали заявки или база данных была очищена.\n"
            "Начинаем стандартную процедуру...",
            parse_mode=ParseMode.HTML
        )
        return await conv_ask_name(update, context)

    context.user_data["name"] = last_data["name"]
    context.user_data["ip_address"] = last_data["ip"]
    context.user_data["department"] = last_data["department"]

    context.user_data["saved_name"] = last_data["name"]
    context.user_data["saved_ip"] = last_data["ip"]
    context.user_data["saved_department"] = last_data["department"]

    details_kb = ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL)]],
        resize_keyboard=True,
    )

    msg = (
        f"🔄 <b>Автозаполнение по последней заявке:</b>\n\n"
        f"👤 ФИО: <b>{last_data['name']}</b>\n"
        f"🌐 IP: <b>{last_data['ip']}</b>\n"
        f"🏥 Отделение: <b>{last_data['department']}</b>\n\n"
        f"🚀 1️⃣2️⃣3️⃣4️⃣ <b>Шаг 4 из 4</b>\n"
        f"<b>✍️ Опишите проблему:</b>"
    )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=details_kb
    )
    
    return States.START_WAIT_DETAILS

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALL_PERMITTED_IDS:
        await update.message.reply_text("⛔ У вас нет прав для просмотра статистики.")
        return

    msg = await update.message.reply_text("📊 Анализирую базу данных и рисую график...")

    rows = await asyncio.to_thread(db_service.get_statistics_data)
    if not rows:
        await msg.edit_text("В базе данных пока пусто.")
        return
    
    if process_pool is None:
        logger.error("Пул процессов не инициализирован! Проверьте блок if __name__ == '__main__':")
        await msg.edit_text("Ошибка конфигурации бота (нет пула процессов).")
        return

    total_apps = len(rows)
    active_apps = 0
    done_today = 0
    departments = []
    total_seconds_spent = 0
    count_closed_with_dates = 0
    today_str = datetime.now().strftime("%Y-%m-%d")

    for row in rows:
        status = row["status"]
        dept = row["department"]
        archived_at = row["archived_at"]
        created_at = row["created_at"]

        if status == "active":
            active_apps += 1
        
        if dept:
            departments.append(dept)

        if status == "done" and archived_at:
            if archived_at.startswith(today_str):
                done_today += 1

            if created_at:
                try:
                    fmt = "%Y-%m-%d %H:%M"
                    start_dt = datetime.strptime(created_at, fmt)
                    end_dt = datetime.strptime(archived_at, fmt)
                    delta = (end_dt - start_dt).total_seconds()
                    if delta > 0:
                        total_seconds_spent += delta
                        count_closed_with_dates += 1
                except ValueError:
                    continue

    avg_time_str = "—"
    if count_closed_with_dates > 0:
        avg_seconds = total_seconds_spent / count_closed_with_dates
        avg_hours = int(avg_seconds // 3600)
        avg_minutes = int((avg_seconds % 3600) // 60)
        avg_time_str = f"{avg_hours} ч. {avg_minutes} мин." if avg_hours > 0 else f"{avg_minutes} мин."

    dept_counts = Counter(departments).most_common(5)
    top_dept_text = f"{dept_counts[0][0]} ({dept_counts[0][1]})" if dept_counts else "Нет данных"

    report_text = (
        f"📊 <b>Статистика КИС ЕМИАС-отдела</b>\n\n"
        f"📅 <b>Закрыто за сегодня:</b> {done_today}\n"
        f"⚡ <b>Среднее время решения:</b> {avg_time_str}\n"
        f"🔥 <b>В работе прямо сейчас:</b> {active_apps}\n"
        f"📂 <b>Всего заявок за всё время:</b> {total_apps}\n"
        f"🏆 <b>Самое проблемное отделение:</b> {top_dept_text}\n"
    )

    
    photo_stream = None
    
    if dept_counts:
        labels = [d[0] for d in dept_counts]
        values = [d[1] for d in dept_counts]

        labels.reverse()
        values.reverse()

        try:
            loop = asyncio.get_running_loop()

            photo_stream = await loop.run_in_executor(
                process_pool, 
                generate_plot_sync, 
                labels, 
                values
            )
        except Exception as e:
            logger.error(f"Ошибка генерации графика: {e}")
            pass

    try:
        await msg.delete()
    except:
        pass

    if photo_stream:
        await context.bot.send_photo(
            chat_id=user_id,
            photo=photo_stream,
            caption=report_text,
            parse_mode=ParseMode.HTML
        )
    else:
        await context.bot.send_message(
            chat_id=user_id,
            text=report_text + "\n\n(График не создан — нет данных по отделам)",
            parse_mode=ParseMode.HTML
        )

async def stats_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in ALL_PERMITTED_IDS:
        await update.message.reply_text("⛔ У вас нет прав для просмотра статистики.")
        return
    
    rows = await asyncio.to_thread(db_service.get_time_stats)
    if not rows:
        await update.message.reply_text("Нет данных о времени создания заявок.")
        return

    hours = [int(row['h']) for row in rows]
    counts = [row[1] for row in rows]

    msg = await update.message.reply_text("⏳ Рисую график по времени...")
    
    if process_pool is None:
        await msg.edit_text("Ошибка пула процессов.")
        return

    loop = asyncio.get_running_loop()
    photo_stream = await loop.run_in_executor(process_pool, generate_time_plot_sync, hours, counts)

    await msg.delete()
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=photo_stream,
        caption="📈 <b>Нагрузка по часам</b>",
        parse_mode=ParseMode.HTML
    )

async def stats_rating_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in ALL_PERMITTED_IDS:
        await update.message.reply_text("⛔ У вас нет прав для просмотра статистики.")
        return

    rows = await asyncio.to_thread(db_service.get_rating_stats)
    if not rows:
        await update.message.reply_text("Нет данных об оценках.")
        return
    labels = [f"{row['rating']}⭐" for row in rows]
    clean_labels = [str(row['rating']) for row in rows] 
    counts = [row[1] for row in rows]

    msg = await update.message.reply_text("🍰 Рисую диаграмму удовлетворенности...")
    
    if process_pool is None:
        await msg.edit_text("Ошибка пула процессов.")
        return

    loop = asyncio.get_running_loop()
    photo_stream = await loop.run_in_executor(process_pool, generate_rating_plot_sync, clean_labels, counts)

    await msg.delete()
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=photo_stream,
        caption="🍰 <b>Удовлетворенность пользователей</b>",
        parse_mode=ParseMode.HTML
    )



async def assign_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data 
    app_id = data.split(":")[1]
    tg_user = query.from_user
    username = tg_user.username
    if username and username in USERNAME_TO_FIO:
        admin_name = USERNAME_TO_FIO[username]
    else:
        admin_name = f"@{username}" if username else tg_user.full_name
    status, response_data = await api_client.assign_application(app_id, admin_name)

    original_text = query.message.text_html or query.message.caption_html or ""

    if status == "success":
        await query.answer("✅ Вы взяли заявку!")
        if "✅ Заявку принял:" not in original_text:
            new_text = original_text + f"\n\n✅ <b>Заявку принял:</b> {admin_name}"
            try:
                if query.message.photo:
                    await query.edit_message_caption(caption=new_text, parse_mode=ParseMode.HTML, reply_markup=None)
                else:
                    await query.edit_message_text(text=new_text, parse_mode=ParseMode.HTML, reply_markup=None)
            except Exception:
                pass
        user_chat_id = response_data.get("user_chat_id")
        
        if user_chat_id:
            try:
                await context.bot.send_message(
                    chat_id=user_chat_id,
                    text=f"👨‍💻 Вашей заявкой <code>{app_id}</code> занимается: <b>{admin_name}</b>.",
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление пользователю {user_chat_id}: {e}")
            
    elif status == "already_taken":
        current = response_data.get("current_assignee", "коллега")
        await query.answer(f"❌ Заявку выполняет другой сотрудник: {current}", show_alert=True)
        
        if "✅ Заявку принял:" not in original_text:
            new_text = original_text + f"\n\n✅ <b>Заявку принял:</b> {current}"
            try:
                if query.message.photo:
                    await query.edit_message_caption(caption=new_text, parse_mode=ParseMode.HTML, reply_markup=None)
                else:
                    await query.edit_message_text(text=new_text, parse_mode=ParseMode.HTML, reply_markup=None)
            except Exception:
                pass
                
    else:
        await query.answer("❌ Ошибка сервера или заявка не найдена.", show_alert=True)

def main():
    my_persistence = PicklePersistence(filepath="bot_data.pickle")
    db_service.check_and_add_assignee_column()

    application = Application.builder().token(TOKEN).persistence(my_persistence).build()

    cancel_filter = filters.Regex(f"^{re.escape(BTN_CANCEL)}$")

    cancel_handler = MessageHandler(cancel_filter, cancel)

    back_filter = filters.Regex(f"^{re.escape(BTN_BACK)}$")

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text("📝 Новая заявка"), conv_ask_name),
            MessageHandler(filters.Text("🔄 Повторить последнюю заявку"), conv_repeat_last_app),
            MessageHandler(filters.Text("Добавить фото"), conv_ask_update_id),
            MessageHandler(filters.Text("Дополнить заявку"), conv_ask_append_id),
            MessageHandler(filters.Text("Вернуть заявку в работу"), conv_ask_return_id),
            MessageHandler(
                filters.Text("Проверить статус заявки"), conv_ask_check_status
            ),
            MessageHandler(filters.Text("📚 Инструкция"), send_instruction),
            CallbackQueryHandler(reply_to_admin_callback, pattern="^reply_admin:"),
        ],
        states={
            States.START_WAIT_NAME: [
                cancel_handler,
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_ip),
            ],
            States.START_WAIT_IP: [
                MessageHandler(back_filter, conv_back_to_name),
                cancel_handler,
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_department),
            ],
            States.START_WAIT_DEPARTMENT: [
                MessageHandler(back_filter, conv_back_to_ip),
                MessageHandler(
                    filters.Text("📂 Выбрать отделение"), conv_show_departments_list
                ),
                CallbackQueryHandler(department_callback, pattern="^dep_"),
                CallbackQueryHandler(department_callback, pattern="^use_saved_dep"),
                CallbackQueryHandler(cancel, pattern="^cancel_action$"),
                cancel_handler,
            ],
            States.START_WAIT_DETAILS: [
                MessageHandler(back_filter, conv_back_to_department_bridge),
                cancel_handler,
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_photos),
            ],
            States.START_WAIT_PHOTOS: [
                MessageHandler(back_filter, conv_back_to_details),
                MessageHandler(filters.Text("✔️ Готово"), conv_show_confirmation),
                MessageHandler(filters.Text("❌ Отменить все"), cancel),
                MessageHandler(filters.PHOTO | filters.Document.ALL | filters.VIDEO, conv_add_photo),
            ],
            States.START_CONFIRMATION: [
                CallbackQueryHandler(conv_done, pattern="^confirm_send$"),
                CallbackQueryHandler(conv_back_to_photos, pattern="^confirm_back$"),
                CallbackQueryHandler(cancel, pattern="^confirm_cancel$"),
                CallbackQueryHandler(conv_edit_choice_callback, pattern="^edit:"),
            ],
            States.EDIT_WAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_save_edited_name)
            ],
            States.EDIT_WAIT_IP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_save_edited_ip)
            ],
            States.EDIT_WAIT_DETAILS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, conv_save_edited_details
                )
            ],
            States.UPDATE_WAIT_ID: [
                cancel_handler,
                CallbackQueryHandler(conv_update_id_callback, pattern="^up_sel:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_update_photo),
            ],
            States.UPDATE_WAIT_PHOTO: [
                MessageHandler(
                    filters.PHOTO | filters.Document.ALL,
                    conv_process_update_photo_add,
                ),
                MessageHandler(
                    filters.Text("✔️ Готово"), conv_process_update_photo_done
                ),
                MessageHandler(filters.Text("❌ Отменить все"), cancel),
            ],
            States.APPEND_WAIT_ID: [
                cancel_handler,
                CallbackQueryHandler(conv_append_id_callback, pattern="^ap_sel:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_append_text),
            ],
            States.APPEND_WAIT_TEXT: [
                cancel_handler,
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, conv_process_append_text
                ),
            ],
            States.RETURN_WAIT_ID: [
                cancel_handler,
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_ask_return_reason),
            ],
            States.RETURN_WAIT_REASON: [
                cancel_handler,
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, conv_process_return_reason
                ),
            ],
            States.REPLY_WAIT_TEXT: [
                cancel_handler,
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, 
                    process_reply_text
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Text(BTN_CANCEL), cancel),
            CommandHandler("start", conv_ask_name),
            MessageHandler(filters.Text("📝 Новая заявка"), conv_ask_name),
        ],
    )

    pwd_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(conv_password_start_cb, pattern="^pwd_start:")
        ],
        states={
            States.PASSWORD_REQUEST_WAIT_PASSWORD: [
                cancel_handler,
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_password_receive),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(pwd_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("q", finish_command))
    application.add_handler(CommandHandler("w", whisper_command))
    application.add_handler(CommandHandler("e", restore_command))
    application.add_handler(CommandHandler("r", request_password_command))
    application.add_handler(CommandHandler("b", broadcast_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("time", stats_time_command))
    application.add_handler(CommandHandler("rating", stats_rating_command))

    application.add_handler(
        CallbackQueryHandler(check_status_callback, pattern="^check_status:")
    )
    application.add_handler(
        CallbackQueryHandler(back_to_list_callback, pattern="^back_to_active_list$")
    )
    application.add_handler(
        CallbackQueryHandler(rate_handler, pattern="^rate:")
    )
    application.add_handler(
        CallbackQueryHandler(assign_callback, pattern="^assign:")
    )

    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
            handle_unknown_message,
        )
    )

    application.run_polling()


if __name__ == "__main__":
    process_pool = ProcessPoolExecutor(max_workers=2)
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        if process_pool:
            process_pool.shutdown(wait=True)
