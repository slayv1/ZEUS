@echo off
cd /d "c:\Users\admin\Desktop\Проекты\ИИ\Zeus v1.1"
set PYTHONPATH=src
python -u main.py > launch_out.txt 2> launch_err.txt
echo LAUNCH_EXIT=%ERRORLEVEL%