import os
import base64
import sqlite3
import json
from flask import Flask, request, jsonify, send_from_directory, render_template, redirect, url_for

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
    department = data.get('department')
    details = data.get('details')
    chat_id = data.get('chat_id')
    photos_b64 = data.get('photos', [])

    photo_paths = []
    for idx, photo_b64 in enumerate(photos_b64):
        filename = f"{chat_id}_{len(details)}_{idx}.jpg"
        photo_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(photo_path, "wb") as f:
            f.write(base64.b64decode(photo_b64))
        photo_paths.append(filename)

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO applications (name, department, details, chat_id, photos) VALUES (?, ?, ?, ?, ?)",
        (name, department, details, chat_id, json.dumps(photo_paths))
    )
    conn.commit()
    conn.close()
    return jsonify({'message': 'Заявка добавлена'}), 201

@app.route('/applications', methods=['GET'])
def get_applications():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM applications")
    rows = cur.fetchall()
    applications = []
    for row in rows:
        app_data = dict(row)
        photo_paths = json.loads(app_data.get('photos', '[]'))
        app_data['photo_urls'] = [f"/uploads/{p}" for p in photo_paths]
        applications.append(app_data)
    conn.close()
    return render_template('applications.html', applications=applications)

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
    return redirect(url_for('get_applications'))

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

if __name__ == '__main__':
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        department TEXT,
        details TEXT,
        chat_id TEXT,
        photos TEXT
    )''')
    conn.close()
    app.run(debug=True)
