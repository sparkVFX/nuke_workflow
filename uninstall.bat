@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion

echo ============================================
echo   AI Workflow for Nuke - 卸载脚本
echo ============================================
echo.

:: .nuke 目录
set "NUKE_DIR=%USERPROFILE%\.nuke"

echo Nuke配置目录: %NUKE_DIR%
echo.

:: ====== 标记用于识别我们的代码块 ======
set "BEGIN_MARKER=# --- AI_WORKFLOW_BEGIN ---"
set "END_MARKER=# --- AI_WORKFLOW_END ---"

:: ====== 清理 init.py ======
if exist "%NUKE_DIR%\init.py" (
    echo 正在清理 init.py ...
    set "SKIP=0"
    > "%NUKE_DIR%\init.py.tmp" (
        for /f "usebackq delims=" %%L in ("%NUKE_DIR%\init.py") do (
            set "LINE=%%L"
            if "!LINE!"=="%BEGIN_MARKER%" (
                set "SKIP=1"
            )
            if "!SKIP!"=="0" (
                echo %%L
            )
            if "!LINE!"=="%END_MARKER%" (
                set "SKIP=0"
            )
        )
    )
    move /y "%NUKE_DIR%\init.py.tmp" "%NUKE_DIR%\init.py" >nul
    echo   init.py 已清理完成。
) else (
    echo   init.py 不存在，跳过。
)

:: ====== 清理 menu.py ======
if exist "%NUKE_DIR%\menu.py" (
    echo 正在清理 menu.py ...
    set "SKIP=0"
    > "%NUKE_DIR%\menu.py.tmp" (
        for /f "usebackq delims=" %%L in ("%NUKE_DIR%\menu.py") do (
            set "LINE=%%L"
            if "!LINE!"=="%BEGIN_MARKER%" (
                set "SKIP=1"
            )
            if "!SKIP!"=="0" (
                echo %%L
            )
            if "!LINE!"=="%END_MARKER%" (
                set "SKIP=0"
            )
        )
    )
    move /y "%NUKE_DIR%\menu.py.tmp" "%NUKE_DIR%\menu.py" >nul
    echo   menu.py 已清理完成。
) else (
    echo   menu.py 不存在，跳过。
)

:: ====== 清理 Studio Listener ======
if exist "%NUKE_DIR%\Python\Startup\studio_listener.py" (
    echo 正在清理 studio_listener.py ...
    del "%NUKE_DIR%\Python\Startup\studio_listener.py"
    echo   studio_listener.py 已清理。
)

:: ====== 清理 site-packages ======
if exist "%NUKE_DIR%\ai_workflow.conf" (
    :: 读取插件路径，清理 site-packages
    for /f "usebackq delims=" %%P in ("%NUKE_DIR%\ai_workflow.conf") do (
        set "OLD_PLUGIN_DIR=%%P"
    )
    if defined OLD_PLUGIN_DIR (
        :: 将正斜杠转回反斜杠
        set "OLD_PLUGIN_DIR=!OLD_PLUGIN_DIR:/=\!"
        if exist "!OLD_PLUGIN_DIR!\site-packages" (
            echo 正在清理 site-packages 目录...
            rmdir /s /q "!OLD_PLUGIN_DIR!\site-packages"
            echo   site-packages 已清理。
        )
    )
)

:: ====== 删除配置文件 ======
if exist "%NUKE_DIR%\ai_workflow.conf" (
    echo 正在删除 ai_workflow.conf ...
    del "%NUKE_DIR%\ai_workflow.conf"
    echo   ai_workflow.conf 已删除。
)

echo.
echo ============================================
echo   卸载完成！
echo   请重启 Nuke / NukeStudio 使配置生效。
echo ============================================
echo.
pause
