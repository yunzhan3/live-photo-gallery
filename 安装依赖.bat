@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在安装相册需要的小零件（只装一次）……
python -m pip install flask pillow pillow-heif
echo.
echo 装完啦！以后直接双击「启动相册.bat」就行。
pause
