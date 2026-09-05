@echo off
chcp 65001 >nul
title Чистая сборка Zeus (Flet)

echo ============================================
echo  Чистая сборка Zeus v1.1 (Flet)
echo ============================================
echo.

:: Шаг 1: Очистка старых сборок
echo [1/4] Очистка старых сборок...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
echo   Готово.
echo.

:: Шаг 2: Создание папки release с подпапкой data
echo [2/4] Создание папки release...
if not exist release mkdir release
if not exist release\data mkdir release\data
echo   Готово.
echo.

:: Шаг 3: Сборка .exe через PyInstaller
echo [3/4] Сборка Zeus.exe...
echo   Используются флаги:
echo     --noconsole (без окна консоли)
echo     --onefile (один файл)
echo     --paths src (доступ к пакетам core, modules, ui)
echo     --collect-all flet
echo     --collect-all flet_desktop
echo     --add-data data/commands.json;data
echo     --add-data data/index.json;data
echo     --add-data data/settings.json;data

python -m PyInstaller --noconsole --onefile --name="Zeus" --clean ^
    --paths src ^
    --collect-all flet --collect-all flet_desktop ^
    --add-data "data\commands.json;data" ^
    --add-data "data\index.json;data" ^
    --add-data "data\settings.json;data" ^
    src\ui\main_flet.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ОШИБКА] Сборка не удалась! Код ошибки: %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)
echo   Сборка завершена успешно.
echo.

:: Шаг 4: Копирование в release
echo [4/4] Копирование в папку release...
copy /Y "dist\Zeus.exe" "release\Zeus.exe" >nul
copy /Y "data\commands.json" "release\data\commands.json" >nul
copy /Y "data\index.json" "release\data\index.json" >nul
copy /Y "data\settings.json" "release\data\settings.json" >nul
copy /Y "data\file_cache.json" "release\data\file_cache.json" >nul
echo   Готово.
echo.

echo ============================================
echo  Сборка завершена!
echo  Файлы в папке release/
echo ============================================
echo.
echo  Zeus.exe — готовый исполняемый файл
echo  data\commands.json — команды приложений
echo  data\index.json — индекс программ
echo  data\settings.json — настройки
echo  data\file_cache.json — кэш файлов
echo.
echo  ВАЖНО: Все .json файлы должны лежать в папке data\ рядом с .exe!
echo.
echo  (при необходимости положите файл zeus.ico рядом с Zeus.exe
echo   или в src\, чтобы задать иконку окна)
echo.

pause
