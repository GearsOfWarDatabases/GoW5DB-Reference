@echo off
rem Sorts loose screenshots in this folder into their class subfolders.
rem Add --dry-run to preview, --unlocks for "A - Card Unlock Levels".
cd /d "%~dp0"
python sort-screenshots.py %*
pause
