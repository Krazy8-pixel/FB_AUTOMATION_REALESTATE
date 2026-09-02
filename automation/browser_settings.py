import json
import os

from automation.runtime_paths import browser_settings_path


DEFAULT_BROWSER_SETTINGS = {
    "run_in_background": True,
}


def _coerce_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def load_browser_settings():
    path = browser_settings_path()
    if not os.path.exists(path):
        return dict(DEFAULT_BROWSER_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return dict(DEFAULT_BROWSER_SETTINGS)
    if not isinstance(payload, dict):
        return dict(DEFAULT_BROWSER_SETTINGS)

    settings = dict(DEFAULT_BROWSER_SETTINGS)
    settings["run_in_background"] = _coerce_bool(
        payload.get("run_in_background"),
        default=DEFAULT_BROWSER_SETTINGS["run_in_background"],
    )
    return settings


def save_browser_settings(*, run_in_background=None):
    settings = load_browser_settings()
    if run_in_background is not None:
        settings["run_in_background"] = bool(run_in_background)

    path = browser_settings_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    return settings


def is_run_in_background_enabled():
    settings = load_browser_settings()
    return bool(settings.get("run_in_background", True))


def set_run_in_background_enabled(enabled):
    settings = save_browser_settings(run_in_background=enabled)
    return bool(settings.get("run_in_background", True))


def apply_background_mode_option(options, enabled=None):
    run_in_background = (
        is_run_in_background_enabled() if enabled is None else bool(enabled)
    )
    if run_in_background:
        options.add_argument("--headless=new")
    return run_in_background
