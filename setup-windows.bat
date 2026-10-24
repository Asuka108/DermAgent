@echo off
rem ============================================================================
rem  肤智 DermAgent —— Windows 链接重建脚本
rem  从 mac 拷过来的 10 条软链在 Windows 上会失效，本脚本用 junction(目录) +
rem  hardlink(文件) 原位重建，**无需管理员权限**，可重复执行（幂等）。
rem  用法：把整个 DermAgent 目录放到本机任意位置，双击本文件（或在 cmd 里执行）。
rem  注：脚本按标准 mklink 语义编写，未在 Windows 实机跑过；跑完请看末尾校验行。
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

call :mkj "win\frontend\web"             "%~dp0web\frontend"
call :mkh "win\frontend\agent_client.py" "%~dp0win\backend\agent_client.py"
call :mkj "win\backend\shared"           "%~dp0web\backend\shared"
call :mkj "win\backend\server"           "%~dp0web\backend\server"
call :mkj "win\shared"                   "%~dp0win\backend\shared"
call :mkj "win\server"                   "%~dp0win\backend\server"
call :mkj "win\client\web"               "%~dp0web\frontend"
call :mkj "win\webui"                    "%~dp0web\frontend\legacy-webui"
call :mkj "web\backend\client\web"       "%~dp0web\frontend"
call :mkj "web\backend\webui"            "%~dp0web\frontend\legacy-webui"

echo.
echo —— 可达性校验 ——
for %%F in ("win\frontend\web\index.html" "win\frontend\web\vendor\three.min.js" "win\server\app.py" "win\shared\derm_core.py" "web\backend\client\web\main.js" "web\backend\shared\derm_core.py") do (
  if exist %%F (echo   OK    %%~F) else (echo   FAIL  %%~F   ^<- 该路径可能是解压缩留下的占位文件，请手动删除后重跑本脚本)
)
echo.
echo 下一步：
echo   python -m venv .venv ^&^& .venv\Scripts\activate
echo   pip install -r web\backend\requirements.txt -r win\frontend\requirements.txt
echo   服务端： cd web\backend\server ^&^& python app.py    （浏览器 http://127.0.0.1:8765/）
echo   桌面端： cd win\frontend      ^&^& python main.py
echo   知识库：把 dermagent-kb-backup.json 内容 POST 到 /api/kb/restore（或界面齿轮-知识库-恢复）
exit /b 0

:mkj
if exist "%~1\" ( echo 跳过（已存在目录） %~1 & goto :eof )
if not exist "%~1" goto :mkj_make
for %%A in ("%~1") do set "SZ=%%~zA"
if not "!SZ!"=="0" ( echo 跳过（已存在文件，非占位） %~1 & goto :eof )
move "%~1" "%~1.placeholder-bak" >nul && echo 移走 0 字节占位文件 %~1
:mkj_make
for %%I in ("%~1") do if not exist "%%~dpI" mkdir "%%~dpI"
mklink /J "%~1" "%~2" >nul && echo junction  %~1  || echo 失败    %~1
goto :eof

:mkh
if not exist "%~1" goto :mkh_link
for %%A in ("%~1") do set "SZ=%%~zA"
if not "!SZ!"=="0" ( echo 跳过（已存在） %~1 & goto :eof )
move "%~1" "%~1.placeholder-bak" >nul && echo 移走 0 字节占位文件 %~1
:mkh_link
for %%I in ("%~1") do if not exist "%%~dpI" mkdir "%%~dpI"
mklink /H "%~1" "%~2" >nul && echo hardlink  %~1  || echo 失败    %~1
goto :eof
