import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sha = 'a' * 40
        parts = []
        sums = []
        for name, offset in [('bootloader.bin',0),('partitions.bin',32768),('boot_app0.bin',57344),('firmware.bin',65536)]:
            path = self.root/'firmware'/name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(('test-'+name).encode())
            parts.append({'path':'firmware/'+name,'offset':offset})
            sums.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  firmware/'+name)
        (self.root/'manifest.json').write_text(json.dumps({'buildSha':self.sha,'builds':[{'chipFamily':'ESP32-S3','parts':parts}]}))
        (self.root/'build-info.json').write_text(json.dumps({'commit':self.sha}))
        (self.root/'SHA256SUMS.txt').write_text('\n'.join(sums))
        (self.root/'index.html').write_text('今のホイールと重り <esp-web-install-button manifest="manifest.json">',encoding='utf-8')
        self.server = ThreadingHTTPServer(('127.0.0.1',0),partial(QuietHandler,directory=str(self.root)))
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def verify(self, sha=None):
        tool = Path(__file__).resolve().parents[1]/'tools/verify_publication.py'
        return subprocess.run([sys.executable,str(tool),'--url',f'http://127.0.0.1:{self.server.server_port}/',
                               '--sha',sha or self.sha,'--attempts','1'],capture_output=True,text=True)

    def test_complete_publication(self):
        result = self.verify()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('verified_assets',result.stdout)

    def test_incorrect_commit(self):
        self.assertNotEqual(self.verify('b'*40).returncode,0)

    def test_corrupt_binary(self):
        (self.root/'firmware/firmware.bin').write_bytes(b'incorrect binary')
        self.assertNotEqual(self.verify().returncode,0)


if __name__ == '__main__':
    unittest.main()
