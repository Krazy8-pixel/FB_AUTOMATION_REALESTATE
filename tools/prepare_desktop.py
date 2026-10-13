"""Fetch runtime/compiler prerequisites; clients use their installed Chrome."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def download(url, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.suffix != '.zip' or zipfile.is_zipfile(path)):
        return
    partial = path.with_suffix('.partial')
    if path.exists():
        path.replace(partial)
    print('Downloading', path.name, flush=True)
    for attempt in range(80):
        offset = partial.stat().st_size if partial.exists() else 0
        request = urllib.request.Request(url, headers={'Range': f'bytes={offset}-'} if offset else {})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                if offset and response.status != 206:
                    offset = 0
                total = int(response.headers.get('Content-Range', '').split('/')[-1]) if response.headers.get('Content-Range') else offset + int(response.headers.get('Content-Length', 0))
                with partial.open('ab' if offset else 'wb') as stream:
                    while block := response.read(1024 * 1024):
                        stream.write(block)
            size = partial.stat().st_size
            print(path.name, size, '/', total, flush=True)
            if total and size < total:
                continue
            if path.suffix == '.zip' and not zipfile.is_zipfile(partial):
                continue
            partial.replace(path)
            return
        except Exception as exc:
            print('Retry', path.name, str(exc), flush=True)
            time.sleep(min(10, attempt + 1))
    raise RuntimeError(f'Download could not finish: {path.name}')


def main():
    folder = ROOT / 'packaging/prerequisites'
    jobs = [
        ('https://go.microsoft.com/fwlink/p/?LinkId=2124703', folder / 'MicrosoftEdgeWebview2Setup.exe'),
        ('https://downloads.sourceforge.net/project/nsis/NSIS%203/3.12/nsis-3.12.zip', folder / 'nsis.zip'),
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda item: download(*item), jobs))
    for name, destination in [('nsis.zip', ROOT / 'packaging/compiler')]:
        with zipfile.ZipFile(folder / name) as archive:
            archive.extractall(destination)
    print('Runtime prerequisite and installer compiler ready.', flush=True)


if __name__ == '__main__':
    main()
