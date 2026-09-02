import json
import os

from automation.runtime_paths import auto_reply_state_path


def load_auto_reply_state():
    path = auto_reply_state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_auto_reply_state(account, reply_message="", delay=5, pin=""):
    payload = {
        "enabled": True,
        "account": str(account or "").strip(),
        "reply_message": str(reply_message or ""),
        "delay": int(delay or 5),
        "pin": str(pin or ""),
    }
    path = auto_reply_state_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def clear_auto_reply_state():
    path = auto_reply_state_path()
    if os.path.exists(path):
        try:
            os.remove(path)
            return True
        except Exception:
            return False
    return True
