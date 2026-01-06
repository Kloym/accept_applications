#!/bin/bash

export PYTHONPATH=$PYTHONPATH:$(pwd)/bot:$(pwd)/server

# 2. Создаем папку для фото в вечном хранилище
mkdir -p /data/uploads

# 3. Запускаем сервер
python server/server.py &

# 4. Ждем
sleep 5

# 5. Запускаем бота
python bot/bot.py