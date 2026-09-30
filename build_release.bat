@echo off
chcp 65001 >nul
rem ============================================================
rem  Сборка «Зевс v1.4» -> dist\Zeus -> release\Zeus\v1.4
rem  Использование: build_release.bat [--console]
rem    --console : собрать с консолью (отладочный билд)
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo [1/5] Очистка старых сборок...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release rmdir /s /q release
if exist build_done.txt del build_done.txt
if exist build_log.txt del build_log.txt
if exist pyi_log.txt del pyi_log.txt

rem Лог ключевых шагов: robocopy-перенос и сбои с ретраями (см. шаг 4/5).
rem Чтобы сбой переноса не был «молчаливым» — все повторы и итог пишутся сюда.
echo [%date% %time%] Сборка v1.4 запущена > build_log.txt

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

echo [3/5] Копирование ресурсов data/ и models/ в Zeus\v1.4...
if not exist dist\Zeus\Zeus.exe (
    echo [ОШИБКА] PyInstaller не создал dist\Zeus\Zeus.exe
    exit /b 1
)
rem zeus.log / settings.json.bak / index_meta.json — рабочие файлы конкретной
rem установки; папку logs/ (личные логи сессий и крашей пользователя) в релиз
rem тоже не переносим — всё это создастся при первом запуске.
robocopy data dist\Zeus\data /E /XD logs /XF zeus.log settings.json.bak index_meta.json >nul
robocopy models dist\Zeus\models /E >nul

echo [4/5] Перенос в release\Zeus\v1.4...
rem Папку release\Zeus иногда держат антивирус или только что закрытый Zeus.exe —
rem поэтому удаление и robocopy повторяем до 5 раз с паузой ~2 с.
rem Раньше bat падал здесь «молча» (файл оставался в dist) — теперь каждый
rem шаг логируется в build_log.txt.
set /a retry=0
:clean_release
if exist release\Zeus rmdir /s /q release\Zeus
if exist release\Zeus (
    set /a retry+=1
    if !retry! GEQ 5 (
        echo [ОШИБКА] Папка release\Zeus занята ^(закройте Zeus.exe^) - детали в build_log.txt
        echo [%date% %time%] FAILED: release\Zeus is locked ^(retries exhausted^) >> build_log.txt
        exit /b 1
    )
    echo        release\Zeus занята, повтор !retry!/5 через 2 с...
    echo [%date% %time%] retry !retry!/5: release\Zeus is locked >> build_log.txt
    ping -n 3 127.0.0.1 >nul
    goto clean_release
)
mkdir release\Zeus\v1.4
set /a retry=0
:move_dist
robocopy dist\Zeus release\Zeus\v1.4 /E /MOVE /NFL /NDL /NJH >> build_log.txt
if errorlevel 8 (
    set /a retry+=1
    if !retry! GEQ 5 (
        echo [ОШИБКА] robocopy не перенёс файлы - детали в build_log.txt
        echo [%date% %time%] FAILED: robocopy error ^(retries exhausted^) >> build_log.txt
        exit /b 1
    )
    echo        robocopy сбой, повтор !retry!/5 через 2 с...
    ping -n 3 127.0.0.1 >nul
    goto move_dist
)
echo [%date% %time%] robocopy OK ^(see summary above^) >> build_log.txt

echo [5/5] Проверка...
if not exist release\Zeus\v1.4\Zeus.exe (
    echo [ОШИБКА] exe не найден!
    echo [%date% %time%] FAILED: Zeus.exe missing after move >> build_log.txt
    exit /b 1
)
echo [%date% %time%] OK: release\Zeus\v1.4 готов >> build_log.txt
echo [OK] Сборка v1.4 успешно перемещена в папку release\Zeus\v1.4
echo      Запуск: release\Zeus\v1.4\Zeus.exe
endlocal
