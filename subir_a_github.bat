@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  SUBIENDO NEXO MADRYN A GITHUB
echo ============================================
echo.
echo  Paso 1 de la subida: creando el commit...
git add .
git commit -m "actualizacion"
git branch -M main
echo.
echo  Paso 2: conectando con tu repositorio de GitHub...
git remote remove origin 2>nul
git remote add origin https://github.com/isaiasreyesbjj16-ux/Nexo-Madryn.git
echo.
echo  Paso 3: subiendo el codigo...
echo  (Si aparece una ventana de GitHub, inicia sesion y acepta)
echo.
echo  Paso 3a: trayendo cambios del remoto si los hay (evita el error "fetch first")...
git pull --rebase origin main
echo  (si no habia cambios nuevos, sigue normal)
echo.
echo  Paso 3b: subiendo el codigo...
git push -u origin main
echo.
if %errorlevel%==0 (
  echo ============================================
  echo  OK: el codigo ya esta en GitHub.
  echo ============================================
) else (
  echo ============================================
  echo  ERROR: algo fallo. Copia el mensaje de arriba.
  echo  Comprobaciones:
  echo   - Creaste el repo vacio en https://github.com/new
  echo   - Iniciaste sesion en la ventana de GitHub
  echo ============================================
)
echo.
pause
