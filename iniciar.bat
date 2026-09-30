@echo off
chcp 65001 >nul
title IKIGAI VIEDMA - App
echo.
echo  ============================================
echo    IKIGAI VIEDMA - App de la academia
echo  ============================================
echo.

cd /d "%~dp0"

REM --- Verificar Python ---
where py >nul 2>&1
if errorlevel 1 (
  echo  [ERROR] No se encontro Python.
  echo  Instala Python desde https://www.python.org/downloads/
  echo  IMPORTANTE: marca la casilla "Add Python to PATH" al instalar.
  pause
  exit /b
)

REM --- Instalar dependencias si falta Flask ---
py -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo  Instalando dependencias (solo la primera vez)...
  py -m pip install flask pywebpush pillow
)

echo.
echo  Iniciando la app...
for /f %%i in ('powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 ^| Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*' } ^| Select-Object -First 1).IPAddress"') do set LANIP=%%i
echo  Abri el navegador en:  http://localhost:5000
echo  Desde el celular (misma red WiFi):  http://%LANIP%:5000
echo  Usuario admin:  admin   -   Contrasena:  admin123
echo.
echo  Presiona Ctrl+C para detener la app.
echo.

start "" http://localhost:5000
py app.py
pause

