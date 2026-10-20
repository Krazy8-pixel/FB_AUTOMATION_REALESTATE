"""Launch an updater or restarted app independently of its frozen parent."""
import ctypes
import os
import subprocess
import sys


def launch_independent(args, cwd):
    environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    kernel = None
    previous = None
    if sys.platform == 'win32' and getattr(sys, 'frozen', False):
        kernel = ctypes.windll.kernel32
        previous = ctypes.create_unicode_buffer(32768)
        kernel.GetDllDirectoryW(len(previous), previous)
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError()
    try:
        return subprocess.Popen(
            args, cwd=str(cwd), env=environment, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    finally:
        if kernel is not None:
            kernel.SetDllDirectoryW(previous.value or None)
