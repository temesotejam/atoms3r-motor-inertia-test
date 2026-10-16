"""Serve the packaged local flasher only on loopback; no motor/USB action."""
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import hashlib
import json
import threading
import webbrowser

root = Path(__file__).resolve().parents[1]/'flash'
manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
for line in (root/'SHA256SUMS.txt').read_text().splitlines():
    digest, name = line.split(maxsplit=1)
    if hashlib.sha256((root/name.strip()).read_bytes()).hexdigest() != digest:
        raise SystemExit('Firmware checksum mismatch: '+name)
for port in range(8765, 8786):
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), partial(SimpleHTTPRequestHandler, directory=str(root)))
        break
    except OSError:
        pass
else:
    raise SystemExit('No local port is available')
url = f'http://127.0.0.1:{port}/'
print('Open in Chrome / Edge: '+url, flush=True)
print('Keep this window open while installing. Ctrl+C stops the local server.', flush=True)
threading.Timer(.5, lambda: webbrowser.open(url)).start()
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
