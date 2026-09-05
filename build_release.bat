@echo off
chcp 65001 >nul
rem ============================================================
rem  Сборка «Зевс v1.3» -> dist\Zeus -> release\Zeus\v1.3
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
if exist build_log.txt del build_log.txt
if exist pyi_log.txt del pyi_log.txt

echo [2/5] Сборка через PyInstaller (zeus.spec)...
if "%1"=="--console" (
    python -m PyInstaller zeus.spec --noconfirm > pyi_log.txt 2>&1
) else (
    python -m PyInstaller zeus.spec --noconfirm > pyi_log.txt 2>&1
)
if errorlevel 1 (
    echo [ОШИБКА] Сборка не удалась, см. pyi_log.txt
    exit /b 1
)

echo [3/5] Копирование ресурсов data/ и models/ в Zeus\v1.3...
if not exist dist\Zeus\Zeus.exe (
    echo [ОШИБКА] PyInstaller не создал dist\Zeus\Zeus.exe
    exit /b 1
)
robocopy data dist\Zeus\data /E >nul
robocopy models dist\Zeus\models /E >nul

echo [4/5] Перенос в release\Zeus\v1.3...
if exist release\Zeus rmdir /s /q release\Zeus
if exist release\Zeus (
    echo [ОШИБКА] Не удалось удалить старую папку release\Zeus - она занята!
    exit /b 1
)
mkdir release\Zeus\v1.3
robocopy dist\Zeus release\Zeus\v1.3 /E /MOVE /NFL /NDL /NJH /NJS >nul
if errorlevel 8 (
    echo [ОШИБКА] robocopy переноса завершился с ошибкой.
    exit /b 1
)

echo [5/5] Проверка...
if not exist release\Zeus\v1.3\Zeus.exe (
    echo [ОШИБКА] exe не найден!
    exit /b 1
)
echo [OK] Сборка v1.3 успешно перемещена в папку release\Zeus\v1.3
echo      Запуск: release\Zeus\v1.3\Zeus.exe
endlocal
