@echo off
cd /d %~dp0
poetry run python server/server.py
pause