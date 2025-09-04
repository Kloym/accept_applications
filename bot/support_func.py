import os
import requests

media_groups = {}

async def send_album_to_server(group_id, context):
    album = media_groups.pop(group_id, None)
    if not album:
        return
    ticket_id = album['ticket_id']
    caption = album['caption']
    files = []
    for file_path in album['photos']:
        files.append(('photos', (os.path.basename(file_path), open(file_path, 'rb'), 'image/jpeg')))
    data = {'ticket_id': ticket_id, 'caption': caption}
    requests.post(
        "http://127.0.0.1:5000/add_ticket",
        data=data,
        files=files
    )