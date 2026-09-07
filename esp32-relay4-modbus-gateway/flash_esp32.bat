@echo off
setlocal EnableDelayedExpansion

rem Flash script for ESP32-C3 4-relay + Modbus gateway MicroPython firmware
rem Usage: flash_esp32.bat COMx
rem Example: flash_esp32.bat COM3

set BASE=D:\8-relay\esp32-relay4-modbus-gateway
set FIRMWARE=%BASE%\LOLIN_C3_MINI-20241025-v1.24.0.bin
set MAINPY=%BASE%\main.py
set MODBUSPY=%BASE%\modbus_master.py
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
echo [1/4] Erasing flash on %PORT%
"%PYTHON%" -m esptool --port %PORT% --chip esp32-c3 erase_flash
if errorlevel 1 (
    echo Erase failed. Make sure the board is in download mode.
    exit /b 1
)

echo.
echo [2/4] Writing MicroPython firmware
"%PYTHON%" -m esptool --port %PORT% --chip esp32-c3 --baud 460800 write_flash -z 0x0 "%FIRMWARE%"
if errorlevel 1 (
    echo Firmware write failed. Try lowering baud: --baud 115200
    exit /b 1
)

echo.
echo [3/4] Waiting for board to reset, then uploading modbus_master.py
timeout /t 3 /nobreak >nul
"%PYTHON%" -m mpremote connect %PORT% fs cp "%MODBUSPY%" :modbus_master.py
if errorlevel 1 (
    echo modbus_master.py upload failed. Reset the board and run:
    echo   %PYTHON% -m mpremote connect %PORT% fs cp "%MODBUSPY%" :modbus_master.py
    exit /b 1
)

echo.
echo [4/4] Uploading main.py
"%PYTHON%" -m mpremote connect %PORT% fs cp "%MAINPY%" :main.py
if errorlevel 1 (
    echo main.py upload failed. Reset the board and run:
    echo   %PYTHON% -m mpremote connect %PORT% fs cp "%MAINPY%" :main.py
    exit /b 1
)

echo.
echo Done. Reset or power-cycle the board if it does not start automatically.
echo Then connect to WiFi hotspot "Relay4-Setuplfx" and open http://192.168.4.1
