import json
import os
import sys
from automation.runtime_paths import data_dir


def load():
    defaults = {'background_sync': False, 'start_with_windows': True}
    path = data_dir() / 'desktop_preferences.json'
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved.pop('automatic_updates', None)
        saved['background_sync'] = False
        return dict(defaults, **saved)
    return defaults


def save(background_sync, start_with_windows):
    payload = load()
    payload.update(background_sync=False, start_with_windows=bool(start_with_windows))
    if getattr(sys, 'frozen', False):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
            if start_with_windows:
                winreg.SetValueEx(key, 'FBAutomationRealEstate', 0, winreg.REG_SZ, '"' + sys.executable + '" --background')
            else:
                try:
                    winreg.DeleteValue(key, 'FBAutomationRealEstate')
                except FileNotFoundError:
                    pass
    path = data_dir() / 'desktop_preferences.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload), encoding='utf-8')
    os.replace(temporary, path)
    return payload
