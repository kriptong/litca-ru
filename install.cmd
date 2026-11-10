@echo off
rem litca-ru — запуск установщика локальной нейросети.
rem Двойной клик по этому файлу. Права администратора не нужны.
rem Все аргументы передаются в install.ps1, например:
rem     install.cmd -Lite
rem     install.cmd -Model "gemma4:12b" -ContextLength 16384

setlocal
set "SCRIPT_DIR=%~dp0"
set "PS1=%SCRIPT_DIR%install.ps1"
set "EXITCODE=0"

if not exist "%PS1%" (
    echo.
    echo [ОШИБКА] Рядом с этим файлом нет install.ps1
    echo Положите install.cmd и install.ps1 в одну папку и запустите снова.
    echo.
    pause
    exit /b 1
)

set "PS=powershell.exe"
where pwsh.exe >nul 2>&1 && set "PS=pwsh.exe"

echo Запускаю установку через %PS% ...
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo Установка завершена успешно.
) else (
    echo Скрипт завершился с кодом %EXITCODE%.
    echo. Посмотрите сообщения выше: там написано, что именно не получилось.
)
echo.
pause
exit /b %EXITCODE%
