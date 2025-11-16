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

PER_PAGE = 10


class FileService:
    def __init__(self, upload_folder):
        self.upload_folder = upload_folder
        os.makedirs(self.upload_folder, exist_ok=True)

    def save_photo_from_b64(self, b64_data: str, filename: str) -> str:
        """Сохраняет base64 строку как файл и возвращает имя файла."""
        try:
            photo_path = os.path.join(self.upload_folder, filename)
            with open(photo_path, "wb") as f:
                f.write(base64.b64decode(b64_data))
            return filename
        except Exception as e:
            print(f"Ошибка сохранения файла {filename}: {e}")
            return None

    def delete_photos(self, filenames: list):
        """Удаляет список файлов из папки uploads."""
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
    """Открывает новое соединение с БД, если его еще нет в этом контексте."""
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    """Закрывает соединение с БД в конце запроса."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    """Инициализирует таблицу в БД."""
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id TEXT UNIQUE,
        chat_id TEXT,
        emiac TEXT,
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
        difficulty TEXT DEFAULT 'low'
    )''')
    db.commit()


def repo_create_application(data: dict):
    """Сохраняет новую заявку в БД."""
    db = get_db()
    db.execute(
        """INSERT INTO applications 
           (name, department, details, username, photos, application_id, chat_id, status, ip, emiac, created_at, difficulty) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get('name'), data.get('department'), data.get('details'), data.get('username'),
            data.get('photos_json'), data.get('application_id'), data.get('chat_id'),
            data.get('status'), data.get('ip'), data.get('emiac'),
            data.get('created_at'), data.get('difficulty')
        )
    )
    db.commit()

def repo_restore_application(application_id: str):
    """Восстанавливает заявку и возвращает ее данные."""
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
    Получает список заявок по статусу (с пагинацией) и обогащает URL-ами фото.
    Возвращает (список заявок, общее кол-во страниц).
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
        
        app_data['photo_urls'] = [url_for('uploaded_file', filename=p) for p in photo_paths]
        applications.append(app_data)
        
    return applications, total_pages



def repo_get_raw_apps_for_export() -> list:
    """Получает "сырые" данные выполненных заявок для экспорта в Excel."""
    db = get_db()
    cur = db.execute("SELECT * FROM applications WHERE status = 'done'")
    rows = cur.fetchall()
    return [dict(row) for row in rows]

def repo_delete_application(application_id: int) -> list:
    """Удаляет заявку по ID и возвращает список ее фото для удаления."""
    db = get_db()
    cur = db.execute("SELECT photos FROM applications WHERE id = ?", (application_id,))
    row = cur.fetchone()
    
    db.execute("DELETE FROM applications WHERE id = ?", (application_id,))
    db.commit()
    
    if row and row['photos']:
        try:
            return json.loads(row['photos'])
        except (json.JSONDecodeError, TypeError):
            pass
    return []

def repo_update_photo(application_id: str, username: str, new_filename: str) -> bool:
    """
    Обновляет фото для заявки, ДОБАВЛЯЯ новое фото к списку.
    Проверяет владельца. Возвращает True при успехе.
    """
    db = get_db()
    cur = db.execute(
        "SELECT id, photos FROM applications WHERE application_id=? AND lower(username)=lower(?)", 
        (application_id, username)
    )
    row = cur.fetchone()
    
    if not row:
        return False
    
    try:
        current_photos = json.loads(row['photos'] or '[]')
    except (json.JSONDecodeError, TypeError):
        current_photos = []
        
    if new_filename not in current_photos:
        current_photos.append(new_filename)
    
    new_photos_json = json.dumps(current_photos)
    
    db.execute("UPDATE applications SET photos=? WHERE application_id=?", 
               (new_photos_json, application_id))
    db.commit()
    return True

def repo_append_details(application_id: str, username: str, extra_text: str) -> bool:
    """Дополняет текст заявки, проверяя владельца. Возвращает True при успехе."""
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

@app.route('/', methods=['GET'])
def home():
    """Главная страница (веб-интерфейса)."""
    return render_template('index.html')

@app.route('/applications', methods=['POST'])
def add_application():
    """API: Добавить новую заявку (от бота)."""
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
        'emiac': data.get('emiac'),
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
    """API: Восстановить заявку (от админа в боте)."""
    user_data = repo_restore_application(application_id)
    if not user_data:
        return jsonify({'error': 'Заявка не найдена'}), 404
    
    return jsonify(user_data), 200

@app.route('/set_difficulty/<int:application_id>', methods=['POST'])
def set_difficulty(application_id):
    """WEB: Установить сложность (из веб-интерфейса)."""
    new_difficulty = request.form.get('difficulty')
    if new_difficulty not in ['low', 'medium', 'high']:
        return "Некорректное значение сложности", 400
    
    repo_set_difficulty(application_id, new_difficulty)
    return redirect(request.referrer or url_for('get_active_applications'))

@app.route('/applications')
def get_active_applications():
    """WEB: Показать страницу с активными заявками (с пагинацией)."""
    
    page = request.args.get('page', 1, type=int)
    if page < 1:
        page = 1
        
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

@app.route('/archive')
def get_archive():
    """WEB: Показать страницу с архивом заявок (с пагинацией)."""
    
    page = request.args.get('page', 1, type=int)
    if page < 1:
        page = 1
    
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


@app.route('/export_archive')
def export_archive():
    """WEB: Экспорт архива в Excel."""
    raw_rows = repo_get_raw_apps_for_export()
    if not raw_rows:
        return "Нет архивных заявок", 404
    
    df = pd.DataFrame(raw_rows)
    df = df[['application_id', 'name', 'department', 'details', 'created_at', 'archived_at', 'done_by']]
    df = df.rename(columns={
        'application_id': 'ID', 'name': 'ФИО', 'department': 'Отделение',
        'details': 'Проблема', 'created_at': 'Создание заявки',
        'archived_at': 'Дата выполнения', 'done_by': 'Исполнитель'
    })
    for col in ['Создание заявки', 'Дата выполнения']:
        df[col] = pd.to_datetime(df[col], format='%Y-%m-%d %H:%M', errors='coerce').dt.strftime('%d-%m-%Y %H:%M')
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Archive')
    output.seek(0)
    
    return send_file(
        output,
        as_attachment=True,
        download_name='archive.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/delete/<int:application_id>', methods=['POST'])
def delete_application(application_id):
    """WEB: Удалить заявку (из архива)."""
    filenames_to_delete = repo_delete_application(application_id)
    
    file_service.delete_photos(filenames_to_delete)
    
    return redirect(request.referrer or url_for('get_archive'))

@app.route('/update_photo', methods=['POST'])
def update_photo():
    """API: Обновить фото (от бота)."""
    data = request.get_json()
    application_id = data.get('application_id')
    username = data.get('username')
    photo_b64 = data.get('photo')
    
    if not all([application_id, username, photo_b64]):
        return jsonify({'error': 'Missing required fields'}), 400
    
    filename = f"{application_id}_updated_{int(datetime.now().timestamp())}.jpg"
    
    saved_name = file_service.save_photo_from_b64(photo_b64, filename)
    if not saved_name:
         return jsonify({'error': 'Failed to save photo'}), 500

    success = repo_update_photo(application_id, username, saved_name)
    
    if not success:
        file_service.delete_photos([saved_name])
        return jsonify({'error': 'Заявка не найдена или не принадлежит вам'}), 404
        
    return jsonify({'message': 'Фото обновлено'}), 200

@app.route('/append_details', methods=['POST'])
def append_details():
    """API: Дополнить текст заявки (от бота)."""
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

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """WEB: Отдать сохраненный файл (для <img src=...>)."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


if __name__ == '__main__':
    with app.app_context():
        init_db()
    
    app.run(host='0.0.0.0', port=5000, debug=True)