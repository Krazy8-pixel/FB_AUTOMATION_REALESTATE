import json
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from automation.chromedriver import Service, SeleniumService


class DriverServiceTests(unittest.TestCase):
    def test_local_readiness_and_shutdown_work_even_when_ssl_initialization_fails(self):
        requested = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requested.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'value':{'ready':True}}).encode())
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            service = Service('unused-driver.exe', port=server.server_port)
            with patch.object(ssl, 'create_default_context', side_effect=FileNotFoundError('broken frozen SSL')), patch.object(ssl, '_create_default_https_context', side_effect=FileNotFoundError('broken frozen SSL')):
                self.assertFalse(SeleniumService.is_connectable(service))
                self.assertTrue(service.is_connectable())
                service.send_remote_shutdown_command()
            self.assertEqual(requested, ['/status', '/shutdown'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_service_requires_a_ready_http_response_and_handles_refused_connections(self):
        with patch('automation.chromedriver.HTTPConnection') as factory:
            response = factory.return_value.getresponse.return_value
            service = Service('unused-driver.exe')
            for body, status in [(b'{"value":{"ready":false}}',200), (b'{}',200), (b'not json',200), (b'{"value":{"ready":true}}',500)]:
                response.status = status
                response.read.return_value = body
                self.assertFalse(service.is_connectable())
            factory.return_value.request.side_effect = ConnectionRefusedError()
            self.assertFalse(service.is_connectable())
            service.send_remote_shutdown_command()
