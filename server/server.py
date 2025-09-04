from flask import Flask, request, jsonify, send_from_directory
import os

app = Flask(__name__)
tickets = []

@app.route('/add_ticket', methods=['POST'])
def add_ticket():
    data = request.form or request.json
    ticket_id = data.get('ticket_id')
    text = data.get('text') or data.get('caption')
    photos = request.files.getlist('photos')
    photo_paths = []
    if photos:
        for idx, photo in enumerate(photos, 1):
            photo_path = f"photos/{ticket_id}_{idx}.jpg"
            photo.save(photo_path)
            photo_paths.append(photo_path)
    tickets.append({
        "ticket_id": ticket_id,
        "text": text,
        "photos": photo_paths
    })
    return jsonify({"status": "ok"})

@app.route('/')
def index():
    html = "<h1>Заявки</h1><ul>"
    for t in tickets:
        html += f"<li><b>{t['ticket_id']}</b>: {t['text']}"
        if t.get('photos'):
            for photo_path in t['photos']:
                html += f"<br><img src='/{photo_path}' width=200>"
        html += "</li>"
    html += "</ul>"
    return html

@app.route('/photos/<filename>')
def photos(filename):
    return send_from_directory('photos', filename)

if __name__ == '__main__':
    os.makedirs('photos', exist_ok=True)
    app.run(debug=True)
