@echo off
cd /d "%~dp0"
del /F /Q ".git\index.lock" 2>nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish_beta.ps1" -AllowCleanWorktree > "%~dp0_publish_log.txt" 2>&1
echo --- exit code: %ERRORLEVEL% --- >> "%~dp0_publish_log.txt"
