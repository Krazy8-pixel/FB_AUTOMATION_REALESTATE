"""Windows desktop entry point: local server, persistent UI, and system tray."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import socket
import sys
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--health-file')
    parser.add_argument('--smoke-test', action='store_true')
    args = parser.parse_args()
    os.environ['FB_AUTOMATION_DESKTOP'] = '1'
    from automation.task_control import set_window_open, stop_foreground_work
    set_window_open(not args.background)
    os.environ['FB_AUTOMATION_BLOCK_BUTTON_SLEEP'] = '0'
    os.environ['FB_AUTOMATION_KEEP_AWAKE'] = '0'
    from automation.runtime_paths import data_dir, resource_root
    folder = data_dir()
    # Keep console-free builds from crashing on print() calls in automation.
    log = (folder / 'desktop.log').open('a', encoding='utf-8', buffering=1)
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.restype = ctypes.c_void_p
    mutex_name = 'Local\\TayyaraMotorsDesktop'
    if args.smoke_test:
        mutex_name += f'SmokeTest-{os.getpid()}'
    mutex = kernel.CreateMutexW(None, False, mutex_name)
    if kernel.GetLastError() == 183:
        # A second launch asks the already-running tray instance to show itself.
        (folder / 'show-window').touch()
        return
    from app import app
    from web import routes
    if args.background:
        stop_foreground_work(routes)
    from waitress import create_server
    import pystray
    import webview
    # Allow the authenticated diagnostic-report link to open Save As in WebView2.
    webview.settings['ALLOW_DOWNLOADS'] = True
    from PIL import Image
    from automation import app_updates
    from automation.desktop_state import checkpoint
    from automation.maintenance import busy

    server = create_server(app, host='127.0.0.1', port=0, threads=12)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    port = server.effective_port
    shutting_down = threading.Event()
    window = webview.create_window('Tayyara Motors', f'http://127.0.0.1:{port}/', width=1380, height=900, min_size=(390, 600), hidden=args.background)

    def show(icon=None, item=None):
        set_window_open(True)
        tray.visible = True
        window.show()
        window.restore()

    def quit_app(icon=None, item=None, updating=False):
        if not updating:
            stop_foreground_work(routes)
        shutting_down.set()
        routes.scheduler.pause()
        checkpoint()
        tray.stop()
        window.destroy()

    def closing():
        if not shutting_down.is_set():
            stop_foreground_work(routes)
            window.hide()
            return False
        return True

    window.events.closing += closing
    tray = pystray.Icon('TayyaraMotors', Image.open(resource_root() / 'assets/app-logo.png'), 'Tayyara Motors — background sync', menu=pystray.Menu(pystray.MenuItem('Open Tayyara Motors', show, default=True), pystray.MenuItem('Exit App', lambda icon, item: quit_app(icon, item))))
    tray.run_detached()
    app_updates.exit_callback = lambda: quit_app(updating=True)

    def monitor():
        while not shutting_down.wait(1):
            uninstall_signal = folder / 'exit-for-uninstall'
            if uninstall_signal.exists():
                uninstall_signal.unlink(missing_ok=True)
                quit_app(updating=True)
                return
            signal = folder / 'show-window'
            if signal.exists():
                signal.unlink(missing_ok=True)
                show()

    def loaded():
        if args.health_file:
            health = Path(args.health_file).resolve()
            if health.parent == (folder / 'updates').resolve():
                health.write_text(json.dumps({'version': app_updates.config()['version']}), encoding='utf-8')
        if args.smoke_test:
            threading.Timer(3, lambda: quit_app(updating=True)).start()
    window.events.loaded += loaded
    threading.Thread(target=monitor, daemon=True).start()
    try:
        webview.start(gui='edgechromium', private_mode=False, storage_path=str(data_dir('webview')), icon=str(resource_root() / 'assets/app.ico'))
    finally:
        shutting_down.set()
        checkpoint()
        routes.scheduler.shutdown(wait=False)
        tray.stop()
        server.close()
        kernel.CloseHandle(ctypes.c_void_p(mutex))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        if '--smoke-test' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, str(exc), 'Tayyara Motors startup error', 0x10)
        raise
