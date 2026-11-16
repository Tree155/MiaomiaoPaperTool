@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [X] python not found in PATH.
  pause
  exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo [X] PyInstaller is missing. Run:  pip install pyinstaller
  pause
  exit /b 1
)

echo Building MiaomiaoTool.exe (about 40 seconds)...
echo.
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name MiaomiaoTool --icon "..\app.ico" --add-data "ui.html;." ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import clr_loader --hidden-import pythonnet ^
  --collect-submodules pypdf --collect-submodules fontTools ^
  desktop.py

echo.
if exist "dist\MiaomiaoTool.exe" (
  copy /y "dist\MiaomiaoTool.exe" "..\..\MiaomiaoTool.exe" >nul
  echo [OK] MiaomiaoTool.exe written to the repository root.
  rmdir /s /q build 2>nul
  del /q MiaomiaoTool.spec 2>nul
) else (
  echo [X] Build failed, see the log above.
)
echo.
pause
