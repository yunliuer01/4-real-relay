@echo off
rem GUI sensor simulator launcher
rem Uses system Python (has tkinter) + venv packages (paho-mqtt)
cd /d D:\code-1\mqtt-iot-terminal
set PYTHONPATH=C:\Users\yunliu\.workbuddy\binaries\python\envs\default\Lib\site-packages
start "" "C:\Users\yunliu\AppData\Local\Microsoft\WindowsApps\python.exe" gui_sensor.py
