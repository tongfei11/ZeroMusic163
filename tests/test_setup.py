import http.client
import json
from pathlib import Path
import shutil
import time
from tempfile import TemporaryDirectory
import unittest

from netease import Netease, NeteaseError
from setup_web import SetupWeb


STUB = """#!/usr/bin/env node
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const [action, flag, field, value] = process.argv.slice(2);
if (action === 'config' && field === 'privateKey') {
  if (value !== 'test\\nkey') process.exit(1);
  const directory = path.join(os.homedir(), '.config/ncm-cli');
  fs.mkdirSync(directory, {recursive: true});
  fs.writeFileSync(path.join(directory, 'credentials.enc.json'), 'mock');
}
if (action === 'login') console.log(JSON.stringify(flag === '--check'
  ? {success: false, message: '未登录'}
  : {success: true, qrCodeUrl: 'https://163cn.tv/mock'}));
"""


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.web = SetupWeb(self.configure, configured=True)
        self.addCleanup(self.web.close)

    def configure(self, app_id, key, *, is_active):
        self.assertTrue(is_active())
        if app_id == "invalid":
            raise ValueError(key)
        self.received.append((app_id, key))

    def request(self, method, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.web._server.server_port, timeout=4)
        try:
            connection.request(method, "/", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def test_web_rejects_wrong_code_and_never_echoes_key(self):
        status, page = self.request("GET")
        self.assertEqual(status, 200)
        self.assertEqual(page.count("https://developer.music.163.com/"), 1)
        self.assertIn("已配置", page)
        request = {"code": self.web.code + "0", "app_id": "invalid", "key": "test\nkey"}
        self.assertEqual(self.request("POST", json.dumps(request))[0], 403)
        request["code"] = self.web.code
        status, response = self.request("POST", json.dumps(request))
        self.assertEqual(status, 422)
        self.assertNotIn("test", response)
        request["app_id"] = "valid"
        self.assertEqual(self.request("POST", json.dumps(request))[0], 200)
        self.assertTrue(self.web.saved.wait(1))
        self.assertEqual(self.received, [("valid", "test\nkey")])


@unittest.skipUnless(shutil.which("node"), "requires Node.js")
class BackendSetupTest(unittest.TestCase):
    def test_cli_stdin_and_qr_validation_preserve_previous_profile(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            cli = root / "fake-ncm"
            cli.write_text(STUB)
            cli.chmod(0o700)
            backend = Netease(root / "accounts", str(cli))
            backend.configure("test-app", "test\nkey")
            active = backend._current.resolve()
            self.addCleanup(shutil.rmtree, backend._runtime_path(active), True)
            self.assertTrue(backend.configured())
            self.assertEqual(backend.login(), "https://163cn.tv/mock")
            self.assertFalse(backend.login_status()["success"])
            with self.assertRaises(NeteaseError):
                backend.configure("bad-app", "invalid")
            with self.assertRaises(NeteaseError):
                backend.configure("test-app", "test\nkey", is_active=lambda: False)
            self.assertEqual(backend._current.resolve(), active)


class MediaCacheTest(unittest.TestCase):
    def test_replay_reuses_an_unexpired_authorized_address(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = Netease(root, "fake-cli")
            backend._current.mkdir()
            backend._runtime = lambda _home: root
            requests = []
            played = []

            def resolve(*args, media_file, **_kwargs):
                requests.append(args)
                media_file.write_text(json.dumps({
                    "url": "https://p2.music.126.net/test",
                    "expiresAt": time.time() * 1000 + 60000
                }))

            backend._run = resolve
            backend._play_url = lambda _home, url, volume, position: played.append((volume, position))
            song = {"id": "test", "originalId": "123"}
            backend.preload(song)
            self.assertEqual(played, [])
            backend.play(song)
            backend.play(song, volume=55, position=42)
            self.assertEqual(len(requests), 1)
            self.assertEqual(played, [(75, 0), (55, 42)])

            url, _expiry = backend._media_urls[("test", "123")]
            backend._media_urls[("test", "123")] = (url, 0)
            backend.play(song)
            self.assertEqual(len(requests), 2)
