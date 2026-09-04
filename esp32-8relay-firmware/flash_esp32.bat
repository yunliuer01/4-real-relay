@echo off
setlocal EnableDelayedExpansion

rem Flash script for ESP32-C3 8-relay MicroPython firmware
rem Usage: flash_esp32.bat COMx
rem Example: flash_esp32.bat COM3

set FIRMWARE=D:\8-relay\esp32-8relay-firmware\LOLIN_C3_MINI-20241025-v1.24.0.bin
set MAINPY=D:\8-relay\esp32-8relay-firmware\main.py
set PYTHON=C:\Users\yunliu\.workbuddy\binaries\python\envs\default\Scripts\python.exe

if "%~1"=="" (
    echo Usage: flash_esp32.bat COMx
    echo Example: flash_esp32.bat COM3
    echo.
    echo Available ports:
    wmic path Win32_SerialPort Get DeviceID,Description 2^>nul
    exit /b 1
)

set PORT=%~1

echo.
echo [1/3] Erasing flash on %PORT%
"%PYTHON%" -m esptool --port %PORT% --chip esp32-c3 erase_flash
if errorlevel 1 (
    echo Erase failed. Make sure the board is in download mode.
    exit /b 1
)

echo.
echo [2/3] Writing MicroPython firmware
"%PYTHON%" -m esptool --port %PORT% --chip esp32-c3 --baud 460800 write_flash -z 0x0 "%FIRMWARE%"
if errorlevel 1 (
    echo Firmware write failed. Try lowering baud: --baud 115200
    exit /b 1
)

echo.
echo [3/3] Waiting for board to reset, then uploading main.py
timeout /t 3 /nobreak >nul
"%PYTHON%" -m mpremote connect %PORT% fs cp "%MAINPY%" :main.py
if errorlevel 1 (
    echo main.py upload failed. Reset the board and run:
    echo   %PYTHON% -m mpremote connect %PORT% fs cp "%MAINPY%" :main.py
    exit /b 1
)

echo.
echo Done. Reset or power-cycle the board if it does not start automatically.
echo Then connect to WiFi hotspot "Relay8-Setup" and open http://192.168.4.1
