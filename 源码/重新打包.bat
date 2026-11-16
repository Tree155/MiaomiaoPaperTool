@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

rem 优先用本目录 .venv，其次上级 .venv，最后系统 Python
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe"        set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0..\.venv\Scripts\python.exe"     set "PY=%~dp0..\.venv\Scripts\python.exe"

echo 使用的 Python: %PY%
"%PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo [X] 没找到 PyInstaller。
  echo     先装依赖：
  echo       "%PY%" -m pip install -r requirements.txt pyinstaller
  echo.
  pause
  exit /b 1
)

echo.
echo 正在打包，约 1 分钟...
echo.
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name MiaomiaoTool --icon "%~dp0app.ico" --add-data "%~dp0ui.html;." ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import clr_loader --hidden-import pythonnet ^
  --collect-submodules pypdf --collect-submodules fontTools desktop.py

echo.
if exist "dist\MiaomiaoTool.exe" (
  copy /y "dist\MiaomiaoTool.exe" "..\文献妙妙小工具.exe" >nul
  echo [OK] 已生成 ..\文献妙妙小工具.exe
  rmdir /s /q build 2>nul
  del /q MiaomiaoTool.spec 2>nul
) else (
  echo [X] 打包失败，看上面报错
)
echo.
pause
