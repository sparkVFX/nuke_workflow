"""
Shared settings manager — singleton pattern.
Manages API key, temp directory, project cache, ProRes codec, prompt history.
"""

import os
import json
import tempfile

CONFIG_FILE_NAME = "nanobanana_config.json"
DEFAULT_TEMP_DIR_NAME = "nanobanana_temp"
DEFAULT_PROJECT_CACHE_NAME = "nanobanana_projects"
UNSAVED_PROJECT_DIR = "_unsaved_"


class AppSettings:
    """Manages AI Workflow settings (API key, temp directory, etc.)
    
    Singleton — always returns the same instance via AppSettings().
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AppSettings, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._load()

    def _load(self):
        config_path = os.path.join(os.path.expanduser("~"), ".nuke", CONFIG_FILE_NAME)
        self._data = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        config_path = os.path.join(os.path.expanduser("~"), ".nuke", CONFIG_FILE_NAME)
        try:
            config_dir = os.path.dirname(config_path)
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print("[AI Workflow] Error saving settings: {}".format(e))

    @property
    def api_key(self):
        return self._data.get("api_key", "")

    @api_key.setter
    def api_key(self, value):
        self._data["api_key"] = value
        self._save()

    @property
    def temp_directory(self):
        """Custom temp directory override, or empty string for default."""
        return self._data.get("temp_directory", "")

    @temp_directory.setter
    def temp_directory(self, value):
        self._data["temp_directory"] = value
        self._save()

    @property
    def prompt_history(self):
        return self._data.get("prompt_history", [])

    @prompt_history.setter
    def prompt_history(self, value):
        self._data["prompt_history"] = value[:20]
        self._save()

    @property
    def veo_prompt_history(self):
        return self._data.get("veo_prompt_history", [])

    @veo_prompt_history.setter
    def veo_prompt_history(self, value):
        self._data["veo_prompt_history"] = value[:20]
        self._save()

    @property
    def prores_codec(self):
        return self._data.get("prores_codec", "ProRes 422 HQ")

    @prores_codec.setter
    def prores_codec(self, value):
        self._data["prores_codec"] = value
        self._save()

    @property
    def project_cache_root(self):
        """Root directory for per-project caches.
        Default: <temp>/nanobanana_projects
        """
        custom = self._data.get("project_cache_root", "")
        if custom:
            return custom
        return os.path.join(tempfile.gettempdir(), DEFAULT_PROJECT_CACHE_NAME)

    @project_cache_root.setter
    def project_cache_root(self, value):
        self._data["project_cache_root"] = value
        self._save()


# Module-level singleton instance
app_settings = AppSettings()
