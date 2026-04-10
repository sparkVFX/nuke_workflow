@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion

echo ============================================
echo   AI Workflow for Nuke - 安装脚本
echo ============================================
echo.

:: 获取当前脚本所在目录（即插件根目录）
set "PLUGIN_DIR=%~dp0"
:: 去掉末尾反斜杠
if "%PLUGIN_DIR:~-1%"=="\" set "PLUGIN_DIR=%PLUGIN_DIR:~0,-1%"

:: .nuke 目录
set "NUKE_DIR=%USERPROFILE%\.nuke"

:: 将路径中的反斜杠转为正斜杠（Nuke/Python 使用正斜杠）
set "PLUGIN_DIR_FWD=%PLUGIN_DIR:\=/%"

echo 插件目录: %PLUGIN_DIR%
echo Nuke配置目录: %NUKE_DIR%
echo.

:: 确保 .nuke 目录存在
if not exist "%NUKE_DIR%" (
    echo 创建 .nuke 目录...
    mkdir "%NUKE_DIR%"
)

:: ====== 标记用于识别我们的代码块 ======
set "BEGIN_MARKER=# --- AI_WORKFLOW_BEGIN ---"
set "END_MARKER=# --- AI_WORKFLOW_END ---"

:: ====== 写入配置文件（记录插件路径） ======
echo 写入插件路径配置...
echo %PLUGIN_DIR_FWD%> "%NUKE_DIR%\ai_workflow.conf"
echo   ai_workflow.conf 已写入。

:: ====== 处理 init.py ======
echo 正在配置 init.py ...

:: 先清除旧的安装内容
if exist "%NUKE_DIR%\init.py" (
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
)

:: 追加代码块（读取配置文件获取路径，不硬编码）
>> "%NUKE_DIR%\init.py" (
    echo.
    echo %BEGIN_MARKER%
    echo import nuke, os, sys
    echo _conf = os.path.join^(os.path.expanduser^("~"^), ".nuke", "ai_workflow.conf"^)
    echo if os.path.isfile^(_conf^):
    echo     with open^(_conf, "r"^) as _f:
    echo         _plugin_dir = _f.read^(^).strip^(^)
    echo     if _plugin_dir and os.path.isdir^(_plugin_dir^):
    echo         if _plugin_dir not in sys.path:
    echo             sys.path.insert^(0, _plugin_dir^)
    echo         nuke.pluginAddPath^(_plugin_dir^)
    echo         _sp = os.path.join^(_plugin_dir, "site-packages"^)
    echo         _need_install = False
    echo         if os.path.isdir^(_sp^):
    echo             if _sp not in sys.path:
    echo                 sys.path.insert^(0, _sp^)
    echo             try:
    echo                 import pydantic_core._pydantic_core
    echo             except ImportError:
    echo                 if _sp in sys.path:
    echo                     sys.path.remove^(_sp^)
    echo                 _need_install = True
    echo         else:
    echo             _need_install = True
    echo         if _need_install:
    echo             import shutil
    echo             if os.path.isdir^(_sp^):
    echo                 shutil.rmtree^(_sp, ignore_errors=True^)
    echo             os.makedirs^(_sp, exist_ok=True^)
    echo             print^("[AI Workflow] Installing dependencies..."^)
    echo             try:
    echo                 import pip
    echo             except ImportError:
    echo                 try:
    echo                     import ensurepip
    echo                     ensurepip.bootstrap^(upgrade=True^)
    echo                 except Exception:
    echo                     pass
    echo             try:
    echo                 from pip._internal.cli.main import main as pip_main
    echo                 pip_main^(["install", "google-genai", "--target", _sp]^)
    echo             except Exception:
    echo                 import subprocess
    echo                 subprocess.run^([sys.executable, "-m", "pip", "install", "google-genai", "--target", _sp]^)
    echo             if _sp not in sys.path:
    echo                 sys.path.insert^(0, _sp^)
    echo         _icons = os.path.join^(_plugin_dir, "ai_workflow", "icons"^)
    echo         if os.path.exists^(_icons^):
    echo             nuke.pluginAddPath^(_icons^)
    echo         def _preload_custom_knob_modules^(^):
    echo             try:
    echo                 import ai_workflow.nanobanana
    echo             except ImportError:
    echo                 pass
    echo             try:
    echo                 import ai_workflow.veo
    echo             except ImportError:
    echo                 pass
    echo         nuke.addOnCreate^(_preload_custom_knob_modules, nodeClass="Root"^)
    echo %END_MARKER%
)

echo   init.py 已配置完成。

:: ====== 处理 menu.py ======
echo 正在配置 menu.py ...

if exist "%NUKE_DIR%\menu.py" (
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
)

>> "%NUKE_DIR%\menu.py" (
    echo.
    echo %BEGIN_MARKER%
    echo import ai_workflow.toolbar
    echo ai_workflow.toolbar.register_toolbar^(^)
    echo %END_MARKER%
)

echo   menu.py 已配置完成。

:: ====== 部署 Studio Listener 到 NukeStudio 自动启动目录 ======
echo 正在部署 Studio Listener ...

set "STARTUP_DIR=%NUKE_DIR%\Python\Startup"
if not exist "%STARTUP_DIR%" (
    mkdir "%STARTUP_DIR%"
)

:: 拷贝 studio_listener.py 到 .nuke/Python/Startup/
if exist "%PLUGIN_DIR%\ai_workflow\studio_listener.py" (
    copy /y "%PLUGIN_DIR%\ai_workflow\studio_listener.py" "%STARTUP_DIR%\studio_listener.py" >nul
    echo   studio_listener.py 已部署到 %STARTUP_DIR%
) else (
    echo   [警告] 未找到 studio_listener.py，跳过部署。
)

echo.
echo ============================================
echo   [OK] Install complete!
echo   First Nuke launch will auto-install Python deps.
echo   Please ensure network connectivity.
echo   Please restart Nuke / NukeStudio.
echo ============================================
echo.
pause
