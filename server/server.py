import os
import base64
import sqlite3
import json
from datetime import datetime 
from flask import (
    Flask, request, jsonify, send_from_directory, 
    render_template, redirect, url_for, send_file, g
)
import pandas as pd
from io import BytesIO
from math import ceil

DATABASE_FILE = 'applications.db'
UPLOAD_FOLDER_NAME = 'uploads'
BASE_DIR = os.path.dirname(__file__)
UPLOAD_FOLDER_PATH = os.path.join(BASE_DIR, UPLOAD_FOLDER_NAME)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER_PATH
app.config['DATABASE'] = DATABASE_FILE

PER_PAGE = 50 


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

file_service = FileService(app.config['UPLOAD_FOLDER'])

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS applications (
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
        staff_notes TEXT  -- <--- НОВОЕ ПОЛЕ
    )''')
    
    db.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id TEXT NOT NULL,
        sender TEXT,
        message_text TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    db.commit()

def repo_update_staff_notes(app_db_id: int, notes: str):
    db = get_db()
    db.execute("UPDATE applications SET staff_notes = ? WHERE id = ?", (notes, app_db_id))
    db.commit()

def repo_create_application(data: dict):
    db = get_db()
    db.execute(
        """INSERT INTO applications 
           (name, department, details, username, photos, application_id, chat_id, status, ip, created_at, difficulty) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get('name'), data.get('department'), data.get('details'), data.get('username'),
            data.get('photos_json'), data.get('application_id'), data.get('chat_id'),
            data.get('status'), data.get('ip'), 
            data.get('created_at'), data.get('difficulty')
        )
    )
    db.commit()

def repo_add_message(application_id, sender, text):
    """Сохраняет сообщение сотрудника в БД."""
    db = get_db()
    db.execute(
        "INSERT INTO messages (application_id, sender, message_text) VALUES (?, ?, ?)",
        (application_id, sender, text)
    )
    db.commit()

def repo_restore_application(application_id: str):
    db = get_db()
    cur = db.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (application_id,))
    row = cur.fetchone()
    if not row:
        return None
    
    cur.execute("UPDATE applications SET status = 'active', archived_at = NULL, done_by = NULL WHERE application_id = ?", (application_id,))
    db.commit()
    return dict(row)

def repo_set_difficulty(application_id: int, difficulty: str):
    db = get_db()
    db.execute("UPDATE applications SET difficulty=? WHERE id=?", (difficulty, application_id))
    db.commit()

def repo_get_applications_paginated(status: str, page: int, per_page: int):
    """
    Получает список заявок (active/done).
    Для done дополнительно подтягивает ПОСЛЕДНЕЕ сообщение как 'solution'.
    """
    db = get_db()
    
    cur_count = db.execute("SELECT COUNT(id) FROM applications WHERE status = ?", (status,))
    total_items = cur_count.fetchone()[0]
    
    if total_items == 0:
        return [], 1
        
    total_pages = ceil(total_items / per_page)
    offset = (page - 1) * per_page
    order_by = "archived_at DESC" if status == 'done' else "created_at DESC"
    
    query = f"SELECT * FROM applications WHERE status = ? ORDER BY {order_by} LIMIT ? OFFSET ?"
    cur = db.execute(query, (status, per_page, offset))
    rows = cur.fetchall()
    
    applications = []
    for row in rows:
        app_data = dict(row)
        
        try:
            photo_paths = json.loads(app_data.get('photos', '[]'))
        except (json.JSONDecodeError, TypeError):
            photo_paths = []
        app_data['photo_objects'] = [
            {'url': url_for('uploaded_file', filename=p), 'filename': p} 
            for p in photo_paths
        ]

        for date_field in ['created_at', 'archived_at']:
            try:
                if app_data.get(date_field):
                    app_data[date_field] = datetime.strptime(app_data[date_field], '%Y-%m-%d %H:%M')
            except (ValueError, TypeError):
                app_data[date_field] = None

        if status == 'done':
            cur_msg = db.execute(
                "SELECT message_text FROM messages WHERE application_id = ? ORDER BY created_at DESC LIMIT 1",
                (app_data['application_id'],)
            )
            msg_row = cur_msg.fetchone()
            app_data['solution'] = msg_row['message_text'] if msg_row else None

        applications.append(app_data)
        
    return applications, total_pages

def repo_get_comments_view():
    """
    Получает активные заявки + ВСЮ историю сообщений к ним.
    """
    db = get_db()
    # Берем активные заявки
    cur = db.execute("SELECT application_id, name, department, details FROM applications WHERE status = 'active' ORDER BY created_at DESC")
    apps = [dict(row) for row in cur.fetchall()]
    
    # Для каждой подтягиваем сообщения
    for app in apps:
        cur_msgs = db.execute("SELECT sender, message_text, created_at FROM messages WHERE application_id = ? ORDER BY created_at ASC", (app['application_id'],))
        msgs = [dict(row) for row in cur_msgs.fetchall()]
        
        # Форматируем дату сообщений
        for m in msgs:
            try:
                dt = datetime.strptime(m['created_at'], '%Y-%m-%d %H:%M:%S')
                m['created_at'] = dt.strftime('%d-%m %H:%M')
            except:
                pass
        
        app['messages'] = msgs
    
    return apps

def repo_get_raw_apps_for_export() -> list:
    """Получает выполненные заявки + поле solution (последнее сообщение)."""
    db = get_db()
    query = """
        SELECT 
            a.*,
            (SELECT message_text 
             FROM messages m 
             WHERE m.application_id = a.application_id 
             ORDER BY m.created_at DESC 
             LIMIT 1) as solution
        FROM applications a 
        WHERE a.status = 'done'
    """
    cur = db.execute(query)
    rows = cur.fetchall()
    return [dict(row) for row in rows]

def repo_delete_application(application_id: int) -> list:
    db = get_db()
    cur = db.execute("SELECT photos, application_id FROM applications WHERE id = ?", (application_id,))
    row = cur.fetchone()
    
    if row:
        app_uuid = row['application_id']
        db.execute("DELETE FROM messages WHERE application_id = ?", (app_uuid,))
    
    db.execute("DELETE FROM applications WHERE id = ?", (application_id,))
    db.commit()
    
    if row and row['photos']:
        try:
            return json.loads(row['photos'])
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
        current_photos = json.loads(row['photos'] or '[]')
    except (json.JSONDecodeError, TypeError):
        current_photos = []
    
    if filename_to_delete not in current_photos:
        return False
        
    new_photos_list = [f for f in current_photos if f != filename_to_delete]
    new_photos_json = json.dumps(new_photos_list)
    db.execute("UPDATE applications SET photos = ? WHERE id = ?", (new_photos_json, app_id))
    db.commit()
    return True

def repo_append_photos(application_id: str, username: str, new_filenames_list: list) -> bool:
    db = get_db()
    try:
        with db: 
            cur_check = db.execute(
                "SELECT id FROM applications WHERE application_id=? AND lower(username)=lower(?)",
                (application_id, username)
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
                    (filename, application_id)
                )
        return True
    except sqlite3.Error as e:
        print(f"Ошибка при batch-обновлении json: {e}")
        return False

def repo_append_details(application_id: str, username: str, extra_text: str) -> bool:
    db = get_db()
    cur = db.execute(
        "SELECT details FROM applications WHERE application_id=? AND lower(username)=lower(?)", 
        (application_id, username)
    )
    row = cur.fetchone()
    if not row:
        return False
    old_details = row['details'] or ""
    new_details = f'{old_details}\n{extra_text}'
    db.execute("UPDATE applications SET details=? WHERE application_id=?", 
               (new_details, application_id))
    db.commit()
    return True

# --- API ROUTES ---

@app.route('/api/add_message', methods=['POST'])
def api_add_message():
    """Сохранение сообщения от бота в базу."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    
    repo_add_message(
        data.get('application_id'),
        data.get('sender'),
        data.get('text')
    )
    return jsonify({'status': 'ok'}), 200

@app.route('/api/get_user_info_for_app/<app_id>', methods=['GET'])
def api_get_user_info_for_app(app_id):
    db = get_db()
    cur = db.execute("SELECT chat_id, name FROM applications WHERE application_id = ?", (app_id,))
    row = cur.fetchone()
    if row:
        return jsonify(dict(row)), 200
    else:
        return jsonify({'error': 'Not found'}), 404

@app.route('/api/get_app_ids_for_user/<chat_id>', methods=['GET'])
def api_get_app_ids_for_user(chat_id):
    db = get_db()
    cur = db.execute("SELECT application_id FROM applications WHERE chat_id = ?", (chat_id,))
    rows = cur.fetchall()
    app_ids = [row['application_id'] for row in rows]
    return jsonify(app_ids), 200

@app.route('/applications', methods=['POST'])
def add_application():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    application_id = data.get('application_id')
    photo_paths = []
    for idx, photo_b64 in enumerate(data.get('photos', [])):
        filename = f"{application_id}_{idx}.jpg"
        saved_name = file_service.save_photo_from_b64(photo_b64, filename)
        if saved_name:
            photo_paths.append(saved_name)

    db_data = {
        'name': data.get('name', '').title(),
        'ip': data.get('ip'),
        'department': data.get('department'),
        'details': data.get('details'),
        'application_id': application_id,
        'username': data.get('username'),
        'chat_id': data.get('chat_id'),
        'status': data.get('status', 'active'),
        'difficulty': data.get('difficulty', 'low'),
        'photos_json': json.dumps(photo_paths),
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M')
    }

    try:
        repo_create_application(db_data)
        return jsonify({'message': 'Заявка добавлена'}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Application ID already exists'}), 409
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/restore/<application_id>', methods=['POST'])
def restore_application(application_id):
    user_data = repo_restore_application(application_id)
    if not user_data:
        return jsonify({'error': 'Заявка не найдена'}), 404
    return jsonify(user_data), 200

@app.route('/update_photos', methods=['POST'])
def update_photos():
    data = request.get_json()
    application_id = data.get('application_id')
    username = data.get('username')
    photos_b64_list = data.get('photos', [])
    
    if not all([application_id, username, photos_b64_list]):
        return jsonify({'error': 'Missing required fields'}), 400
    
    saved_names_list = []
    for idx, photo_b64 in enumerate(photos_b64_list):
        filename = f"{application_id}_updated_{int(datetime.now().timestamp())}_{idx}.jpg"
        saved_name = file_service.save_photo_from_b64(photo_b64, filename)
        if not saved_name:
            file_service.delete_photos(saved_names_list) 
            return jsonify({'error': 'Failed to save photo'}), 500
        saved_names_list.append(saved_name)

    success = repo_append_photos(application_id, username, saved_names_list)
    if not success:
        file_service.delete_photos(saved_names_list)
        return jsonify({'error': 'Заявка не найдена или не принадлежит вам'}), 404
        
    return jsonify({'message': 'Фото обновлены'}), 200

@app.route('/append_details', methods=['POST'])
def append_details():
    data = request.get_json()
    application_id = data.get('application_id')
    username = data.get('username')
    extra_text = data.get('extra_text')

    if not all([application_id, username, extra_text]):
        return jsonify({'error': 'Missing required fields'}), 400

    success = repo_append_details(application_id, username, extra_text)
    if not success:
        return jsonify({'error': 'Заявка не найдена или не принадлежит вам'}), 404

    return jsonify({'message': 'Текст заявки дополнен'}), 200


# --- WEB ROUTES ---

@app.route('/', methods=['GET'])
def home():
    return render_template('index.html')

@app.route('/applications')
def get_active_applications():
    page = request.args.get('page', 1, type=int)
    if page < 1: page = 1
        
    applications, total_pages = repo_get_applications_paginated('active', page, PER_PAGE)
    if page > total_pages and total_pages > 0:
        page = total_pages
        applications, total_pages = repo_get_applications_paginated('active', page, PER_PAGE)

    return render_template(
        'applications.html', 
        applications=applications, 
        archive=False,
        current_page=page,
        total_pages=total_pages
    )

@app.route('/comments')
def comments_view():
    """НОВЫЙ РОУТ: Лента сообщений по активным заявкам."""
    applications = repo_get_comments_view()
    return render_template('comments.html', applications=applications)

@app.route('/archive')
def get_archive():
    page = request.args.get('page', 1, type=int)
    if page < 1: page = 1
    
    applications, total_pages = repo_get_applications_paginated('done', page, PER_PAGE)
    if page > total_pages and total_pages > 0:
        page = total_pages
        applications, total_pages = repo_get_applications_paginated('done', page, PER_PAGE)

    return render_template(
        'applications.html', 
        applications=applications, 
        archive=True,
        current_page=page,
        total_pages=total_pages
    )

@app.route('/set_difficulty/<int:application_id>', methods=['POST'])
def set_difficulty(application_id):
    new_difficulty = request.form.get('difficulty')
    if new_difficulty not in ['low', 'medium', 'high', 'naumen']:
        return "Некорректное значение сложности", 400
    
    repo_set_difficulty(application_id, new_difficulty)
    return redirect(request.referrer or url_for('get_active_applications'))

@app.route('/export_archive')
def export_archive():
    raw_rows = repo_get_raw_apps_for_export()
    if not raw_rows:
        return "Нет архивных заявок", 404
    
    df = pd.DataFrame(raw_rows)

    columns_to_export = [
        'application_id', 'name', 'department', 'details', 
        'created_at', 'archived_at', 'done_by', 'solution'
    ]
    available_columns = [c for c in columns_to_export if c in df.columns]

    df = df[available_columns]
    rename_map = {
        'application_id': 'ID', 
        'name': 'ФИО', 
        'department': 'Отделение',
        'details': 'Проблема', 
        'created_at': 'Создание заявки',
        'archived_at': 'Дата выполнения', 
        'done_by': 'Исполнитель',
        'solution': 'Решение'
    }
    df = df.rename(columns=rename_map)
    for col in ['Создание заявки', 'Дата выполнения']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], format='%Y-%m-%d %H:%M', errors='coerce').dt.strftime('%d-%m-%Y %H:%M')
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Archive')
        worksheet = writer.sheets['Archive']
        for column_cells in worksheet.columns:
            length = max(len(str(cell.value) or "") for cell in column_cells)
            worksheet.column_dimensions[column_cells[0].column_letter].width = min(length + 2, 60)

    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name='archive.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/delete/<int:application_id>', methods=['POST'])
def delete_application(application_id):
    filenames_to_delete = repo_delete_application(application_id)
    file_service.delete_photos(filenames_to_delete)
    return redirect(request.referrer or url_for('get_archive'))

@app.route('/delete_photo/<int:app_id>/<path:filename>', methods=['POST'])
def delete_photo(app_id, filename):
    success = repo_delete_single_photo(app_id, filename)
    if not success:
        return redirect(request.referrer or url_for('get_active_applications'))
    try:
        file_service.delete_photos([filename])
    except Exception as e:
        print(f"Ошибка удаления файла {filename}: {e}")
        pass 
    return redirect(request.referrer or url_for('get_active_applications'))

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/update_notes/<int:app_id>', methods=['POST'])
def update_notes(app_id):
    notes = request.form.get('staff_notes')
    repo_update_staff_notes(app_id, notes)
    return redirect(request.referrer or url_for('get_active_applications'))


if __name__ == '__main__':
    with app.app_context():
        init_db()
    
    app.run(host='0.0.0.0', port=5000, debug=True)