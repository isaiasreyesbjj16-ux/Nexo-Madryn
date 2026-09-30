@echo off
chcp 65001 >nul
title IKIGAI VIEDMA - Modo NUBE
echo.
echo  =============================================================
echo    IKIGAI VIEDMA - Modo NUBE (HTTPS, para cualquier lugar)
echo  =============================================================
echo.
echo  Necesitas: esta PC encendida + internet.
echo  La URL es GRATIS pero CAMBIA en cada inicio. Para una URL
echo  fija, mira el archivo PYTHONANYWHERE.txt (unico modo 24/7).
echo.

cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
  echo  [ERROR] No se encontro Python.
  pause
  exit /b
)

py -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo  Instalando dependencias (solo la primera vez)...
  py -m pip install flask pywebpush pillow
)

if not exist cloudflared.exe (
  echo  [ERROR] Falta cloudflared.exe en esta carpeta.
  echo  Bajalo de https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
  pause
  exit /b
)

REM --- Arrancar la app en una ventana aparte (minimizada) ---
start "IKIGAI App" /min cmd /k "cd /d ""%~dp0"" && py app.py"
timeout /t 4 /nobreak >nul

echo.
echo  Levantando el tunel HTTPS de Cloudflare...
echo  Busca abajo la linea que dice:  https://algo.trycloudflare.com
echo  Ese es el link para compartir / instalar en el celular.
echo.
echo  >  IMPORTANTE: al terminar, cierra ESTA ventana con Ctrl+C
echo  >  (solo se apaga el tunel; la app sigue corriendo).
echo.
cloudflared.exe tunnel --url http://localhost:5000
pause
