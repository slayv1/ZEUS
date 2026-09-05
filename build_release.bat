@echo off
chcp 65001 >nul
rem ============================================================
rem  Сборка «Зевс v1.2» -> dist -> release\ZeusAssistant
rem  Использование: build_release.bat [--console]
rem    --console : собрать с консолью (отладочный билд)
rem ============================================================
setlocal
cd /d "%~dp0"

echo [1/5] Очистка старых сборок...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release rmdir /s /q release
if exist build_done.txt del build_done.txt

echo [2/5] Сборка через PyInstaller (zeus.spec)...
if "%1"=="--console" (
    python -m PyInstaller zeus.spec --noconfirm > pyi_log.txt 2>&1
) else (
    python -m PyInstaller zeus.spec --noconfirm > pyi_log.txt 2>&1
)
if errorlevel 1 (
    echo [ОШИБКА] Сборка не удалась, см. build_log.txt
    exit /b 1
)

echo [3/5] Копирование ресурсов data/ и models/ рядом с exe...
robocopy data dist\ZeusAssistant\data /E >nul
robocopy models dist\ZeusAssistant\models /E >nul

echo [4/5] Перенос в release/...
if exist release\ZeusAssistant rmdir /s /q release\ZeusAssistant
if exist release\ZeusAssistant (
    echo [ОШИБКА] Не удалось удалить старую папку release\ZeusAssistant - она занята!
    exit /b 1
)
robocopy dist\ZeusAssistant release\ZeusAssistant /E /MOVE /NFL /NDL /NJH /NJS >nul
if errorlevel 8 (
    echo [ОШИБКА] robocopy переноса завершился с ошибкой.
    exit /b 1
)

echo [5/5] Проверка...
if not exist release\ZeusAssistant\ZeusAssistant.exe (
    echo [ОШИБКА] exe не найден!
    exit /b 1
)
echo [OK] Сборка v1.2 успешно перемещена в папку release\ZeusAssistant
echo      Запуск: release\ZeusAssistant\ZeusAssistant.exe
endlocal
