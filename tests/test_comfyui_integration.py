# -*- coding: utf-8 -*-
"""Контракт адаптера, маршруты и границы сетевых загрузок."""
import copy
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

from hub import caddyfile, config, stabilitymatrix_mcp as comfy, supervisor


class ComfyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['token'] = 'test-token'
        self.svc = config.service('stabilitymatrix', self.cfg)
        patcher = mock.patch.object(config, 'load', return_value=self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disabled_adapter_has_no_routes_or_listener(self):
        rendered = caddyfile.render(self.cfg)
        self.assertNotIn('handle /comfyui', rendered)
        self.assertNotIn('handle /stabilitymatrix-output', rendered)
        with mock.patch.object(comfy, 'serve') as serve:
            with self.assertRaises(ValueError):
                supervisor.start_service('stabilitymatrix')
        serve.assert_not_called()

    def test_reverse_proxy_and_aliases_keep_auth(self):
        self.cfg.update(domain='mcp.example.com', behindProxy=True, bind='0.0.0.0')
        self.svc.update(enabled=True, authMode='token')
        rendered = caddyfile.render(self.cfg)
        self.assertIn('http://:8443 {', rendered)
        self.assertNotIn('issuer acme', rendered)
        for route in ('/comfyui', '/stabilitymatrix'):
            block = rendered.split('handle %s* {' % route)[1].split('\n\t}')[0]
            self.assertIn('respond @unauthorized', block)
            self.assertIn('/_inspect/stabilitymatrix', block)
        self.assertIn('reverse_proxy 127.0.0.1:8780', rendered)
        self.assertIn('handle /oauth/* {\n\t\treverse_proxy 127.0.0.1:8765', rendered)

    def test_local_profile_migrates_and_new_services_stay_disabled(self):
        stored = [dict(self.svc, kind='builtin', enabled=True, path='/stabilitymatrix')]
        merged = config._merge_services(stored)
        service = next(s for s in merged if s['id'] == 'stabilitymatrix')
        self.assertEqual(service['kind'], 'remote')
        self.assertEqual(service['path'], '/stabilitymatrix')
        self.assertTrue(service['enabled'])
        self.assertTrue(all(not s['enabled'] for s in merged if s['id'] != 'stabilitymatrix'))

    def test_auto_start_disabled_or_remote_never_spawns_local_process(self):
        for patch in ({'comfyuiAutoStart': False}, {'comfyuiAutoStart': True, 'comfyuiApiUrl': 'https://comfy.example.com'}):
            self.cfg.update(patch)
            with mock.patch.object(comfy, 'backend_online', return_value=False), \
                    mock.patch.object(comfy.subprocess, 'Popen') as spawn:
                with self.assertRaises(comfy.ToolError):
                    comfy.start_backend()
                spawn.assert_not_called()

    def test_api_url_rejects_embedded_credentials_and_bad_port(self):
        for value in ('file:///etc/passwd', 'http://user:password@example.com', 'http://localhost:wrong'):
            self.cfg['comfyuiApiUrl'] = value
            with self.assertRaises(comfy.ToolError):
                comfy.comfyui_api_url()

    def test_http_tool_discovery_notifications_and_invalid_requests(self):
        server, thread = comfy.serve(0)
        try:
            address = 'http://127.0.0.1:%d/mcp' % server.server_port
            def post(value):
                return urlopen(Request(address, data=json.dumps(value).encode(),
                                       headers={'Content-Type': 'application/json'}), timeout=3)
            with post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) as response:
                self.assertEqual(response.status, 200)
                names = {t['name'] for t in json.load(response)['result']['tools']}
                self.assertIn('comfyui_generate', names)
            with post({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(response.read(), b'')
            with post({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'comfyui_generate', 'arguments': []}}) as response:
                self.assertEqual(json.load(response)['error']['code'], -32602)
            with self.assertRaises(HTTPError) as caught:
                post([])
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_pinned_download_keeps_tls_hostname_and_uses_validated_ip(self):
        handler = comfy._PinnedHTTPSHandler(['8.8.8.8'])
        def open_connection(factory, request):
            conn = factory('models.example.com', timeout=3)
            self.assertEqual(conn.host, 'models.example.com')
            return conn._create_connection(('models.example.com', 443), 3)
        with mock.patch.object(handler, 'do_open', side_effect=open_connection), \
                mock.patch.object(comfy.socket, 'create_connection') as connect:
            handler.https_open(Request('https://models.example.com/model.safetensors'))
        connect.assert_called_once_with(('8.8.8.8', 443), 3, None)

    def test_redirect_to_private_network_is_rejected(self):
        redirect = HTTPError('https://models.example.com/a', 302, 'redirect',
                             {'Location': 'https://127.0.0.1/model.safetensors'}, None)
        with mock.patch.object(comfy.socket, 'getaddrinfo', side_effect=[
                [(2, 1, 6, '', ('8.8.8.8', 443))],
                [(2, 1, 6, '', ('127.0.0.1', 443))]]), \
                mock.patch.object(comfy, 'build_opener') as opener:
            opener.return_value.open.side_effect = redirect
            with self.assertRaises(comfy.ToolError) as caught:
                comfy._open_download('https://models.example.com/a')
            self.assertEqual(caught.exception.code, 'download_private_address')


if __name__ == '__main__':
    unittest.main()
