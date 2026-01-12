import os
import base64
import sqlite3
import json
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, AdminIndexView, expose
from flask_admin.contrib.sqla import ModelView
import requests
from flask import (
    Flask,
    request,
    jsonify,
    send_from_directory,
    render_template,
    redirect,
    url_for,
    send_file,
    g,
    session,
    Response,
)
import sys
import pandas as pd
from io import BytesIO
from threading import Thread
import io
from math import ceil
from functools import wraps
from dotenv import load_dotenv


load_dotenv()
UPLOAD_FOLDER_NAME = "uploads"
BOT_TOKEN = os.getenv("TOKEN")
API_SECRET = os.getenv("API_TOKEN")
if not API_SECRET:
    print("⛔ SERVER ERROR: No API_TOKEN found!")
    exit(1)

app = Flask(__name__)

WEB_USER = os.getenv("WEB_USER")
WEB_PASS = os.getenv("WEB_PASS")
if not API_SECRET or not WEB_PASS:
    raise ValueError("Не заданы переменные окружения API_TOKEN или WEB_PASS")

def check_auth(username, password):
    """Сверяет введенные данные с переменными окружения"""
    return username == WEB_USER and password == WEB_PASS

def authenticate():
    """Отправляет заголовк 401, вызывающий окно логина в браузере"""
    return Response(
    'Доступ запрещен. Введите правильный логин и пароль.\n', 401,
    {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


if os.path.exists("/data"):
    print("Server running on Render with Persistent Disk")
    DATABASE_FILE = "/data/applications.db"
    UPLOAD_FOLDER_PATH = "/data/uploads"
else:
    print("Server running locally")
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATABASE_FILE = os.path.join(BASE_DIR, "applications.db")
    UPLOAD_FOLDER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER_PATH
app.config["DATABASE"] = DATABASE_FILE
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.secret_key = "super_secret_flask_key"
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

PER_PAGE = 50

def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get("Authorization")
        if not token or token != f"Bearer {API_SECRET}":
            return jsonify({"error": "Доступ запрещен. Неверный токен."}), 401
            
        return f(*args, **kwargs)
    return decorated_function


class FileService:
    def __init__(self, upload_folder):
        self.upload_folder = upload_folder
        os.makedirs(self.upload_folder, exist_ok=True)

    def save_photo_from_b64(self, b64_data: str, filename: str) -> str:
        try:
            photo_path = os.path.join(self.upload_folder, filename)
            with open(photo_path, "wb") as f:
                f.write(base64.b64decode(b64_data))
            return filename
        except Exception as e:
            print(f"Ошибка сохранения файла {filename}: {e}")
            return None

    def delete_photos(self, filenames: list):
        if not filenames:
            return
        for filename in filenames:
            try:
                full_path = os.path.join(self.upload_folder, filename)
                if os.path.exists(full_path):
                    os.remove(full_path)
            except Exception as e:
                print(f"Ошибка удаления файла {filename}: {e}")


file_service = FileService(app.config["UPLOAD_FOLDER"])


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.execute(
        """CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id TEXT UNIQUE,
        chat_id TEXT,
        ip TEXT,
        name TEXT,
        department TEXT,
        details TEXT,
        username TEXT,
        photos TEXT,
        status TEXT DEFAULT 'active',
        created_at TEXT,
        archived_at TEXT,
        done_by TEXT,
        difficulty TEXT DEFAULT 'low',
        staff_notes TEXT,
        solution TEXT,
        rating INTEGER DEFAULT 0,
        assignee TEXT 
    )"""
    )

    db.execute(
        """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id TEXT NOT NULL,
        sender TEXT,
        message_text TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )"""
    )

    migrations = [
        "ALTER TABLE applications ADD COLUMN rating INTEGER DEFAULT 0",
        "ALTER TABLE applications ADD COLUMN assignee TEXT",
        "ALTER TABLE applications ADD COLUMN difficulty TEXT DEFAULT 'low'"
    ]

    for sql in migrations:
        try:
            db.execute(sql)
            print(f"Миграция успешна: {sql}")
        except sqlite3.OperationalError:
            pass
        
    db.commit()

def repo_get_stats_time():
    db = get_db()
    cursor = db.execute("SELECT strftime('%H', created_at) as h, COUNT(*) FROM applications GROUP BY h ORDER BY h")
    return [dict(row) for row in cursor.fetchall()]

def repo_get_stats_rating():
    db = get_db()
    cursor = db.execute("SELECT rating, COUNT(*) as count FROM applications WHERE rating > 0 GROUP BY rating")
    return [dict(row) for row in cursor.fetchall()]

def repo_update_staff_notes(app_db_id: int, notes: str):
    db = get_db()
    db.execute(
        "UPDATE applications SET staff_notes = ? WHERE id = ?", (notes, app_db_id)
    )
    db.commit()

def repo_create_application(data: dict):
    db = get_db()
    db.execute(
        """INSERT INTO applications 
           (name, department, details, username, photos, application_id, chat_id, status, ip, created_at, difficulty) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get("name"),
            data.get("department"),
            data.get("details"),
            data.get("username"),
            data.get("photos_json"),
            data.get("application_id"),
            data.get("chat_id"),
            data.get("status"),
            data.get("ip"),
            data.get("created_at"),
            data.get("difficulty"),
        ),
    )
    db.commit()

def repo_add_message(application_id, sender, text):
    db = get_db()
    db.execute(
        "INSERT INTO messages (application_id, sender, message_text) VALUES (?, ?, ?)",
        (application_id, sender, text),
    )
    db.commit()
    
def repo_get_stats_complexity():
    db = get_db()

    cursor = db.execute("""
        SELECT department, difficulty, COUNT(*) as count
        FROM applications
        WHERE department IS NOT NULL AND department != ''
        GROUP BY department, difficulty
    """)
    rows = cursor.fetchall()

    departments = sorted(list(set(row["department"] for row in rows)))

    data_map = {
        "low": [0] * len(departments),
        "medium": [0] * len(departments),
        "high": [0] * len(departments),
        "naumen": [0] * len(departments),
        "employee": [0] * len(departments)
    }
    
    for row in rows:
        dept = row["department"]
        diff = row["difficulty"] or "low"
        count = row["count"]
        
        if diff in data_map and dept in departments:
            idx = departments.index(dept)
            data_map[diff][idx] = count
            
    return {
        "departments": departments,
        "datasets": data_map
    }

def repo_save_rating(application_id: str, rating: int):
    db = get_db()
    db.execute(
        "UPDATE applications SET rating = ? WHERE application_id = ?",
        (rating, application_id)
    )
    db.commit()

def repo_restore_application(application_id: str):
    db = get_db()
    cur = db.execute(
        "SELECT chat_id, name FROM applications WHERE application_id = ?",
        (application_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    cur.execute(
        "UPDATE applications SET status = 'active', archived_at = NULL, done_by = NULL, rating = 0 WHERE application_id = ?",
        (application_id,),
    )
    db.commit()
    return dict(row)

def repo_set_difficulty(application_id: int, difficulty: str):
    db = get_db()
    db.execute(
        "UPDATE applications SET difficulty=? WHERE id=?", (difficulty, application_id)
    )
    db.commit()

def repo_get_applications_paginated(status: str, page: int, per_page: int):
    db = get_db()

    cur_count = db.execute(
        "SELECT COUNT(id) FROM applications WHERE status = ?", (status,)
    )
    total_items = cur_count.fetchone()[0]

    if total_items == 0:
        return [], 1

    total_pages = ceil(total_items / per_page)
    offset = (page - 1) * per_page
    order_by = "archived_at DESC" if status == "done" else "created_at DESC"

    query = f"SELECT * FROM applications WHERE status = ? ORDER BY {order_by} LIMIT ? OFFSET ?"
    cur = db.execute(query, (status, per_page, offset))
    rows = cur.fetchall()

    applications = []
    for row in rows:
        app_data = dict(row)

        try:
            photo_paths = json.loads(app_data.get("photos", "[]"))
        except (json.JSONDecodeError, TypeError):
            photo_paths = []

        file_objects = []
        for p in photo_paths:
            ext = p.split(".")[-1].lower()
            is_image = ext in ["jpg", "jpeg", "png", "gif", "bmp", "webp"]
            is_video = ext in ["mp4", "mov", "avi", "webm", "mkv"]
            is_other_file = not is_image and not is_video
            
            file_objects.append(
                {
                    "url": url_for("uploaded_file", filename=p),
                    "filename": p,
                    "is_image": is_image,
                    "is_video": is_video,
                    "is_file": is_other_file,
                    "extension": ext,
                }
            )
        app_data["photo_objects"] = file_objects

        for date_field in ["created_at", "archived_at"]:
            try:
                if app_data.get(date_field):
                    app_data[date_field] = datetime.strptime(
                        app_data[date_field], "%Y-%m-%d %H:%M"
                    )
            except (ValueError, TypeError):
                app_data[date_field] = None

        if status == "done":
            manual_solution = app_data.get("solution")
            if manual_solution and "[ОТВЕТ ПОЛЬЗОВАТЕЛЯ]" in str(manual_solution):
                manual_solution = None

            if not manual_solution:
                cur_msgs = db.execute(
                    """SELECT message_text FROM messages WHERE application_id = ? 
                    ORDER BY created_at DESC LIMIT 20""",
                    (app_data["application_id"],),
                )
                messages = cur_msgs.fetchall()
                found_text = None
                for msg in messages:
                    text = msg["message_text"]
                    if "[ОТВЕТ ПОЛЬЗОВАТЕЛЯ]" not in text:
                        found_text = text
                        break
                app_data["solution"] = found_text

        applications.append(app_data)

    return applications, total_pages

def repo_get_comments_view():
    db = get_db()
    cur = db.execute(
        "SELECT application_id, name, department, details FROM applications WHERE status = 'active' ORDER BY created_at DESC"
    )
    apps = [dict(row) for row in cur.fetchall()]

    for app in apps:
        cur_msgs = db.execute(
            "SELECT sender, message_text, created_at FROM messages WHERE application_id = ? ORDER BY created_at ASC",
            (app["application_id"],),
        )
        msgs = [dict(row) for row in cur_msgs.fetchall()]
        for m in msgs:
            try:
                dt = datetime.strptime(m["created_at"], "%Y-%m-%d %H:%M:%S")
                dt = dt + timedelta(hours=3)
                m["created_at"] = dt.strftime("%d-%m %H:%M")
            except Exception as e:
                pass
        app["messages"] = msgs

    return apps

def repo_get_raw_apps_for_export() -> list:
    db = get_db()
    query = """
        SELECT a.*, 
        COALESCE(a.solution, 
            (SELECT message_text FROM messages m 
             WHERE m.application_id = a.application_id 
             AND m.message_text NOT LIKE '[ОТВЕТ ПОЛЬЗОВАТЕЛЯ]%' 
             ORDER BY m.created_at DESC LIMIT 1)
        ) as solution 
        FROM applications a 
        WHERE a.status = 'done'
    """
    cur = db.execute(query)
    return [dict(row) for row in cur.fetchall()]

def repo_delete_application(application_id: int) -> list:
    db = get_db()
    cur = db.execute(
        "SELECT photos, application_id FROM applications WHERE id = ?",
        (application_id,),
    )
    row = cur.fetchone()

    if row:
        app_uuid = row["application_id"]
        db.execute("DELETE FROM messages WHERE application_id = ?", (app_uuid,))

    db.execute("DELETE FROM applications WHERE id = ?", (application_id,))
    db.commit()

    if row and row["photos"]:
        try:
            return json.loads(row["photos"])
        except (json.JSONDecodeError, TypeError):
            pass
    return []

def repo_delete_single_photo(app_id: int, filename_to_delete: str) -> bool:
    db = get_db()
    cur = db.execute("SELECT photos FROM applications WHERE id = ?", (app_id,))
    row = cur.fetchone()
    if not row:
        return False
    try:
        current_photos = json.loads(row["photos"] or "[]")
    except (json.JSONDecodeError, TypeError):
        current_photos = []

    if filename_to_delete not in current_photos:
        return False

    new_photos_list = [f for f in current_photos if f != filename_to_delete]
    new_photos_json = json.dumps(new_photos_list)
    db.execute(
        "UPDATE applications SET photos = ? WHERE id = ?", (new_photos_json, app_id)
    )
    db.commit()
    return True

def repo_append_photos(application_id: str, username: str, new_filenames_list: list) -> bool:
    db = get_db()
    try:
        with db:
            cur_check = db.execute(
                "SELECT id FROM applications WHERE application_id=? AND lower(username)=lower(?)",
                (application_id, username),
            )
            if not cur_check.fetchone():
                return False
            for filename in new_filenames_list:
                db.execute(
                    """
                    UPDATE applications
                    SET photos = json_insert(COALESCE(photos, '[]'), '$[#]', ?)
                    WHERE application_id = ?
                    """,
                    (filename, application_id),
                )
        return True
    except sqlite3.Error as e:
        print(f"Ошибка при batch-обновлении json: {e}")
        return False

def repo_append_details(application_id: str, username: str, extra_text: str) -> bool:
    db = get_db()
    cur = db.execute(
        "SELECT details FROM applications WHERE application_id=? AND lower(username)=lower(?)",
        (application_id, username),
    )
    row = cur.fetchone()
    if not row:
        return False
    old_details = row["details"] or ""
    new_details = f"{old_details}\n{extra_text}"
    db.execute(
        "UPDATE applications SET details=? WHERE application_id=?",
        (new_details, application_id),
    )
    db.commit()
    return True

@app.route('/rate_application', methods=['POST'])
@require_api_key
def rate_application():
    data = request.json
    app_id = data.get('application_id')
    rating = data.get('rating')

    if not app_id or not rating:
        return jsonify({"error": "Missing data"}), 400
        
    try:
        repo_save_rating(app_id, rating)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/add_message", methods=["POST"])
@require_api_key
def api_add_message():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    repo_add_message(data.get("application_id"), data.get("sender"), data.get("text"))
    return jsonify({"status": "ok"}), 200


@app.route("/api/assign", methods=["POST"])
@require_api_key
def api_assign_user():
    data = request.json
    app_id = data.get("application_id")
    admin_name = data.get("admin_name")
    
    if not app_id or not admin_name:
        return jsonify({"error": "Missing data"}), 400
        
    db = get_db()
    cursor = db.execute(
        "UPDATE applications SET assignee = ? WHERE application_id = ? AND (assignee IS NULL OR assignee = '')",
        (admin_name, app_id)
    )
    db.commit()

    if cursor.rowcount > 0:
        cur_user = db.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,))
        row = cur_user.fetchone()
        
        return jsonify({
            "status": "success",
            "user_chat_id": row["chat_id"],
            "user_name": row["name"]
        }), 200
    else:
        cur_check = db.execute("SELECT assignee FROM applications WHERE application_id = ?", (app_id,))
        row = cur_check.fetchone()
        if row and row["assignee"]:
            return jsonify({
                "status": "already_taken", 
                "current_assignee": row["assignee"]
            }), 409
            
        return jsonify({"error": "Not found"}), 404


@app.route("/applications", methods=["POST"])
@require_api_key
def add_application():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    application_id = data.get("application_id")
    file_paths = []
    attachments = data.get("attachments", [])
    if not attachments and data.get("photos"):
        attachments = [{"b64": p, "extension": "jpg"} for p in data.get("photos")]

    for idx, file_data in enumerate(attachments):
        photo_b64 = file_data.get("b64")
        extension = file_data.get("extension", "jpg").replace(".", "")
        if photo_b64:
            filename = f"{application_id}_{idx}.{extension}"
            saved_name = file_service.save_photo_from_b64(photo_b64, filename)
            if saved_name:
                file_paths.append(saved_name)

    db_data = {
        "name": data.get("name", "").title(),
        "ip": data.get("ip"),
        "department": data.get("department"),
        "details": data.get("details"),
        "application_id": application_id,
        "username": data.get("username"),
        "chat_id": data.get("chat_id"),
        "status": data.get("status", "active"),
        "difficulty": data.get("difficulty", "low"),
        "photos_json": json.dumps(file_paths),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    try:
        repo_create_application(db_data)
        return jsonify({"message": "Заявка добавлена"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Application ID already exists"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/restore/<application_id>", methods=["POST"])
@require_api_key
def restore_application(application_id):
    user_data = repo_restore_application(application_id)
    if not user_data:
        return jsonify({"error": "Заявка не найдена"}), 404
    return jsonify(user_data), 200

@app.route("/api/stats/complexity", methods=["GET"])
@require_api_key
def api_stats_complexity():
    data = repo_get_stats_complexity()
    return jsonify(data), 200


@app.route("/update_photos", methods=["POST"])
@require_api_key
def update_photos():
    data = request.get_json()
    application_id = data.get("application_id")
    username = data.get("username")
    attachments = data.get("attachments", [])
    if not attachments and data.get("photos"):
        attachments = [{"b64": p, "extension": "jpg"} for p in data.get("photos")]

    if not all([application_id, username, attachments]):
        return jsonify({"error": "Missing required fields"}), 400

    saved_names_list = []
    for idx, file_data in enumerate(attachments):
        photo_b64 = file_data.get("b64")
        extension = file_data.get("extension", "jpg").replace(".", "")
        filename = (
            f"{application_id}_upd_{int(datetime.now().timestamp())}_{idx}.{extension}"
        )
        saved_name = file_service.save_photo_from_b64(photo_b64, filename)
        if not saved_name:
            file_service.delete_photos(saved_names_list)
            return jsonify({"error": "Failed to save file"}), 500
        saved_names_list.append(saved_name)

    success = repo_append_photos(application_id, username, saved_names_list)
    if not success:
        file_service.delete_photos(saved_names_list)
        return jsonify({"error": "Заявка не найдена или не принадлежит вам"}), 404
    return jsonify({"message": "Файлы добавлены"}), 200


@app.route("/append_details", methods=["POST"])
@require_api_key
def append_details():
    data = request.get_json()
    application_id = data.get("application_id")
    username = data.get("username")
    extra_text = data.get("extra_text")

    if not all([application_id, username, extra_text]):
        return jsonify({"error": "Missing required fields"}), 400

    success = repo_append_details(application_id, username, extra_text)
    if not success:
        return jsonify({"error": "Заявка не найдена или не принадлежит вам"}), 404
    return jsonify({"message": "Текст заявки дополнен"}), 200


@app.route("/delete/<int:application_id>", methods=["POST"])
@require_api_key
def delete_application(application_id):
    filenames_to_delete = repo_delete_application(application_id)
    file_service.delete_photos(filenames_to_delete)
    return redirect(request.referrer or url_for("get_archive"))


@app.route("/api/stats/time", methods=["GET"])
@require_api_key
def api_stats_time():
    data = repo_get_stats_time()
    return jsonify(data), 200


@app.route("/api/stats/rating", methods=["GET"])
@require_api_key
def api_stats_rating():
    data = repo_get_stats_rating()
    return jsonify(data), 200

@app.route("/api/get_user_info_for_app/<app_id>", methods=["GET"])
def api_get_user_info_for_app(app_id):
    db = get_db()
    cur = db.execute(
        "SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,)
    )
    row = cur.fetchone()
    if row:
        return jsonify(dict(row)), 200
    else:
        return jsonify({"error": "Not found"}), 404


@app.route("/api/get_app_ids_for_user/<chat_id>", methods=["GET"])
def api_get_app_ids_for_user(chat_id):
    db = get_db()
    cur = db.execute(
        "SELECT application_id FROM applications WHERE chat_id = ?", (chat_id,)
    )
    rows = cur.fetchall()
    app_ids = [row["application_id"] for row in rows]
    return jsonify(app_ids), 200


@app.route("/", methods=["GET"])
@requires_auth
def home():
    return render_template("index.html")


@app.route("/applications")
@requires_auth
def get_active_applications():
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1

    applications, total_pages = repo_get_applications_paginated(
        "active", page, PER_PAGE
    )
    if page > total_pages and total_pages > 0:
        page = total_pages
        applications, total_pages = repo_get_applications_paginated(
            "active", page, PER_PAGE
        )

    return render_template(
        "applications.html",
        applications=applications,
        archive=False,
        current_page=page,
        total_pages=total_pages,
    )


@app.route("/comments")
@requires_auth
def comments_view():
    applications = repo_get_comments_view()
    return render_template("comments.html", applications=applications)


@app.route("/archive")
@requires_auth
def get_archive():
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    applications, total_pages = repo_get_applications_paginated("done", page, PER_PAGE)
    if page > total_pages and total_pages > 0:
        page = total_pages
        applications, total_pages = repo_get_applications_paginated(
            "done", page, PER_PAGE
        )
    return render_template(
        "applications.html",
        applications=applications,
        archive=True,
        current_page=page,
        total_pages=total_pages,
    )


@app.route("/set_difficulty/<int:application_id>", methods=["POST"])
@requires_auth
def set_difficulty(application_id):
    new_difficulty = request.form.get("difficulty")
    if new_difficulty not in ["low", "medium", "high", "naumen", "employee"]:
        return "Некорректное значение сложности", 400
    
    repo_set_difficulty(application_id, new_difficulty)
    return redirect(request.referrer or url_for("get_active_applications"))


@app.route("/export_archive")
@requires_auth
def export_archive():
    raw_rows = repo_get_raw_apps_for_export()
    if not raw_rows:
        return "Нет архивных заявок", 404

    df = pd.DataFrame(raw_rows)
    columns_to_export = [
        "application_id", "name", "department", "details",
        "created_at", "archived_at", "done_by", "solution", "rating"
    ]
    available_columns = [c for c in columns_to_export if c in df.columns]
    df = df[available_columns]
    rename_map = {
        "application_id": "ID", "name": "ФИО", "department": "Отделение",
        "details": "Проблема", "created_at": "Создание заявки",
        "archived_at": "Дата выполнения", "done_by": "Исполнитель",
        "solution": "Решение", "rating": "Оценка"
    }
    df = df.rename(columns=rename_map)
    for col in ["Создание заявки", "Дата выполнения"]:
        if col in df.columns:
            df[col] = pd.to_datetime(
                df[col], format="%Y-%m-%d %H:%M", errors="coerce"
            ).dt.strftime("%d-%m-%Y %H:%M")

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Archive")
        worksheet = writer.sheets["Archive"]
        for column_cells in worksheet.columns:
            length = max(len(str(cell.value) or "") for cell in column_cells)
            worksheet.column_dimensions[column_cells[0].column_letter].width = min(
                length + 2, 60
            )
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name="archive.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/delete_photo/<int:app_id>/<path:filename>", methods=["POST"])
@requires_auth
def delete_photo(app_id, filename):
    success = repo_delete_single_photo(app_id, filename)
    if not success:
        return redirect(request.referrer or url_for("get_active_applications"))
    try:
        file_service.delete_photos([filename])
    except Exception as e:
        print(f"Ошибка удаления файла {filename}: {e}")
        pass
    return redirect(request.referrer or url_for("get_active_applications"))


@app.route("/uploads/<filename>")
def uploaded_file(filename):
    ext = filename.split(".")[-1].lower()
    image_extensions = ["jpg", "jpeg", "png", "gif", "webp", "bmp"]
    video_extensions = ["mp4", "mov", "avi", "webm", "mkv"]
    is_attachment = (ext not in image_extensions) and (ext not in video_extensions)
    return send_from_directory(
        app.config["UPLOAD_FOLDER"], 
        filename, 
        as_attachment=is_attachment
    )


@app.route("/update_notes/<int:app_id>", methods=["POST"])
@requires_auth
def update_notes(app_id):
    notes = request.form.get("staff_notes")
    repo_update_staff_notes(app_id, notes)
    return redirect(request.referrer or url_for("get_active_applications"))

@app.route("/api/download_db", methods=["GET"])
@require_api_key
def download_db():
    """Позволяет скачать файл базы данных, используя API токен"""
    try:
        return send_file(
            app.config["DATABASE"],
            as_attachment=True,
            download_name=f"backup_{datetime.now().strftime('%Y%m%d')}.db"
        )
    except Exception as e:
        return jsonify({"error": f"Ошибка скачивания: {e}"}), 500
    
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{app.config['DATABASE']}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db_orm = SQLAlchemy(app)

class ApplicationModel(db_orm.Model):
    __tablename__ = 'applications'
    id = db_orm.Column(db_orm.Integer, primary_key=True)
    application_id = db_orm.Column(db_orm.String, unique=True)
    chat_id = db_orm.Column(db_orm.String)
    ip = db_orm.Column(db_orm.String)
    name = db_orm.Column(db_orm.String)
    department = db_orm.Column(db_orm.String)
    details = db_orm.Column(db_orm.Text)
    username = db_orm.Column(db_orm.String)
    photos = db_orm.Column(db_orm.Text)
    status = db_orm.Column(db_orm.String, default='active')
    created_at = db_orm.Column(db_orm.String)
    archived_at = db_orm.Column(db_orm.String)
    done_by = db_orm.Column(db_orm.String)
    difficulty = db_orm.Column(db_orm.String, default='low')
    staff_notes = db_orm.Column(db_orm.Text)
    solution = db_orm.Column(db_orm.Text)
    rating = db_orm.Column(db_orm.Integer, default=0)
    assignee = db_orm.Column(db_orm.String)

class MessageModel(db_orm.Model):
    __tablename__ = 'messages'
    id = db_orm.Column(db_orm.Integer, primary_key=True)
    application_id = db_orm.Column(db_orm.String, nullable=False)
    sender = db_orm.Column(db_orm.String)
    message_text = db_orm.Column(db_orm.Text)
    created_at = db_orm.Column(db_orm.DateTime)

class SecureModelView(ModelView):
    def is_accessible(self):
        auth = request.authorization
        return auth and check_auth(auth.username, auth.password)

    def inaccessible_callback(self, name, **kwargs):
        return authenticate()

class SecureAdminIndexView(AdminIndexView):
    @expose('/')
    def index(self):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return super(SecureAdminIndexView, self).index()

admin = Admin(app, name='Управление Заявками', index_view=SecureAdminIndexView())
class ApplicationView(SecureModelView):
    column_searchable_list = ['name', 'application_id', 'username', 'details']
    column_filters = ['status', 'department', 'difficulty', 'rating']
    column_exclude_list = ['photos']
    form_widget_args = {
        'created_at': {'readonly': True}
    }

class MessageView(SecureModelView):
    column_searchable_list = ['message_text', 'sender', 'application_id']
    column_filters = ['sender']

admin.add_view(ApplicationView(ApplicationModel, db_orm.session, name="Заявки"))
admin.add_view(MessageView(MessageModel, db_orm.session, name="Сообщения"))

def send_telegram_document(chat_id, file_storage, caption, reply_markup=None):
    if not BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)

    file_storage.seek(0)
    files = {"document": (file_storage.filename, file_storage)}
    
    try:
        requests.post(url, data=data, files=files, timeout=30)
    except Exception as e:
        print(f"Ошибка отправки документа в TG: {e}")

def send_telegram_message(chat_id, text, reply_markup=None):
    if not BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=30)
    except Exception as e:
        print(f"Ошибка отправки в TG: {e}")

def send_telegram_photo(chat_id, file_storage, caption, reply_markup=None):
    if not BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    file_storage.seek(0)
    files = {"photo": file_storage}
    try:
        requests.post(url, data=data, files=files, timeout=30)
    except Exception as e:
        print(f"Ошибка отправки фото в TG: {e}")


@app.route("/tools")
@requires_auth
def tools_page():
    return render_template("tools.html")

def background_sender(app, chat_id, text, reply_markup, file_data=None, filename=None, is_photo=False):
    """
    Эта функция работает в отдельном потоке внутри контекста приложения.
    """
    with app.app_context():
        try:
            file_obj = None
            if file_data and filename:
                file_obj = io.BytesIO(file_data)
                file_obj.name = filename 

            if file_obj:
                if is_photo:
                    send_telegram_photo(chat_id, file_obj, text, reply_markup=reply_markup)
                else:
                    send_telegram_document(chat_id, file_obj, text, reply_markup=reply_markup)
            else:
                send_telegram_message(chat_id, text, reply_markup=reply_markup)
                
            print(f"Log: Сообщение успешно отправлено в фоне для {chat_id}", file=sys.stdout)
            sys.stdout.flush()
            
        except Exception as e:
            print(f"Error: Ошибка при фоновой отправке: {e}", file=sys.stderr)
            sys.stdout.flush()


@app.route("/api/tools/execute", methods=["POST"])
@require_api_key
def tools_execute():
    action = request.form.get("action")
    app_id = request.form.get("app_id")
    admin_name = request.form.get("admin_name")
    text = request.form.get("text", "")
    file_obj = request.files.get("photo") 
    
    if not app_id or not action:
        return jsonify({"error": "Нет ID или действия"}), 400

    db = get_db()
    is_photo = False
    file_data = None
    filename = None

    if file_obj and file_obj.filename:
        filename = file_obj.filename
        file_data = file_obj.read()
        
        if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
            is_photo = True

    if action == "finish":
        cur = db.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,))
        row = cur.fetchone()
        if not row: return jsonify({"error": "Заявка не найдена"}), 404
            
        chat_id, user_name = row["chat_id"], row["name"]
        archived_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        solution_text = text if text else "Заявка выполнена."
        db.execute(
            "UPDATE applications SET status='done', archived_at=?, done_by=?, solution=? WHERE application_id=?",
            (archived_at, admin_name, solution_text, app_id)
        )
        db.commit()

        msg = f"{user_name}, ваша заявка <code>{app_id}</code> выполнена!\n\n<b>Решение:</b>\n{solution_text}"

        rating_kb = {
            "inline_keyboard": [[
                {"text": "⭐ 1", "callback_data": f"rate:{app_id}:1"},
                {"text": "⭐ 2", "callback_data": f"rate:{app_id}:2"},
                {"text": "⭐ 3", "callback_data": f"rate:{app_id}:3"},
                {"text": "⭐ 4", "callback_data": f"rate:{app_id}:4"},
                {"text": "⭐ 5", "callback_data": f"rate:{app_id}:5"},
            ]]
        }
        thread = Thread(target=background_sender, args=(
            app._get_current_object(), 
            chat_id, 
            msg, 
            rating_kb, 
            file_data, 
            filename, 
            is_photo
        ))
        thread.start()
            
        return jsonify({"message": f"Заявка {app_id} закрыта (отправка идет в фоне)."})

    elif action == "message":
        cur = db.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,))
        row = cur.fetchone()
        if not row: return jsonify({"error": "Заявка не найдена"}), 404
        chat_id = row["chat_id"]

        db.execute("INSERT INTO messages (application_id, sender, message_text) VALUES (?, ?, ?)",
                   (app_id, admin_name, text))
        db.commit()

        full_text = f"🔔 Сообщение по заявке <code>{app_id}:</code>\n\n<b>{text}</b>\n\n<i>(От: {admin_name})</i>"
        
        reply_kb = {
            "inline_keyboard": [[{"text": "✍️ Ответить", "callback_data": f"reply_admin:{app_id}"}]]
        }
        thread = Thread(target=background_sender, args=(
            app._get_current_object(), 
            chat_id, 
            full_text, 
            reply_kb, 
            file_data, 
            filename, 
            is_photo
        ))
        thread.start()
            
        return jsonify({"message": "Сообщение отправлено (в фоне)."})

    elif action == "restore":
        cur = db.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,))
        row = cur.fetchone()
        if not row: return jsonify({"error": "Заявка не найдена"}), 404
        
        db.execute("UPDATE applications SET status='active', archived_at=NULL WHERE application_id=?", (app_id,))
        db.commit()
        thread = Thread(target=send_telegram_message, args=(row["chat_id"], f"Ваша заявка <code>{app_id}</code> возвращена в работу!"))
        thread.start()

        return jsonify({"message": "Заявка восстановлена."})

    return jsonify({"error": "Неизвестное действие"}), 400


if __name__ == "__main__":
    with app.app_context():
        init_db()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)