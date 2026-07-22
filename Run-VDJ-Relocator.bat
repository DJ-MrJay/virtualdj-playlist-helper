@echo off
cd /d "%~dp0"
python "%~dp0vdj_relocator.py" --gui %*
pause
