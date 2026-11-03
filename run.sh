#!/bin/bash
# 一键启动脚本（Git Bash / WSL / Linux / macOS 通用）
cd "$(dirname "$0")"
echo "Starting Hospital Maintenance Work Order System (FastAPI)..."
echo "Open http://localhost:5000 in your browser"
# 数据库在 .env 的 DATABASE_URL 中配置（默认 MySQL）。
# 生产建议：uvicorn main:app --host 0.0.0.0 --port 5000 --workers 4
exec ./python/python.exe -m uvicorn main:app --host 0.0.0.0 --port 5000
