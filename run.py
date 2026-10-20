import threading
import time
import webbrowser
import os
import sys
import subprocess
import tempfile
import traceback
from automation.terminal_log import install as install_terminal_log
install_terminal_log()
from automation.keep_awake import enable_keep_awake_from_env, enable_button_sleep_block_from_env


def open_ui():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:5001/")


def _is_truthy_env(name):
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _watchdog_log_path():
    try:
        from automation.runtime_paths import data_dir

        return str(data_dir() / "watchdog.log")
    except Exception:
        return os.path.abspath("watchdog.log")


def _bootstrap_log_path():
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        return os.path.join(exe_dir, "bootstrap.log")
    except Exception:
        try:
            return os.path.join(tempfile.gettempdir(), "FBAutomation-bootstrap.log")
        except Exception:
            return os.path.abspath("FBAutomation-bootstrap.log")


def _append_watchdog_log(text):
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_watchdog_log_path(), "a", encoding="utf-8", errors="ignore") as f:
            f.write(f"[{ts}] {text}\n")
    except Exception:
        pass


def _append_bootstrap_log(text):
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_bootstrap_log_path(), "a", encoding="utf-8", errors="ignore") as f:
            f.write(f"[{ts}] {text}\n")
    except Exception:
        pass


def _show_fatal_message(title, message):
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, str(message), str(title), 0x10)  # MB_ICONERROR
        return True
    except Exception:
        return False


def _install_startup_launcher_for_exe():
    if not getattr(sys, "frozen", False):
        return
    try:
        startup_dir = os.path.join(
            os.getenv("APPDATA", ""),
            "Microsoft",
            "Windows",
            "Start Menu",
            "Programs",
            "Startup",
        )
        if not startup_dir:
            return
        os.makedirs(startup_dir, exist_ok=True)
        launcher_path = os.path.join(startup_dir, "FBAutomationWatchdog.cmd")
        exe_path = os.path.abspath(sys.executable)
        lines = [
            "@echo off",
            f'cd /d "{os.path.dirname(exe_path)}"',
            f'start "" "{exe_path}"',
        ]
        with open(launcher_path, "w", encoding="ascii", errors="ignore") as f:
            f.write("\r\n".join(lines) + "\r\n")
    except Exception:
        # Non-fatal: app can still run without startup launcher.
        pass


def _serve_once():
    try:
        from app import app
    except Exception:
        _append_bootstrap_log("Failed to import app:\n" + traceback.format_exc())
        raise

    no_browser = _is_truthy_env("FB_AUTOMATION_NO_BROWSER")
    if not no_browser:
        threading.Thread(target=open_ui, daemon=True).start()

    # Prefer waitress when available (production WSGI server), but keep a
    # dependency-free fallback so the EXE can run even if waitress isn't installed
    # in the build environment.
    try:
        from waitress import serve  # type: ignore

        serve(app, host="0.0.0.0", port=5001)
        return
    except Exception:
        pass

    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)


if __name__ == "__main__":
    keep_awake_enabled = enable_keep_awake_from_env()
    button_sleep_blocked = enable_button_sleep_block_from_env()
    print(f"[Power] Keep-awake {'enabled' if keep_awake_enabled else 'not enabled'} in run.py", flush=True)
    print(
        f"[Power] Button/lid sleep block {'enabled' if button_sleep_blocked else 'not enabled'} in run.py",
        flush=True,
    )

    child_mode = "--child" in sys.argv or _is_truthy_env("FB_AUTOMATION_CHILD")

    # In source/dev mode, keep single-process behavior.
    if not getattr(sys, "frozen", False):
        _serve_once()
        raise SystemExit(0)

    if child_mode:
        try:
            _append_bootstrap_log("Child starting server")
            _append_watchdog_log("Child starting server")
            _serve_once()
            _append_bootstrap_log("Child server exited normally")
            _append_watchdog_log("Child server exited normally")
            raise SystemExit(0)
        except Exception as e:
            _append_bootstrap_log("Child crashed:\n" + traceback.format_exc())
            _append_watchdog_log(f"Child crashed: {e}")
            raise SystemExit(1)

    # EXE parent: install startup launcher and watch/restart child server forever.
    _install_startup_launcher_for_exe()
    no_browser = _is_truthy_env("FB_AUTOMATION_NO_BROWSER")
    if not no_browser:
        threading.Thread(target=open_ui, daemon=True).start()

    crash_times = []
    while True:
        env = os.environ.copy()
        env["FB_AUTOMATION_CHILD"] = "1"
        env["FB_AUTOMATION_NO_BROWSER"] = "1"
        try:
            creationflags = 0
            if sys.platform.startswith("win"):
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            with open(_watchdog_log_path(), "a", encoding="utf-8", errors="ignore") as logf:
                proc = subprocess.Popen(
                    [sys.executable, "--child"],
                    env=env,
                    stdout=logf,
                    stderr=logf,
                    creationflags=creationflags,
                )
            exit_code = proc.wait()
            _append_watchdog_log(f"Child exited with code {exit_code}")

            now = time.time()
            crash_times = [t for t in crash_times if now - t < 60]
            crash_times.append(now)
            if len(crash_times) >= 5:
                msg = (
                    "The app keeps restarting because the server process is crashing.\n\n"
                    f"Check the log file:\n{_watchdog_log_path()}\n\n"
                    "Common causes:\n"
                    "- Port 5001 already in use\n"
                    "- Missing files/permissions\n"
                    "- Chrome/driver installation issues\n"
                )
                _append_watchdog_log("Too many crashes; stopping watchdog")
                _show_fatal_message("FBAutomation startup failed", msg)
                raise SystemExit(1)
        except Exception as e:
            _append_watchdog_log(f"Watchdog failed to launch child: {e}")
        time.sleep(5)
