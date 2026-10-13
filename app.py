import sys


def _configure_console_encoding():
    """Keep Windows consoles from crashing on Unicode status messages."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_console_encoding()

from flask import Flask
from web.routes import web_bp
from automation.runtime_paths import static_dir, template_dir, screenshots_dir, data_dir
from automation.keep_awake import enable_keep_awake_from_env, enable_button_sleep_block_from_env
from automation.browser_settings import is_run_in_background_enabled
import os

app = Flask(
    __name__,
    template_folder=template_dir(),
    static_folder=static_dir(),
)
app.secret_key = "fb_automation_secret"
if os.getenv("FB_AUTOMATION_DESKTOP") == "1":
    from datetime import timedelta
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
enable_keep_awake_from_env()
enable_button_sleep_block_from_env()

# register blueprint with all routes including dashboard
app.register_blueprint(web_bp)


def _format_mb_gb(num_bytes):
    try:
        value = int(num_bytes or 0)
    except Exception:
        value = 0
    if value <= 0:
        return "0 MB"
    gb = 1024 * 1024 * 1024
    mb = 1024 * 1024
    if value >= gb:
        return f"{value / gb:.2f} GB"
    return f"{value / mb:.2f} MB"


def _screenshot_junk_bytes():
    return _folder_image_bytes(screenshots_dir())


def _manual_images_bytes():
    try:
        folder = str(data_dir("manual_listing_images"))
        videos = str(data_dir("manual_listing_videos"))
    except Exception:
        folder = ""
        videos = ""
    return _folder_image_bytes(folder) + _folder_image_bytes(videos)


def _folder_image_bytes(folder):
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".jfif", ".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}
    total = 0
    try:
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                ext = os.path.splitext(name)[1].strip().lower()
                if ext not in image_exts:
                    continue
                try:
                    total += os.path.getsize(path)
                except Exception:
                    pass
    except Exception:
        return 0
    return total


@app.context_processor
def inject_junk_files_size():
    screenshot_bytes = _screenshot_junk_bytes()
    manual_bytes = _manual_images_bytes()
    return {
        "junk_files_size_bytes": screenshot_bytes,
        "junk_files_size_label": _format_mb_gb(screenshot_bytes),
        "manual_pics_size_bytes": manual_bytes,
        "manual_pics_size_label": _format_mb_gb(manual_bytes),
        "run_in_background_enabled": is_run_in_background_enabled(),
    }


if __name__ == "__main__":
    print(f"[Startup] Running source app: {os.path.abspath(__file__)}", flush=True)
    app.run(host="127.0.0.1", port=5001, debug=False)
