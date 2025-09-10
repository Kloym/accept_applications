import os
import base64
import sqlite3
import json
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, render_template, redirect, url_for, request

app = Flask(__name__)
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/', methods=['GET'])
def home():
    return render_template('index.html')

def get_db():
    conn = sqlite3.connect('applications.db')
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/applications', methods=['POST'])
def add_application():
    data = request.get_json()
    name = data.get('name')
    ip = data.get('ip')
    emiac = data.get('emiac')
    department = data.get('department')
    details = data.get('details')
    application_id = data.get('application_id')
    photos_b64 = data.get('photos', [])
    username = data.get('username')
    chat_id = data.get('chat_id')
    status = data.get('status', 'active')

    photo_paths = []
    for idx, photo_b64 in enumerate(photos_b64):
        filename = f"{application_id}_{idx}.jpg"
        photo_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(photo_path, "wb") as f:
            f.write(base64.b64decode(photo_b64))
        photo_paths.append(filename)

    created_at = datetime.now().strftime('%Y-%m-%d %H:%M')

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO applications (name, department, details, username, photos, application_id, chat_id, status, ip, emiac, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, department, details, username, json.dumps(photo_paths), application_id, chat_id, status, ip, emiac, created_at)
    )
    conn.commit()
    conn.close()
    return jsonify({'message': 'Заявка добавлена'}), 201

@app.route('/applications')
def get_active_applications():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM applications WHERE status = 'active'")
    rows = cur.fetchall()
    applications = []
    for row in rows:
        app_data = dict(row)
        photo_paths = json.loads(app_data.get('photos', '[]'))
        app_data['photo_urls'] = [f"/uploads/{p}" for p in photo_paths]
        applications.append(app_data)
    conn.close()
    return render_template('applications.html', applications=applications, archive=False)

@app.route('/archive')
def get_archive():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM applications WHERE status = 'done'")
    rows = cur.fetchall()
    applications = []
    for row in rows:
        app_data = dict(row)
        photo_paths = json.loads(app_data.get('photos', '[]'))
        app_data['photo_urls'] = [f"/uploads/{p}" for p in photo_paths]
        applications.append(app_data)
    conn.close()
    return render_template('applications.html', applications=applications, archive=True)

@app.route('/delete/<int:application_id>', methods=['POST'])
def delete_application(application_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT photos FROM applications WHERE id = ?", (application_id,))
    row = cur.fetchone()
    if row and row['photos']:
        import json
        photo_paths = json.loads(row['photos'])
        for path in photo_paths:
            full_path = os.path.join(UPLOAD_FOLDER, path)
            if os.path.exists(full_path):
                os.remove(full_path)
    cur.execute("DELETE FROM applications WHERE id = ?", (application_id,))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for('get_applications'))

@app.route('/update_photo', methods=['POST'])
def update_photo():
    data = request.get_json()
    application_id = data.get('application_id')
    username = data.get('username')
    photo_b64 = data.get('photo')
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM applications WHERE application_id=? AND lower(username)=lower(?)", (application_id, username))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Заявка не найдена или не принадлежит вам'}), 404
    filename = f"{application_id}_updated.jpg"
    photo_path = os.path.join(UPLOAD_FOLDER, filename)
    with open(photo_path, "wb") as f:
        f.write(base64.b64decode(photo_b64))
    cur.execute("UPDATE applications SET photos=? WHERE application_id=?", (json.dumps([filename]), application_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Фото обновлено'}), 200

@app.route('/append_details', methods=['POST'])
def append_details():
    data = request.get_json()
    application_id = data.get('application_id')
    username = data.get('username')
    extra_text = data.get('extra_text')

    print("application_id:", application_id, "username:", username)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT details FROM applications WHERE application_id=? AND lower(username)=lower(?)", (application_id, username))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Заявка не найдена или не принадлежит вам'}), 404

    old_details = row['details'] or ""
    new_details = f'{old_details}\n{extra_text}'
    cur.execute("UPDATE applications SET details=? WHERE application_id=?", (new_details, application_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Текст заявки дополнен'}), 200

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

if __name__ == '__main__':
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id TEXT,
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
        archived_at TEXT
    )''')
    conn.close()
    app.run(debug=True)
