#!/bin/bash
# Запуск сервера API в фоне
python server/server.py &
# Ожидание запуска сервера
sleep 5
# Запуск бота
python bot/bot1.py