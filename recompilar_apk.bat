@echo off
title IKIGAI VIEDMA - Recompilar APK
echo.
echo  =====================================================
echo    IKIGAI VIEDMA - Generar el APK de nuevo
echo  =====================================================
echo.
echo  Necesitas internet la primera vez. Luego compila sin red.
echo.

set JAVA_HOME=C:\Program Files\Eclipse Adoptium\jdk-17.0.20.8-hotspot
set ANDROID_HOME=C:\Android
set PATH=%JAVA_HOME%\bin;C:\Android\platform-tools;%PATH%

cd /d C:\Android\IkigaiApp

call C:\Gradle\gradle-8.7\bin\gradle.bat assembleDebug --no-daemon --console=plain
if errorlevel 1 (
  echo.
  echo  [ERROR] No se pudo compilar. Revisa el mensaje de arriba.
  pause
  exit /b
)

copy /y app\build\outputs\apk\debug\app-debug.apk "%~dp0IKIGAI-VIEDMA.apk" >nul
echo.
echo  LISTO: %~dp0IKIGAI-VIEDMA.apk
echo  Mandalo al celular con un cable USB, WhatsApp o Google Drive.
echo.
pause
