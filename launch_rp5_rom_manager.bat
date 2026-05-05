@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    python rp5_rom_manager.py
) else (
    py -3 rp5_rom_manager.py
)
