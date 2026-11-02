import os
import sys
import tempfile
from pathlib import Path

_runtime_root_cache = None


def project_root():
    if getattr(sys, "frozen", False):
        # In a frozen build, this is the directory that contains the EXE.
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _frozen_resource_root():
    exe_dir = Path(sys.executable).resolve().parent
    internal_dir = exe_dir / "_internal"
    if internal_dir.exists():
        return internal_dir
    return exe_dir


def resource_root():
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return _frozen_resource_root()
    return project_root()


def _is_writable_dir(path_obj):
    try:
        path_obj.mkdir(parents=True, exist_ok=True)
        test_file = path_obj / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def runtime_root():
    global _runtime_root_cache
    if _runtime_root_cache is not None:
        return _runtime_root_cache

    candidates = []
    env_home = os.getenv("FB_REALESTATE_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home))

    if getattr(sys, "frozen", False):
        candidates.append(Path(os.getenv("LOCALAPPDATA") or Path.home()) / "FBAutomationRealEstate")
    else:
        candidates.append(project_root())

    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(Path(local_app_data) / "FBAutomationRealEstate")
    candidates.append(Path.home() / "FBAutomationRealEstate")
    candidates.append(Path(tempfile.gettempdir()) / "FBAutomationRealEstate")

    for candidate in candidates:
        if _is_writable_dir(candidate / "data"):
            _runtime_root_cache = candidate
            return _runtime_root_cache

    fallback = Path(tempfile.gettempdir()) / "FBAutomationRealEstate"
    fallback.mkdir(parents=True, exist_ok=True)
    _runtime_root_cache = fallback
    return _runtime_root_cache


def data_dir(*parts):
    base = runtime_root() / "data"
    base.mkdir(parents=True, exist_ok=True)
    path = base.joinpath(*parts)
    if parts:
        path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_dir():
    return str(data_dir("sessions"))


def screenshots_dir():
    return str(data_dir("screenshots"))


def runtime_log_path():
    return str(data_dir() / "runtime.log")


def activity_log_path():
    return str(data_dir() / "activity_log.json")


def browser_settings_path():
    return str(data_dir() / "browser_settings.json")


def session_health_path():
    return str(data_dir() / "session_health.json")


def template_dir():
    return str(resource_root() / "web" / "templates")


def static_dir():
    return str(resource_root() / "web" / "static")
