import json
import os

from automation.runtime_paths import email_receiver_path


def _settings_file():
    return email_receiver_path()


def get_smtp_receiver_email():
    path = _settings_file()
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("smtp_receiver_email") or "").strip()


def set_smtp_receiver_email(email):
    clean_email = str(email or "").strip()
    path = _settings_file()
    payload = {"smtp_receiver_email": clean_email}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return clean_email


def clear_smtp_receiver_email():
    path = _settings_file()
    if os.path.exists(path):
        try:
            os.remove(path)
            return True
        except Exception:
            return False
    return True
