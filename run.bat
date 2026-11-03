@echo off
cd /d "%~dp0"
echo Starting Hospital Maintenance Work Order System (FastAPI)...
echo Open http://localhost:5000 in your browser
rem 数据库在 .env 的 DATABASE_URL 中配置（默认 MySQL）。
rem 生产建议：uvicorn main:app --host 0.0.0.0 --port 5000 --workers 4
python\python.exe -m uvicorn main:app --host 0.0.0.0 --port 5000 --reload
pause
