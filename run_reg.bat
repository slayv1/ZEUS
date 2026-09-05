@echo off
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
python check_improvements.py 2>err.txt
echo EXIT=%ERRORLEVEL%