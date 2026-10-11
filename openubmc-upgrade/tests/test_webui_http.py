"""Verify cookie, CSRF, multipart and cleanup over a loopback HTTP server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from openubmc_upgrade.webui import WebUiHttpSession, upload_multipart_parts

class WebUiHttpTests(unittest.TestCase):
    def test_real_http_retains_cookie_csrf_content_length_and_cleans_session(self):
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                observed.append((self.path, dict(self.headers), body))
                if self.path == '/UI/Rest/Login':
                    payload = {'Token': 'local-csrf', 'Session': {'SessionID': 'local-session'}}
                elif self.path == '/UI/Rest/FirmwareInventory':
                    payload = {'FilePath': '/tmp/web/test.hpm'}
                else:
                    payload = {'TaskId': 1}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Set-Cookie', 'SessionId=local-session; Path=/; HttpOnly')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        deletes = []
        def redfish_request(method, path, **kwargs):
            deletes.append((method, path))
        session = WebUiHttpSession(origin='http://127.0.0.1:'+str(server.server_port),
            username='local-test', password='local-test', redfish_request=redfish_request)
        session.login()
        prefix, suffix, boundary = upload_multipart_parts('test.hpm')
        payload = b'local-hpm'
        length = len(prefix)+len(payload)+len(suffix)
        session.upload(body=(prefix,payload,suffix), boundary=boundary, content_length=length, timeout=5)
        session.start('/tmp/web/test.hpm')
        cleanup = session.close()
        self.assertTrue(cleanup['completed'])
        self.assertEqual(deletes, [('DELETE','/redfish/v1/SessionService/Sessions/local-session')])
        headers = {k.lower():v for k,v in observed[1][1].items()}
        self.assertEqual(headers['cookie'], 'SessionId=local-session')
        self.assertEqual(headers['x-csrf-token'], 'local-csrf')
        self.assertEqual(headers['content-length'], str(length))
        self.assertNotIn('transfer-encoding', headers)
        self.assertEqual(observed[1][2], prefix+payload+suffix)
        self.assertIn(b'name="imgfile"', observed[1][2])
        self.assertEqual(json.loads(observed[2][2]), {'FilePath':'/tmp/web/test.hpm'})

if __name__ == '__main__':
    unittest.main()
