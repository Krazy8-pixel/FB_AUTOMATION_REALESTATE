import atexit
import ctypes
import os
import re
import subprocess
import sys

# Windows SetThreadExecutionState flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_enabled = False
_power_policy_enabled = False
_original_policy = {}

# powercfg constants
_SUB_BUTTONS = "4f971e89-eebd-4455-a8de-9e59040e7347"
_SETTING_LID_ACTION = "5ca83367-6e45-459f-a27b-476b1d01c936"
_SETTING_POWER_BUTTON = "7648efa3-dd9c-4e3e-b566-50f929386280"
_SETTING_SLEEP_BUTTON = "96996bc0-ad50-47ec-923b-6f41874dd9eb"


def _is_windows():
    return sys.platform.startswith("win")


def _run_powercfg(args):
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        ["powercfg", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
        creationflags=creationflags,
    )


def _active_scheme_guid():
    proc = _run_powercfg(["/GETACTIVESCHEME"])
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    m = re.search(r"([0-9a-fA-F-]{36})", text)
    return m.group(1) if m else None


def _query_setting_indexes(scheme_guid, setting_guid):
    proc = _run_powercfg(["/Q", scheme_guid, _SUB_BUTTONS, setting_guid])
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")

    ac_match = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", text)
    dc_match = re.search(r"Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)", text)
    ac_val = int(ac_match.group(1), 16) if ac_match else None
    dc_val = int(dc_match.group(1), 16) if dc_match else None
    return ac_val, dc_val


def _set_setting_indexes(scheme_guid, setting_guid, ac_value, dc_value):
    ok = True
    if ac_value is not None:
        ac_proc = _run_powercfg(
            ["/SETACVALUEINDEX", scheme_guid, _SUB_BUTTONS, setting_guid, str(int(ac_value))]
        )
        ok = ok and ac_proc.returncode == 0
    if dc_value is not None:
        dc_proc = _run_powercfg(
            ["/SETDCVALUEINDEX", scheme_guid, _SUB_BUTTONS, setting_guid, str(int(dc_value))]
        )
        ok = ok and dc_proc.returncode == 0
    return ok


def _apply_scheme(scheme_guid):
    proc = _run_powercfg(["/SETACTIVE", scheme_guid])
    return proc.returncode == 0


def enable_keep_awake(keep_display_on=False):
    """
    Prevent system sleep while this process is running.
    By default, display is allowed to turn off.
    """
    global _enabled
    if _enabled:
        return True
    if not _is_windows():
        return False

    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if keep_display_on:
        flags |= ES_DISPLAY_REQUIRED

    try:
        result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        if result == 0:
            return False
        _enabled = True
        return True
    except Exception:
        return False


def disable_keep_awake():
    """Restore default sleep behavior for this process."""
    global _enabled
    if not _is_windows():
        _enabled = False
        return False
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        _enabled = False
        return True
    except Exception:
        _enabled = False
        return False


def enable_button_sleep_block():
    """
    Best-effort: block user-induced sleep from lid/power/sleep buttons
    by setting them to "Do nothing" while app runs.
    Restored on process exit by disable_button_sleep_block().
    """
    global _power_policy_enabled, _original_policy
    if _power_policy_enabled:
        return True
    if not _is_windows():
        return False

    scheme_guid = _active_scheme_guid()
    if not scheme_guid:
        return False

    settings = [
        _SETTING_LID_ACTION,
        _SETTING_POWER_BUTTON,
        _SETTING_SLEEP_BUTTON,
    ]
    backup = {"scheme": scheme_guid, "values": {}}
    for setting in settings:
        ac_val, dc_val = _query_setting_indexes(scheme_guid, setting)
        backup["values"][setting] = (ac_val, dc_val)

    changed_any = False
    for setting in settings:
        ok = _set_setting_indexes(scheme_guid, setting, 0, 0)  # 0 = Do nothing
        changed_any = changed_any or ok

    if changed_any:
        _apply_scheme(scheme_guid)
        _original_policy = backup
        _power_policy_enabled = True
        return True
    return False


def disable_button_sleep_block():
    global _power_policy_enabled, _original_policy
    if not _is_windows():
        _power_policy_enabled = False
        _original_policy = {}
        return False
    if not _power_policy_enabled or not _original_policy:
        return False

    scheme_guid = _original_policy.get("scheme")
    values = _original_policy.get("values", {})
    ok = True
    for setting, pair in values.items():
        ac_val, dc_val = pair if isinstance(pair, tuple) and len(pair) == 2 else (None, None)
        ok = _set_setting_indexes(scheme_guid, setting, ac_val, dc_val) and ok
    _apply_scheme(scheme_guid)

    _power_policy_enabled = False
    _original_policy = {}
    return ok


def enable_button_sleep_block_from_env():
    """
    Enable policy block unless explicitly disabled:
    FB_AUTOMATION_BLOCK_BUTTON_SLEEP=0/false/no/off
    """
    raw = str(os.getenv("FB_AUTOMATION_BLOCK_BUTTON_SLEEP", "1")).strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return enable_button_sleep_block()


def enable_keep_awake_from_env():
    """
    Enable keep-awake unless explicitly disabled by env var:
    FB_AUTOMATION_KEEP_AWAKE=0/false/no/off
    Optional:
    FB_AUTOMATION_KEEP_DISPLAY_ON=1/true/yes/on
    """
    raw = str(os.getenv("FB_AUTOMATION_KEEP_AWAKE", "1")).strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False

    keep_display_raw = str(os.getenv("FB_AUTOMATION_KEEP_DISPLAY_ON", "")).strip().lower()
    keep_display_on = keep_display_raw in {"1", "true", "yes", "on"}
    return enable_keep_awake(keep_display_on=keep_display_on)


atexit.register(disable_keep_awake)
atexit.register(disable_button_sleep_block)
