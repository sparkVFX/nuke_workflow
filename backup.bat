@echo off
chcp 65001 >nul 2>&1
title Nuke Workflow - 备份到 GitHub

cd /d "%~dp0"

echo ========================================
echo   Nuke Workflow - 备份到 GitHub
echo ========================================
echo.

:: 检查是否有变更
git diff --quiet 2>nul
if %errorlevel% neq 0 goto :has_changes

git diff --cached --quiet 2>nul
if %errorlevel% equ 0 (
    echo [OK] 没有新的更改，无需备份。
    timeout /t 3 >nul
    exit /b 0
)

:has_changes

:: 显示变更文件列表
echo 变更的文件：
echo ------------------------------
git status --short
echo.

:: 输入备注信息
set /p msg=请输入本次修改说明（直接回车使用默认）：

:: 如果没输入，用默认信息（带时间戳）
if "%msg%"=="" (
    for /f "tokens=1-3 delims=/ " %%a in ('date /t') do set d=%%c-%%a-%%b
    for /f "tokens=1-2 delims=: " %%a in ('time /t') do set t=%%a%%b
    set msg=[%d% %t%] daily backup
)

echo.
echo Commit: %msg%
echo.

:: 提交并推送
git add -A && git commit -m "%msg%" && git push
if %errorlevel% equ 0 (
    echo.
    echo ========================================
    echo   [✓] 备份完成！
    echo ========================================
) else (
    echo.
    echo ========================================
    echo   [×] 备份失败，请检查错误信息
    echo ========================================
)

timeout /t 5 >nul
