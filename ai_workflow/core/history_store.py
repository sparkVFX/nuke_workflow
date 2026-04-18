"""统一历史记录存储接口（项目级 / 全局级）。"""

import os
import json

from ai_workflow.core.settings import app_settings
from ai_workflow.core.directories import get_project_directory


def _project_history_file():
    return os.path.join(get_project_directory(), "history.json")


def _load_project_data():
    path = _project_history_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_project_data(data):
    path = _project_history_file()
    try:
        folder = os.path.dirname(path)
        if folder and not os.path.exists(folder):
            os.makedirs(folder)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data or {}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("[HistoryStore] save project history failed: {}".format(e))


def get_history(key, scope="project", limit=20):
    if scope == "global":
        if hasattr(app_settings.__class__, key):
            values = getattr(app_settings, key, []) or []
        else:
            values = app_settings._data.get(key, []) or []
        return list(values)[:limit]

    data = _load_project_data()
    values = data.get(key, []) if isinstance(data, dict) else []
    if not isinstance(values, list):
        values = []
    return values[:limit]


def set_history(key, values, scope="project", limit=20):
    clean_values = [v for v in (values or []) if isinstance(v, str) and v]
    clean_values = clean_values[:limit]

    if scope == "global":
        if hasattr(app_settings.__class__, key):
            setattr(app_settings, key, clean_values)
        else:
            app_settings._data[key] = clean_values
            app_settings._save()
        return

    data = _load_project_data()
    data[key] = clean_values
    _save_project_data(data)


def push_history_item(key, item, scope="project", limit=20):
    item = (item or "").strip()
    if not item:
        return
    history = get_history(key, scope=scope, limit=limit)
    if item in history:
        history.remove(item)
    history.insert(0, item)
    set_history(key, history, scope=scope, limit=limit)
