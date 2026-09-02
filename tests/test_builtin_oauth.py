import hashlib
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from hub import caddyfile, config, oauth


class BuiltinOAuthTests(unittest.TestCase):
    def sample(self):
        cfg = json.loads(json.dumps(config.DEFAULTS))
        cfg.update({"domain": "mcp.riseshield.ru", "httpsPort": 8443,
                    "token": "test", "oauthSigningKey": "unit-signing-key"})
        for service in cfg["services"]:
            service["enabled"] = False
        cfg["services"][1].update({"enabled": True, "authMode": "oauth",
                                    "oauth": {"mode": "builtin",
                                              "requiredScopes": "mcp:tools"}})
        return cfg

    def test_discovery_dcr_pkce_and_resource_binding(self):
        cfg = self.sample()
        resource = "https://mcp.riseshield.ru/roblox"
        verifier = "unit-verifier-" + "x" * 52
        challenge = oauth._b64(hashlib.sha256(verifier.encode()).digest())
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(config, "DATA", Path(folder)), \
                mock.patch.object(config, "load", return_value=cfg):
            with oauth._oauth_lock:
                oauth._codes.clear()
            client = oauth.register_client({
                "client_name": "ChatGPT",
                "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            })
            details = oauth.authorization_request({
                "response_type": "code", "client_id": client["client_id"],
                "redirect_uri": client["redirect_uris"][0],
                "code_challenge": challenge, "code_challenge_method": "S256",
                "state": "state", "resource": resource, "scope": "mcp:tools",
            }, cfg)
            redirect = oauth.authorized_redirect(details)
            code = urllib.parse.parse_qs(urllib.parse.urlsplit(redirect).query)["code"][0]
            tokens = oauth.token_request({
                "grant_type": "authorization_code", "client_id": client["client_id"],
                "redirect_uri": client["redirect_uris"][0], "code": code,
                "code_verifier": verifier, "resource": resource,
            })
            allowed = oauth.validate_incoming(
                {"mode": "builtin", "requiredScopes": "mcp:tools"},
                "Bearer " + tokens["access_token"], resource=resource)
            denied = oauth.validate_incoming(
                {"mode": "builtin", "requiredScopes": "mcp:tools"},
                "Bearer " + tokens["access_token"],
                resource="https://mcp.riseshield.ru/real")
            metadata = oauth.authorization_server_metadata(cfg)
            protected = oauth.protected_resource_metadata(
                "/.well-known/oauth-protected-resource/roblox", cfg)
        self.assertEqual(allowed[:2], (True, 200))
        self.assertEqual(denied[:2], (False, 401))
        self.assertEqual(metadata["registration_endpoint"],
                         "https://mcp.riseshield.ru/oauth/register")
        self.assertEqual(protected["resource"], resource)

    def test_caddy_exposes_discovery_before_mcp(self):
        text = caddyfile.render(self.sample())
        self.assertLess(text.index("handle /.well-known/oauth-protected-resource*"),
                        text.index("handle /roblox*"))
        self.assertIn("handle /oauth/*", text)

    def test_builtin_config_needs_no_introspection_url(self):
        cfg = self.sample()
        payload = {"id": "secure", "label": "Secure", "kind": "remote",
                   "path": "/secure", "upstream": "http://127.0.0.1:9000/mcp",
                   "authMode": "oauth", "oauth": {"mode": "builtin"}}
        with mock.patch.object(config, "load", return_value=cfg), \
                mock.patch.object(config, "save"):
            created = config.add_service(payload)
        self.assertEqual(created["oauth"]["mode"], "builtin")


if __name__ == "__main__":
    unittest.main()
