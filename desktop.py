"""Desktop entry point: local server, persistent UI, and system tray."""
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
    parser.add_argument('--driver-smoke-test', action='store_true')
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
    kernel = None
    mutex = None
    instance_socket = None
    if sys.platform == 'win32':
        kernel = ctypes.windll.kernel32
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex_name = 'Local\\FBAutomationRealEstateDesktop'
        if args.smoke_test or args.driver_smoke_test:
            mutex_name += f'SmokeTest-{os.getpid()}'
        mutex = kernel.CreateMutexW(None, False, mutex_name)
        already_running = kernel.GetLastError() == 183
    else:
        instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        instance_port = 47639 if not (args.smoke_test or args.driver_smoke_test) else 47640 + os.getpid() % 10000
        try:
            instance_socket.bind(('127.0.0.1', instance_port))
            already_running = False
        except OSError:
            already_running = True
    if already_running:
        # A second launch asks the already-running tray instance to show itself.
        (folder / 'show-window').touch()
        return
    from app import app
    from web import routes
    if args.driver_smoke_test:
        from automation.driver_smoke import run
        try:
            run(folder)
        finally:
            routes.scheduler.shutdown(wait=False)
            if kernel:
                kernel.CloseHandle(ctypes.c_void_p(mutex))
            if instance_socket:
                instance_socket.close()
        return
    if args.background:
        stop_foreground_work(routes)
    from waitress import create_server
    import pystray
    import webview
    from PIL import Image
    # Allow the authenticated diagnostic-report link to open Save As in WebView2.
    webview.settings['ALLOW_DOWNLOADS'] = True
    from automation import app_updates
    from automation.desktop_state import checkpoint
    from automation.maintenance import busy

    server = create_server(app, host='127.0.0.1', port=0, threads=12)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    port = server.effective_port
    shutting_down = threading.Event()
    window = webview.create_window('FBMK Automation Realestate', f'http://127.0.0.1:{port}/', width=1380, height=900, min_size=(390, 600), hidden=args.background)

    def show(icon=None, item=None):
        set_window_open(True)
        if tray:
            tray.visible = True
        window.show()
        window.restore()

    def quit_app(icon=None, item=None, updating=False):
        from automation.account_browser import stop as stop_account_browser
        try:
            stop_account_browser(force=True)
        except Exception:
            pass
        if not updating:
            stop_foreground_work(routes)
        shutting_down.set()
        routes.scheduler.pause()
        checkpoint()
        if tray:
            tray.stop()
        window.destroy()

    def closing():
        if not shutting_down.is_set():
            from automation.account_browser import stop as stop_account_browser
            try:
                stop_account_browser(force=True)
            except Exception:
                pass
            stop_foreground_work(routes)
            if sys.platform == 'darwin':
                shutting_down.set()
                routes.scheduler.pause()
                checkpoint()
                return True
            window.hide()
            return False
        return True

    window.events.closing += closing
    tray = pystray.Icon('FBAutomationRealEstate', Image.open(resource_root() / 'assets/app-logo.png'), 'FBMK Automation Realestate — background sync', menu=pystray.Menu(pystray.MenuItem('Open FBMK Automation Realestate', show, default=True), pystray.MenuItem('Exit App', lambda icon, item: quit_app(icon, item))))
    if sys.platform != 'darwin':
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
        gui = 'edgechromium' if sys.platform == 'win32' else 'cocoa' if sys.platform == 'darwin' else None
        icon_path = resource_root() / 'assets' / ('app.ico' if sys.platform == 'win32' else 'app-logo.png')
        webview.start(gui=gui, private_mode=False, storage_path=str(data_dir('webview')), icon=str(icon_path))
    finally:
        from automation.account_browser import stop as stop_account_browser
        try:
            stop_account_browser(force=True)
        except Exception:
            pass
        shutting_down.set()
        checkpoint()
        routes.scheduler.shutdown(wait=False)
        if tray:
            tray.stop()
        server.close()
        if kernel:
            kernel.CloseHandle(ctypes.c_void_p(mutex))
        if instance_socket:
            instance_socket.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        if sys.platform == 'win32' and '--smoke-test' not in sys.argv and '--driver-smoke-test' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, str(exc), 'FBMK Automation Realestate startup error', 0x10)
        raise
