@echo off
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM cloudflared.exe >nul 2>&1
cd /d D:\Game_dla_mane\gift_radar_web
start "Tunnel" cmd /k "cloudflared tunnel --url http://127.0.0.1:8080"
start http://127.0.0
python app.py
