"""
Nuke init.py - Plugin initialization.
Run install.bat to install automatically,
or copy this file to ~/.nuke/init.py and create ~/.nuke/ai_workflow.conf with the plugin path.
"""

import nuke
import os
import sys

# Read plugin directory from config file: ~/.nuke/ai_workflow.conf
_conf_path = os.path.join(os.path.expanduser("~"), ".nuke", "ai_workflow.conf")
PLUGIN_DIR = ""
if os.path.isfile(_conf_path):
    with open(_conf_path, "r") as _f:
        PLUGIN_DIR = _f.read().strip()

if not PLUGIN_DIR or not os.path.isdir(PLUGIN_DIR):
    print("[AI Workflow] Warning: plugin path not found. Run install.bat or check {}".format(_conf_path))
else:
    # Add the plugin directory to sys.path so Python can find ai_workflow package
    if PLUGIN_DIR not in sys.path:
        sys.path.insert(0, PLUGIN_DIR)

    # Add the plugin directory to Nuke's plugin path
    nuke.pluginAddPath(PLUGIN_DIR)

    # ---------------------------------------------------------------------------
    # Add local site-packages to sys.path (for google-genai and dependencies)
    # ---------------------------------------------------------------------------
    _site_pkg_dir = os.path.join(PLUGIN_DIR, "site-packages")

    def _install_deps():
        """Install google-genai into site-packages using pip's Python API (runs inside Nuke)."""
        import shutil
        # Clean old site-packages if exists (may contain wrong Python version builds)
        if os.path.isdir(_site_pkg_dir):
            print("[AI Workflow] Cleaning old site-packages (Python version mismatch)...")
            shutil.rmtree(_site_pkg_dir, ignore_errors=True)
        os.makedirs(_site_pkg_dir, exist_ok=True)
        print("[AI Workflow] Installing google-genai with Python {}.{}...".format(
            sys.version_info.major, sys.version_info.minor))
        # Step 1: Ensure pip is available
        try:
            import pip
        except ImportError:
            print("[AI Workflow] pip not found, bootstrapping with ensurepip...")
            try:
                import ensurepip
                ensurepip.bootstrap(upgrade=True)
            except Exception as _ep_err:
                print("[AI Workflow] ensurepip failed: {}".format(_ep_err))
        # Step 2: Install google-genai using pip API
        try:
            from pip._internal.cli.main import main as pip_main
            _ret = pip_main(["install", "google-genai", "--target", _site_pkg_dir])
            if _ret == 0:
                print("[AI Workflow] Auto-install succeeded.")
                return True
            else:
                print("[AI Workflow] pip returned code: {}".format(_ret))
        except Exception as _pip_err:
            print("[AI Workflow] pip API failed: {}".format(_pip_err))
        # Step 3: Fallback to subprocess
        print("[AI Workflow] Trying subprocess fallback...")
        try:
            import subprocess
            _result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "google-genai",
                 "--target", _site_pkg_dir],
                capture_output=True, text=True, timeout=600
            )
            if _result.returncode == 0:
                print("[AI Workflow] Auto-install (subprocess) succeeded.")
                return True
            else:
                print("[AI Workflow] subprocess also failed: {}".format(_result.stderr))
        except Exception as _sub_err:
            print("[AI Workflow] subprocess error: {}".format(_sub_err))
        return False

    _need_install = False
    if os.path.isdir(_site_pkg_dir):
        # site-packages exists, add to path and verify pydantic_core loads correctly
        if _site_pkg_dir not in sys.path:
            sys.path.insert(0, _site_pkg_dir)
        try:
            import pydantic_core._pydantic_core  # noqa: F401
            print("[AI Workflow] Dependencies OK (Python {}.{})".format(
                sys.version_info.major, sys.version_info.minor))
        except ImportError:
            print("[AI Workflow] pydantic_core version mismatch detected, will reinstall...")
            # Remove bad path before reinstall
            if _site_pkg_dir in sys.path:
                sys.path.remove(_site_pkg_dir)
            _need_install = True
    else:
        _need_install = True

    if _need_install:
        print("[AI Workflow] Installing dependencies (first launch or version mismatch)...")
        try:
            if _install_deps():
                if _site_pkg_dir not in sys.path:
                    sys.path.insert(0, _site_pkg_dir)
            else:
                print("[AI Workflow] Please install manually in Nuke Script Editor:")
                print("[AI Workflow]   import subprocess, sys")
                print('[AI Workflow]   subprocess.run([sys.executable, "-m", "pip", "install", "google-genai", "--target", "{}"])'.format(
                    _site_pkg_dir.replace("\\", "/")))
        except Exception as _e:
            print("[AI Workflow] Auto-install error: {}".format(_e))

    # Add icons directory to Nuke's plugin path so icons can be found
    icons_dir = os.path.join(PLUGIN_DIR, "ai_workflow", "icons")
    if os.path.exists(icons_dir):
        nuke.pluginAddPath(icons_dir)

    # Pre-import modules that contain PyCustom_Knob widgets
    def _preload_custom_knob_modules():
        """Preload modules containing PyCustom_Knob classes so they are available
        when Nuke reloads saved scripts."""
        try:
            import ai_workflow.nanobanana
        except ImportError as e:
            print("NanoBanana: Warning - could not preload module: {}".format(e))
        try:
            import ai_workflow.veo
        except ImportError as e:
            print("VEO: Warning - could not preload module: {}".format(e))
        try:
            import ai_workflow.gemini_chat
        except ImportError as e:
            print("GeminiChat: Warning - could not preload module: {}".format(e))

    nuke.addOnCreate(_preload_custom_knob_modules, nodeClass="Root")

    # Restore NB Player thumbnails on script load
    def _restore_thumbnails():
        try:
            from ai_workflow.nanobanana import restore_nb_thumbnails, ensure_save_callback_registered
            restore_nb_thumbnails()
            # Register the onScriptSave callback for cache migration
            ensure_save_callback_registered()
        except Exception as _e:
            print("[AI Workflow] Thumbnail restore error: {}".format(_e))
    nuke.addOnScriptLoad(_restore_thumbnails)

    # --- NukeStudio Listener ---
    # studio_listener.py is now deployed to .nuke/Python/Startup/
    # and auto-started by NukeStudio, no manual import needed.
