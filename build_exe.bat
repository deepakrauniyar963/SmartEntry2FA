@echo off
REM ============================================================
REM  Build SmartEntry2FA as a Windows application (no Python needed to run it)
REM  Run this from the project folder, inside the same virtual env you use for the project.
REM ============================================================

pip install pyinstaller

pyinstaller --noconfirm --clean --onedir --windowed ^
  --name SmartEntry2FA --icon=logo.ico ^
  --collect-all face_recognition ^
  --collect-all face_recognition_models ^
  --collect-all dlib ^
  --collect-all cv2 ^
  --collect-all customtkinter ^
  --collect-all tkcalendar ^
  --hidden-import babel.numbers ^
  --hidden-import win32com.client ^
  --hidden-import pythoncom ^
  SmartEntry2FA_Main.py

IF %ERRORLEVEL% NEQ 0 (
  echo.
  echo BUILD FAILED - read the error above.
  pause
  exit /b 1
)

REM The app reads its settings from a .env file placed NEXT TO SmartEntry2FA.exe
IF EXIST ".env" (
  copy /Y ".env" "dist\SmartEntry2FA\.env" >nul
  echo Copied your .env next to the exe.
) ELSE (
  copy /Y ".env.example" "dist\SmartEntry2FA\.env" >nul
  echo No .env found - copied .env.example. Fill it in before using the app.
)

echo.
echo DONE. Your app is in:  dist\SmartEntry2FA\SmartEntry2FA.exe
pause
