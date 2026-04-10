@echo off
chcp 65001 >nul 2>&1
title Nuke Workflow - Git 工具

cd /d "%~dp0"

echo.
echo ========================================
echo   Nuke Workflow - Git 工具
echo ========================================
echo.
echo   [1] 备份到 GitHub (Backup ^& Push)
echo   [2] 回退所有修改 (Restore all files)
echo   [3] 撤销回退 / 恢复历史版本 (Undo Restore)
echo   [4] 查看修改状态 (Show changes)
echo   [5] 退出
echo.
set /p choice=请选择操作 (1-5):

if "%choice%"=="1" goto :backup
if "%choice%"=="2" goto :restore
if "%choice%"=="3" goto :undo_restore
if "%choice%"=="4" goto :status
if "%choice%"=="5" goto :end

echo 无效选择。
timeout /t 2 >nul
goto :end


:backup
echo.
echo ----------------------------------------
echo   检查变更...
echo ----------------------------------------

git diff --quiet 2>nul
if %errorlevel% neq 0 goto :has_changes

git diff --cached --quiet 2>nul
if %errorlevel% equ 0 (
    echo [OK] 没有新的更改，无需备份。
    timeout /t 3 >nul
    goto :end
)

:has_changes
echo 变更的文件：
echo ------------------------------
git status --short
echo.
set /p msg=请输入本次修改说明（直接回车使用默认）：

if "%msg%"=="" (
    for /f "tokens=1-3 delims=/ " %%a in ('date /t') do set d=%%c-%%a-%%b
    for /f "tokens=1-2 delims=: " %%a in ('time /t') do set t=%%a%%b
    set msg=[%d% %t%] daily backup
)

echo Commit: %msg%
git add -A && git commit -m "%msg%" && git push
if %errorlevel% equ 0 (
    echo.
    echo [✓] 备份完成！
) else (
    echo.
    echo [×] 备份失败，请检查错误信息。
)
timeout /t 5 >nul
goto :end


:restore
echo.
echo ----------------------------------------
echo   当前修改的文件：
echo ----------------------------------------
git status --short
echo.
set /p confirm=确认回退所有修改？(Y/N):
if /i not "%confirm%"=="Y" (
    echo 已取消。
    timeout /t 2 >nul
    goto :end
)
git checkout -- .
echo.
echo [✓] 已回退所有文件到上次提交的状态。
echo     如需撤销此操作，请选择 [3] 撤销回退。
timeout /t 3 >nul
goto :end


:undo_restore
echo.
echo ----------------------------------------
echo   操作记录 (Reflog)：
echo ----------------------------------------
git reflog -10
echo.
echo 说明：每一行前面的 hash 值代表一个历史快照。
echo       HEAD^{0} 是当前位置，HEAD^{1} 是上一步...
echo.
set /p target_hash=输入要恢复的 hash 值（直接回车取消）：

if "%target_hash%"=="" (
    echo 已取消。
    timeout /t 2 >nul
    goto :end
)

echo.
set /p confirm2=警告！这将把代码恢复到 %target_hash% 的状态。确认？(Y/N):
if /i not "%confirm2%"=="Y" (
    echo 已取消。
    timeout /t 2 >nul
    goto :end
)

git reset --hard %target_hash%
if %errorlevel% equ 0 (
    echo.
    echo [✓] 已恢复到版本: %target_hash%
) else (
    echo.
    echo [×] 恢复失败，请检查 hash 是否正确。
)
timeout /t 3 >nul
goto :end


:status
echo.
echo ----------------------------------------
echo   当前修改状态：
echo ----------------------------------------
git status --short
if %errorlevel% neq 0 echo (工作区干净，没有修改)
echo.
echo ----------------------------------------
echo   最近10次提交记录：
echo ----------------------------------------
git log --oneline -10
echo.
pause
goto :end


:end
