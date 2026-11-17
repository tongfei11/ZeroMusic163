"""One-time credential setup over the device's local network."""

import hmac
import json
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PAGE = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>网易云音乐配置</title>
<style>
body { font: 16px system-ui, sans-serif; margin: 2rem auto; padding: 0 1.25rem;
       max-width: 32rem; color: #202124; background: #fafafa; }
h1 { font-size: 1.55rem; }
form { display: grid; gap: .55rem; margin-top: 1.5rem; }
input, textarea, button { font: inherit; box-sizing: border-box; }
input, textarea { width: 100%; padding: .65rem; border: 1px solid #777;
                  border-radius: .4rem; background: white; }
textarea { resize: vertical; }
textarea.masked { color: transparent; caret-color: #202124; }
@supports (-webkit-text-security: disc) {
  textarea.masked { color: #202124; -webkit-text-security: disc; }
}
button { justify-self: start; padding: .55rem .9rem; }
[type=submit] { background: #be112c; border: 1px solid #be112c;
                color: white; border-radius: .4rem; margin-top: .4rem; }
.error { color: #a40021; margin: 0; }
.error:empty { display: none; }
</style>
<main>
<h1>网易云音乐配置</h1>
<a href="https://developer.music.163.com/st/developer/document?docId=9504d35aa41a47c6ac9830b2dbf48f94"
   target="_blank" rel="noopener noreferrer">网易云开放平台入驻指南</a>
<form id="setup" autocomplete="off" novalidate>
<label for="code">访问码</label>
<input id="code" inputmode="numeric" maxlength="6" aria-describedby="code-error">
<p class="error" id="code-error" role="alert"></p>
<label for="app_id">App ID</label>
<input id="app_id" autocapitalize="none" spellcheck="false" aria-describedby="app_id-error">
<p class="error" id="app_id-error" role="alert"></p>
<label for="key">Private Key</label>
<textarea id="key" class="masked" rows="5" autocapitalize="none" spellcheck="false"
          aria-describedby="key-error" placeholder="KEY_PLACEHOLDER"></textarea>
<button id="toggle" type="button" aria-controls="key" aria-pressed="false">显示私钥</button>
<p class="error" id="key-error" role="alert"></p>
<button type="submit">验证并保存</button>
<p class="error" id="form-error" role="alert"></p>
</form>
<p id="status" role="status"></p>
</main>
<script>
const find = id => document.getElementById(id);
const form = find('setup');
const key = find('key');
find('toggle').addEventListener('click', event => {
  const hidden = key.classList.toggle('masked');
  event.target.textContent = hidden ? '显示私钥' : '隐藏私钥';
  event.target.setAttribute('aria-pressed', String(!hidden));
});
form.addEventListener('submit', async event => {
  event.preventDefault();
  const fields = ['code', 'app_id', 'key'];
  for (const field of [...fields, 'form']) find(field + '-error').textContent = '';
  const submit = form.querySelector('[type=submit]');
  submit.disabled = true;
  find('status').textContent = '正在验证…';
  try {
    const response = await fetch('/', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.fromEntries(fields.map(id => [id, find(id).value])))
    });
    const result = await response.json();
    find('status').textContent = '';
    if (result.ok) {
      form.reset();
      form.hidden = true;
      form.style.display = 'none';
      find('status').textContent = '配置成功，请回到设备扫码';
    } else {
      for (const [field, message] of Object.entries(result.errors)) {
        find(field + '-error').textContent = message;
      }
    }
  } catch {
    find('status').textContent = '';
    find('form-error').textContent = '连接已断开，请在设备上确认配置状态。';
  } finally {
    submit.disabled = false;
  }
});
</script>
</html>
"""


MAX_BODY = 32 * 1024


class SetupWeb:
    """Start a temporary HTTP server. configure raises if credentials cannot be saved."""

    def __init__(self, configure, port=0, *, configured=False):
        self.code = f"{secrets.randbelow(1_000_000):06d}"
        self.saved = threading.Event()
        self._closed = threading.Event()
        self._saving = threading.Lock()
        self._closing = threading.Lock()
        page = PAGE.replace("KEY_PLACEHOLDER", "已配置" if configured else "粘贴私钥")

        class Handler(BaseHTTPRequestHandler):
            def setup(handler):
                handler.request.settimeout(5)
                super().setup()

            def log_message(handler, *_args):
                pass

            def reply(handler, status, body, content_type):
                body = body.encode("utf-8")
                handler.send_response(status)
                handler.send_header("Content-Type", content_type + "; charset=utf-8")
                handler.send_header("Content-Length", str(len(body)))
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Referrer-Policy", "no-referrer")
                handler.send_header("X-Frame-Options", "DENY")
                handler.end_headers()
                handler.wfile.write(body)

            def result(handler, status, **payload):
                handler.reply(status, json.dumps(payload), "application/json")

            def do_GET(handler):
                if handler.path == "/" and not self._closed.is_set():
                    handler.reply(200, page, "text/html")
                else:
                    handler.reply(404, "页面不可用", "text/plain")

            def do_POST(handler):
                if handler.path != "/" or self._closed.is_set():
                    handler.result(404, errors={"form": "设置页已关闭"})
                    return

                try:
                    length = int(handler.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        handler.result(413, errors={"form": "请求大小无效"})
                        return
                    if handler.headers.get_content_type() != "application/json":
                        raise ValueError
                    data = json.loads(handler.rfile.read(length))
                    code, app_id, key = (data[field].strip() for field in ("code", "app_id", "key"))
                except (ValueError, KeyError, TypeError, AttributeError, TimeoutError, RecursionError):
                    handler.result(400, errors={"form": "请求格式无效"})
                    return

                if not hmac.compare_digest(code.encode("utf-8", "surrogatepass"), self.code.encode()):
                    handler.result(403, errors={"code": "访问码有误"})
                    return
                errors = {}
                if not app_id:
                    errors["app_id"] = "请输入 App ID"
                elif "\0" in app_id:
                    errors["app_id"] = "App ID 格式有误"
                if not key:
                    errors["key"] = "请粘贴 Private Key"
                elif "\0" in key:
                    errors["key"] = "Private Key 格式有误"
                if errors:
                    handler.result(400, errors=errors)
                    return

                if not self._saving.acquire(blocking=False):
                    handler.result(409, errors={"form": "正在验证另一份配置，请稍后重试"})
                    return
                try:
                    if self.saved.is_set() or self._closed.is_set():
                        handler.result(409, errors={"form": "设置页已关闭"})
                        return
                    try:
                        configure(app_id, key, is_active=lambda: not self._closed.is_set())
                    except Exception:
                        handler.result(422, errors={
                            "key": "无法验证或保存凭据，请检查 App ID 和 Private Key 后重试"
                        })
                        return
                    try:
                        handler.result(200, ok=True)
                    finally:
                        self.saved.set()
                        self.close()
                finally:
                    self._saving.release()

        self._server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True,
        ).start()

    def address(self):
        if self._closed.is_set():
            return ""
        # UDP connect selects the outbound interface without sending a packet.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect(("192.0.2.1", 9))
                host = route.getsockname()[0]
        except OSError:
            return ""
        return f"http://{host}:{self._server.server_port}"

    def close(self):
        with self._closing:
            if self._closed.is_set():
                return
            self._closed.set()

        def stop():
            self._server.shutdown()
            self._server.server_close()

        threading.Thread(target=stop, daemon=True).start()
