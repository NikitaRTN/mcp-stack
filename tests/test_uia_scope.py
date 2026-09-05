"""Opt-in live UI test; opens only the disposable MCP fixture."""
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from hub import config, mcp_client

@unittest.skipUnless(os.name == 'nt' and os.getenv('MCP_TEST_LIVE_UI') == '1', 'opt-in Windows desktop test')
class UiaScopeTests(unittest.TestCase):
    def test_scoped_read_write_and_missing_window(self):
        root = Path(__file__).resolve().parents[1]
        fixture = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(root/'tools/uia_test_window.ps1')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = None
        try:
            cfg = config.load()
            client = mcp_client.StreamableClient(config.public_url(config.service('windows'), cfg), auth={'mode':'bearer','token':cfg['token']}, timeout=6)
            tools = client.list_tools()
            self.assertIn('get_control_details', [t['name'] for t in tools])
            def call(name, **args):
                result = client.rpc('tools/call', {'name':name, 'arguments':args})
                self.assertFalse(result.get('isError'), result)
                return result['structuredContent']
            deadline = time.monotonic()+8
            windows = []
            while time.monotonic() < deadline:
                windows = call('list_windows', processId=fixture.pid)['windows']
                if windows: break
                time.sleep(.2)
            self.assertTrue(windows)
            scope = {'processId':fixture.pid,'windowName':windows[0]['name']}
            call('set_control_value', **scope, automationId='mcpTestInput', value='Scoped MCP 1.2 verification')
            text = call('get_control_details', **scope, automationId='mcpTestInput')
            self.assertEqual(text['value'], 'Scoped MCP 1.2 verification')
            call('select_control', **scope, automationId='mcpTestCheck', state=True)
            check = call('get_control_details', **scope, automationId='mcpTestCheck')
            self.assertEqual(check['toggleState'], 'On')
            empty = call('list_controls', processId=fixture.pid, windowName='NONEXISTENT_MCP_TEST_WINDOW')
            self.assertEqual(empty['controls'], [])
            (root/'logs/uia-scope-test.json').write_text(json.dumps({'ok':True,'version':client.server_info.get('version'),'readWrite':True,'toggle':True,'missingWindowIsEmpty':True}),encoding='utf-8')
        finally:
            if client: client.close()
            if fixture.poll() is None:
                fixture.terminate()
                fixture.wait(timeout=5)
