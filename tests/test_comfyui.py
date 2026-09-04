# -*- coding: utf-8 -*-
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from hub import config, caddyfile, oauth, stabilitymatrix_mcp


class StabilityMatrixTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(config, 'load', return_value=dict(json.loads(json.dumps(config.DEFAULTS)), domain="mcp.example.com"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_legacy_and_comfyui_tool_names_share_profile(self):
        with mock.patch.object(stabilitymatrix_mcp, "load_profile",
                               return_value=stabilitymatrix_mcp.DEFAULT_PROFILE):
            self.assertEqual(stabilitymatrix_mcp.call_tool("comfyui_config_get", {}),
                             stabilitymatrix_mcp.call_tool("stabilitymatrix_config_get", {}))


    def test_builtin_route_and_upstream_are_shipped(self):
        service = next(s for s in config.DEFAULT_SERVICES if s["id"] == "stabilitymatrix")
        self.assertEqual(service["path"], "/comfyui")
        self.assertEqual(config.upstream_of(service), "http://127.0.0.1:8780/mcp")


    def test_initialize_and_tool_list(self):
        initialized = stabilitymatrix_mcp.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(initialized["result"]["serverInfo"]["name"],
                         "mcp-hub-comfyui")
        self.assertIn("resources", initialized["result"]["capabilities"])
        self.assertIn("@generate", initialized["result"]["instructions"])
        listed = stabilitymatrix_mcp.dispatch({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertTrue({"comfyui_config_set", "comfyui_prompt_set",
                         "comfyui_generate", "comfyui_generate_from_chat_image",
                         "comfyui_job_status", "comfyui_model_download",
                         "comfyui_model_download_status", "comfyui_compare",
                         "comfyui_compare_status"} <= names)
        generate = next(tool for tool in listed["result"]["tools"]
                        if tool["name"] == "comfyui_generate")
        self.assertEqual(generate["_meta"]["ui"]["resourceUri"],
                         stabilitymatrix_mcp.IMAGE_UI_URI)
        self.assertIn("outputSchema", generate)
        self.assertIn("@generate", generate["title"])
        self.assertFalse(generate["annotations"]["readOnlyHint"])
        reference = next(tool for tool in listed["result"]["tools"]
                         if tool["name"] == "comfyui_generate_from_chat_image")
        self.assertIn("image-to-prompt", reference["description"])
        self.assertEqual(set(reference["inputSchema"]["required"]),
                         {"reference_analysis", "positive_prompt"})
        self.assertEqual(reference["_meta"]["ui"]["resourceUri"],
                         stabilitymatrix_mcp.IMAGE_UI_URI)
        models = next(tool for tool in listed["result"]["tools"]
                      if tool["name"] == "comfyui_models")
        self.assertIn("@models", models["title"])
        self.assertTrue(models["annotations"]["readOnlyHint"])
        download = next(tool for tool in listed["result"]["tools"]
                        if tool["name"] == "comfyui_model_download")
        self.assertIn("@download", download["title"])
        self.assertIn("loras", download["inputSchema"]["properties"]["model_type"]["enum"])
        self.assertIn("vae", download["inputSchema"]["properties"]["model_type"]["enum"])
        self.assertTrue(download["annotations"]["openWorldHint"])
        self.assertFalse(download["annotations"]["destructiveHint"])
        self.assertEqual(download["_meta"]["ui"]["resourceUri"],
                         stabilitymatrix_mcp.DOWNLOAD_UI_URI)
        compare = next(tool for tool in listed["result"]["tools"]
                       if tool["name"] == "comfyui_compare")
        self.assertIn("@vs", compare["title"])
        self.assertEqual(compare["_meta"]["ui"]["resourceUri"],
                         stabilitymatrix_mcp.COMPARISON_UI_URI)
        self.assertTrue(compare["inputSchema"]["properties"]["same_seed"]["default"])


    def test_ui_resource_is_served_as_mcp_app(self):
        listed = stabilitymatrix_mcp.dispatch({
            "jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {},
        })
        resource = next(item for item in listed["result"]["resources"]
                        if item["uri"] == stabilitymatrix_mcp.IMAGE_UI_URI)
        self.assertEqual(resource["uri"], stabilitymatrix_mcp.IMAGE_UI_URI)
        self.assertEqual(resource["mimeType"], "text/html;profile=mcp-app")
        read = stabilitymatrix_mcp.dispatch({
            "jsonrpc": "2.0", "id": 4, "method": "resources/read",
            "params": {"uri": stabilitymatrix_mcp.IMAGE_UI_URI},
        })
        content = read["result"]["contents"][0]
        self.assertTrue(stabilitymatrix_mcp.IMAGE_UI_URI.endswith(
            "generated-image-v6.html"))
        self.assertIn("ui/notifications/tool-result", content["text"])
        self.assertIn("openExternal", content["text"])
        self.assertIn("requestDisplayMode", content["text"])
        self.assertIn('requestDisplayMode({ mode: "inline" })', content["text"])
        self.assertIn("closeNode.hidden = false", content["text"])
        self.assertNotIn("requestClose", content["text"])
        self.assertIn('displayMode = "fullscreen";\n        updateCloseButton();',
                      content["text"])
        self.assertIn('addEventListener("wheel"', content["text"])
        self.assertIn("passive: false", content["text"])
        self.assertIn('addEventListener("pointerdown"', content["text"])
        self.assertIn('addEventListener("pointermove"', content["text"])
        self.assertIn("setPointerCapture", content["text"])
        self.assertIn("const focalX", content["text"])
        self.assertIn("--pan-x", content["text"])
        self.assertIn("translate(var(--pan-x), var(--pan-y))", content["text"])
        self.assertIn("setInterval", content["text"])
        self.assertNotIn('method: "ui/initialize"', content["text"])
        self.assertIn("event?.detail?.globals", content["text"])
        self.assertIn("toolResponseMetadata", content["text"])
        self.assertIn(oauth.public_base(),
                      content["_meta"]["ui"]["csp"]["resourceDomains"])
        self.assertIn(stabilitymatrix_mcp.comfyui_web_origin(),
                      content["_meta"]["openai/widgetCSP"]["redirect_domains"])

        legacy_image_uri = "ui://stabilitymatrix/generated-image-v2.html"
        legacy_image = stabilitymatrix_mcp.dispatch({
            "jsonrpc": "2.0", "id": 41, "method": "resources/read",
            "params": {"uri": legacy_image_uri},
        })["result"]["contents"][0]
        self.assertEqual(legacy_image["uri"], legacy_image_uri)
        self.assertEqual(legacy_image["text"], content["text"])
        self.assertIn("requestDisplayMode", legacy_image["text"])

        download = stabilitymatrix_mcp.dispatch({
            "jsonrpc": "2.0", "id": 5, "method": "resources/read",
            "params": {"uri": stabilitymatrix_mcp.DOWNLOAD_UI_URI},
        })["result"]["contents"][0]
        self.assertIn("fetch(progressUrl", download["text"])
        self.assertIn("setTimeout(poll, 2000)", download["text"])
        self.assertIn("event?.detail?.globals", download["text"])
        self.assertIn(oauth.public_base(),
                      download["_meta"]["ui"]["csp"]["connectDomains"])

        comparison = stabilitymatrix_mcp.dispatch({
            "jsonrpc": "2.0", "id": 6, "method": "resources/read",
            "params": {"uri": stabilitymatrix_mcp.COMPARISON_UI_URI},
        })["result"]["contents"][0]
        self.assertTrue(stabilitymatrix_mcp.COMPARISON_UI_URI.endswith(
            "image-comparison-v7.html"))
        self.assertIn('type="range"', comparison["text"])
        self.assertIn("clip-path", comparison["text"])
        self.assertIn("--split", comparison["text"])
        self.assertIn("event?.detail?.globals", comparison["text"])
        self.assertIn("mcp_tool_result", comparison["text"])
        self.assertIn('"data", "content", "text"', comparison["text"])
        self.assertIn("setInterval", comparison["text"])
        self.assertNotIn('method: "ui/initialize"', comparison["text"])
        self.assertIn("requestDisplayMode", comparison["text"])
        self.assertIn('requestDisplayMode({ mode: "inline" })', comparison["text"])
        self.assertIn("closeNode.hidden = false", comparison["text"])
        self.assertNotIn("requestClose", comparison["text"])
        self.assertIn('displayMode = "fullscreen";\n        updateCloseButton();',
                      comparison["text"])
        self.assertIn('addEventListener("wheel"', comparison["text"])
        self.assertIn("passive: false", comparison["text"])
        self.assertIn("Math.min(5", comparison["text"])
        self.assertIn('addEventListener("pointerdown"', comparison["text"])
        self.assertIn('addEventListener("pointermove"', comparison["text"])
        self.assertIn("setPointerCapture", comparison["text"])
        self.assertIn("const focalX", comparison["text"])
        self.assertIn("--pan-x", comparison["text"])
        self.assertIn(oauth.public_base(),
                      comparison["_meta"]["ui"]["csp"]["resourceDomains"])

        legacy_comparison_uri = "ui://stabilitymatrix/image-comparison-v3.html"
        legacy_comparison = stabilitymatrix_mcp.dispatch({
            "jsonrpc": "2.0", "id": 61, "method": "resources/read",
            "params": {"uri": legacy_comparison_uri},
        })["result"]["contents"][0]
        self.assertEqual(legacy_comparison["uri"], legacy_comparison_uri)
        self.assertEqual(legacy_comparison["text"], comparison["text"])


    def test_download_rejects_private_network_and_unsafe_filenames(self):
        with mock.patch.object(stabilitymatrix_mcp.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(stabilitymatrix_mcp.ToolError) as raised:
                stabilitymatrix_mcp._validate_download_url("https://localhost/model.safetensors")
        self.assertEqual(raised.exception.code, "download_private_address")
        with self.assertRaises(stabilitymatrix_mcp.ToolError):
            stabilitymatrix_mcp._safe_model_filename("../model.safetensors",
                                                      "https://example.com/model.safetensors")
        with self.assertRaises(stabilitymatrix_mcp.ToolError):
            stabilitymatrix_mcp._safe_model_filename("model.zip",
                                                      "https://example.com/model.zip")


    def test_background_model_download_reports_progress_and_publishes_atomically(self):
        payload = b"model-bytes" * 1024

        class Response(io.BytesIO):
            def __init__(self, value):
                super().__init__(value)
                self.headers = {"Content-Length": str(len(value))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stabilitymatrix_mcp.config, "DATA", Path(tmp) / "data"), \
                mock.patch.object(stabilitymatrix_mcp, "_validate_download_url",
                                  side_effect=lambda value: value), \
                mock.patch.object(stabilitymatrix_mcp, "model_destination",
                                  return_value=Path(tmp) / "Models" / "Lora"), \
                mock.patch.object(stabilitymatrix_mcp, "_open_download",
                                  return_value=(Response(payload), "https://example.com/a.safetensors")):
            store = stabilitymatrix_mcp.DownloadStore()
            started = store.create({
                "url": "https://example.com/a.safetensors",
                "model_type": "loras",
            })
            deadline = time.time() + 5
            current = started
            while current["state"] not in ("completed", "error") and time.time() < deadline:
                time.sleep(0.01)
                current = store.get(started["job_id"])
            self.assertEqual(current["state"], "completed", current.get("error"))
            self.assertEqual(current["bytes_downloaded"], len(payload))
            self.assertEqual(current["percent"], 100.0)
            self.assertIn("/stabilitymatrix-download/", current["progress_url"])
            target = Path(tmp) / "Models" / "Lora" / "a.safetensors"
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(list(target.parent.glob("*.part")), [])


    def test_download_status_token_is_signed(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stabilitymatrix_mcp.config, "DATA", Path(tmp)):
            token = stabilitymatrix_mcp._download_token("a" * 32)
            self.assertEqual(stabilitymatrix_mcp._download_job_from_token(token), "a" * 32)
            with self.assertRaises(stabilitymatrix_mcp.ToolError):
                stabilitymatrix_mcp._download_job_from_token(
                    token[:-1] + ("a" if token[-1] != "a" else "b"))


    def test_generate_waits_inside_one_tool_call(self):
        starting = {"job_id": "job-1", "state": "queued", "prompt_id": "prompt-1",
                    "images": [], "error": None}
        completed = dict(starting, state="completed", images=[{
            "filename": "result.png", "subfolder": "", "type": "output",
            "asset_token": "x" * 43,
        }])
        with mock.patch.object(stabilitymatrix_mcp, "load_profile",
                               return_value=stabilitymatrix_mcp.DEFAULT_PROFILE), \
                mock.patch.object(stabilitymatrix_mcp.JOBS, "create",
                                  return_value=starting) as create, \
                mock.patch.object(stabilitymatrix_mcp.JOBS, "wait",
                                  return_value=completed) as wait, \
                mock.patch.object(stabilitymatrix_mcp, "_embedded_image_content",
                                  return_value=([{"type": "image", "data": "cG5n",
                                                  "mimeType": "image/png"}], [])):
            result = stabilitymatrix_mcp.call_tool(
                "comfyui_generate", {"positive_prompt": "test"})
        create.assert_called_once()
        wait.assert_called_once_with("job-1", stabilitymatrix_mcp.DEFAULT_WAIT_SECONDS)
        self.assertEqual(result["structuredContent"]["state"], "completed")
        self.assertEqual(result["_meta"]["stabilitymatrix/result"]["state"],
                         "completed")
        self.assertEqual(len(result["structuredContent"]["image_urls"]), 1)
        self.assertEqual(result["structuredContent"]["comfyui_url"],
                         stabilitymatrix_mcp.comfyui_web_url())
        self.assertEqual([part["type"] for part in result["content"]],
                         ["text", "image", "resource_link"])
        self.assertIn("data.display_markdown дословно", result["content"][0]["text"])
        self.assertIn("![Результат 1](https://", result["structuredContent"]["display_markdown"])
        self.assertEqual(result["content"][2]["uri"],
                         result["structuredContent"]["image_urls"][0])


    def test_completed_job_returns_native_image_content(self):
        job = {
            "job_id": "job-image", "state": "completed", "prompt_id": "prompt-image",
            "images": [{"filename": "result.png", "subfolder": "MCP-Hub",
                        "type": "output", "asset_token": "x" * 43}],
            "error": None,
        }
        pixels = b"\x89PNG\r\n\x1a\nactual-pixels"
        with mock.patch.object(stabilitymatrix_mcp, "_api_request",
                               return_value=(pixels, "image/png")) as request:
            result = stabilitymatrix_mcp._job_content(job, include_images=True)
        request.assert_called_once()
        self.assertIn("preview=webp%3B90", request.call_args.args[0])
        self.assertEqual([part["type"] for part in result["content"]],
                          ["text", "image", "resource", "resource_link"])
        image = result["content"][1]
        self.assertEqual(image["mimeType"], "image/png")
        self.assertEqual(stabilitymatrix_mcp.base64.b64decode(image["data"]), pixels)
        self.assertEqual(image["annotations"]["audience"], ["user", "assistant"])
        embedded_resource = result["content"][2]
        self.assertEqual(embedded_resource["type"], "resource")
        self.assertEqual(embedded_resource["resource"]["mimeType"], "image/png")
        self.assertEqual(stabilitymatrix_mcp.base64.b64decode(
            embedded_resource["resource"]["blob"]), pixels)
        self.assertEqual(embedded_resource["resource"]["uri"],
                         result["structuredContent"]["image_urls"][0])
        self.assertEqual(embedded_resource["annotations"]["audience"],
                         ["user", "assistant"])
        resource_link = result["content"][3]
        self.assertEqual(resource_link["type"], "resource_link")
        self.assertEqual(resource_link["mimeType"], "image/png")
        self.assertEqual(resource_link["annotations"]["audience"],
                         ["user", "assistant"])
        self.assertIn(resource_link["uri"], result["structuredContent"]["display_markdown"])


    def test_vs_uses_same_seed_and_returns_two_model_images(self):
        created = []

        def create(arguments):
            created.append(arguments)
            index = len(created)
            return {"job_id": "job-%d" % index, "state": "starting_backend",
                    "prompt_id": None, "images": [], "error": None}

        completed = [
            {"job_id": "job-1", "state": "completed", "prompt_id": "prompt-1",
             "images": [{"filename": "a.png", "subfolder": "", "type": "output",
                         "asset_token": "a" * 43}], "error": None},
            {"job_id": "job-2", "state": "completed", "prompt_id": "prompt-2",
             "images": [{"filename": "b.png", "subfolder": "", "type": "output",
                         "asset_token": "b" * 43}], "error": None},
        ]
        profile = json.loads(json.dumps(stabilitymatrix_mcp.DEFAULT_PROFILE))
        with mock.patch.object(stabilitymatrix_mcp, "load_profile", return_value=profile), \
                mock.patch.object(stabilitymatrix_mcp.JOBS, "create",
                                  side_effect=create), \
                mock.patch.object(stabilitymatrix_mcp, "_wait_jobs",
                                  return_value=completed) as wait, \
                mock.patch.object(stabilitymatrix_mcp, "_embedded_image_content",
                                  return_value=([{"type": "image", "data": "YQ==",
                                                  "mimeType": "image/png"},
                                                 {"type": "image", "data": "Yg==",
                                                  "mimeType": "image/png"}], [])):
            result = stabilitymatrix_mcp.call_tool("comfyui_compare", {
                "positive_prompt_a": "portrait",
                "config_a": {"model": "model-a.safetensors"},
                "config_b": {"model": "model-b.safetensors"},
                "label_a": "Model A", "label_b": "Model B",
            })
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0]["config"]["seed"], created[1]["config"]["seed"])
        wait.assert_called_once()
        self.assertEqual(result["structuredContent"]["state"], "completed")
        self.assertEqual(result["_meta"]["stabilitymatrix/result"]["state"],
                         "completed")
        self.assertEqual(result["structuredContent"]["left"]["label"], "Model A")
        self.assertEqual(result["structuredContent"]["right"]["label"], "Model B")
        self.assertEqual([part["type"] for part in result["content"]],
                         ["text", "image", "image", "resource_link", "resource_link"])
        self.assertIn("data.display_markdown дословно с обеими картинками",
                      result["content"][0]["text"])
        self.assertIn("![Model A](", result["structuredContent"]["display_markdown"])
        self.assertIn("![Model B](", result["structuredContent"]["display_markdown"])
        self.assertEqual(result["content"][3]["title"], "Model A")
        self.assertEqual(result["content"][4]["title"], "Model B")


    def test_reference_image_generation_uses_derived_prompt(self):
        starting = {"job_id": "job-ref", "state": "queued", "prompt_id": "prompt-ref",
                    "images": [], "error": None}
        completed = dict(starting, state="completed", images=[])
        arguments = {
            "reference_analysis": "Chibi character, top-down composition, warm autumn palette",
            "positive_prompt": "official game art style, chibi character, autumn palette",
            "negative_prompt": "photorealistic, text, watermark",
        }
        with mock.patch.object(stabilitymatrix_mcp, "load_profile",
                               return_value=stabilitymatrix_mcp.DEFAULT_PROFILE), \
                mock.patch.object(stabilitymatrix_mcp.JOBS, "create",
                                  return_value=starting) as create, \
                mock.patch.object(stabilitymatrix_mcp.JOBS, "wait",
                                  return_value=completed) as wait:
            result = stabilitymatrix_mcp.call_tool(
                "comfyui_generate_from_chat_image", arguments)
        create.assert_called_once_with(arguments)
        wait.assert_called_once_with("job-ref", stabilitymatrix_mcp.DEFAULT_WAIT_SECONDS)
        self.assertEqual(result["structuredContent"]["state"], "completed")

        with self.assertRaises(stabilitymatrix_mcp.ToolError):
            stabilitymatrix_mcp.call_tool(
                "comfyui_generate_from_chat_image",
                {"reference_analysis": "", "positive_prompt": "test"})


    def test_image_asset_tokens_do_not_expose_backend_paths(self):
        store = stabilitymatrix_mcp.JobStore()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stabilitymatrix_mcp.config, "DATA", Path(tmp)):
            token = stabilitymatrix_mcp._image_token({
                "filename": "safe.png", "subfolder": "MCP-Hub", "type": "output",
            })
            self.assertEqual(store.asset(token), {
                "filename": "safe.png", "subfolder": "MCP-Hub", "type": "output",
            })
            with self.assertRaises(stabilitymatrix_mcp.ToolError):
                store.asset(token[:-1] + ("a" if token[-1] != "a" else "b"))


    def test_image_response_allows_cross_origin_desktop_embedding(self):
        headers = stabilitymatrix_mcp.IMAGE_RESPONSE_HEADERS
        self.assertEqual(headers["Content-Disposition"], "inline")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "cross-origin")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")


    def test_config_validation_and_workflow(self):
        value = stabilitymatrix_mcp.validate_config({"width": 768, "steps": 30})
        self.assertEqual(value["width"], 768)
        self.assertEqual(value["steps"], 30)
        with self.assertRaises(stabilitymatrix_mcp.ToolError):
            stabilitymatrix_mcp.validate_config({"width": 770})
        value["model"] = "model.safetensors"
        flow = stabilitymatrix_mcp.build_workflow(value, "cat", "bad", 42, "MCP/Test")
        self.assertEqual(flow["1"]["inputs"]["ckpt_name"], "model.safetensors")
        self.assertEqual(flow["5"]["inputs"]["seed"], 42)
        self.assertEqual(flow["7"]["class_type"], "SaveImage")
        ui_flow = stabilitymatrix_mcp.build_ui_workflow(flow)
        self.assertEqual(ui_flow["version"], 0.4)
        self.assertEqual(ui_flow["last_node_id"], 7)
        self.assertEqual(len(ui_flow["links"]), 9)
        ui_sampler = next(node for node in ui_flow["nodes"] if node["type"] == "KSampler")
        self.assertEqual(ui_sampler["widgets_values"][:3], [42, "fixed", 30])
        self.assertEqual(next(node for node in ui_flow["nodes"]
                              if node["type"] == "SaveImage")["inputs"][0]["link"], 9)

        value.update({
            "model": "anima_aestheticV11.safetensors",
            "_model_kind": "diffusion_model",
            "_text_encoder": "qwen_3_06b_base.safetensors",
            "_clip_type": "stable_diffusion",
            "_vae": "qwen_image_vae.safetensors",
        })
        flow = stabilitymatrix_mcp.build_workflow(value, "cat", "bad", 43, "MCP/Anima")
        self.assertEqual(flow["1"]["class_type"], "UNETLoader")
        self.assertEqual(flow["1"]["inputs"]["unet_name"],
                         "anima_aestheticV11.safetensors")
        self.assertEqual(flow["2"]["inputs"], {
            "clip_name": "qwen_3_06b_base.safetensors", "type": "stable_diffusion",
        })
        self.assertEqual(flow["3"]["inputs"]["vae_name"],
                         "qwen_image_vae.safetensors")
        self.assertEqual(flow["9"]["class_type"], "SaveImage")
        ui_flow = stabilitymatrix_mcp.build_ui_workflow(flow)
        self.assertEqual(ui_flow["last_node_id"], 9)
        self.assertEqual(len(ui_flow["links"]), 9)
        self.assertEqual({node["type"] for node in ui_flow["nodes"]}, {
            "UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode",
            "EmptyLatentImage", "KSampler", "VAEDecode", "SaveImage",
        })


    def test_job_submission_keeps_editable_ui_workflow(self):
        store = stabilitymatrix_mcp.JobStore()
        generation = stabilitymatrix_mcp.validate_config({
            "model": "model.safetensors", "batches": 1,
        })
        generation["_model_kind"] = "checkpoint"
        captured = []

        def fake_api(path, method="GET", payload=None, timeout=3.0, raw=False):
            if path == "/prompt":
                captured.append(payload)
                return {"prompt_id": "prompt-1"}
            raise AssertionError(path)

        with mock.patch.object(stabilitymatrix_mcp, "wait_for_backend"), \
                mock.patch.object(stabilitymatrix_mcp, "load_profile",
                                  return_value=stabilitymatrix_mcp.DEFAULT_PROFILE), \
                mock.patch.object(stabilitymatrix_mcp, "resolve_config",
                                  return_value=generation), \
                mock.patch.object(stabilitymatrix_mcp, "_api_request", side_effect=fake_api), \
                mock.patch.object(store, "_wait_outputs"):
            store.jobs["job-1"] = {
                "job_id": "job-1", "state": "starting_backend", "created_at": 0,
                "updated_at": 0, "prompt_id": None, "images": [], "error": None,
            }
            store._run("job-1", {"positive_prompt": "cat"})

        workflow = captured[0]["extra_data"]["extra_pnginfo"]["workflow"]
        self.assertEqual(workflow["last_node_id"], 7)
        self.assertEqual(len(workflow["nodes"]), 7)


    def test_resolve_config_accepts_anima_diffusion_model(self):
        with mock.patch.object(stabilitymatrix_mcp, "list_models", return_value=[]), \
                mock.patch.object(stabilitymatrix_mcp, "list_diffusion_models",
                                  return_value=["anima_aestheticV11.safetensors"]), \
                mock.patch.object(stabilitymatrix_mcp, "list_text_encoders",
                                  return_value=["qwen_3_06b_base.safetensors"]), \
                mock.patch.object(stabilitymatrix_mcp, "list_vae_models",
                                  return_value=["qwen_image_vae.safetensors"]):
            value = stabilitymatrix_mcp.resolve_config({
                "model": "anima_aestheticV11.safetensors",
            })
        self.assertEqual(value["_model_kind"], "diffusion_model")
        self.assertEqual(value["_text_encoder"], "qwen_3_06b_base.safetensors")
        self.assertEqual(value["_vae"], "qwen_image_vae.safetensors")


    def test_models_returns_checkpoints_and_diffusion_models(self):
        with mock.patch.object(stabilitymatrix_mcp, "backend_online", return_value=True), \
                mock.patch.object(stabilitymatrix_mcp, "list_models",
                                  return_value=["checkpoint.safetensors"]), \
                mock.patch.object(stabilitymatrix_mcp, "list_diffusion_models",
                                  return_value=["unet.safetensors"]), \
                mock.patch.object(stabilitymatrix_mcp, "list_text_encoders",
                                  return_value=["clip.safetensors"]), \
                mock.patch.object(stabilitymatrix_mcp, "list_vae_models",
                                  return_value=["vae.safetensors"]):
            result = stabilitymatrix_mcp.call_tool("comfyui_models", {})
        payload = json.loads(result["content"][0]["text"])["data"]
        self.assertEqual(payload["models"], ["checkpoint.safetensors"])
        self.assertEqual(payload["checkpoints"], ["checkpoint.safetensors"])
        self.assertEqual(payload["diffusion_models"], ["unet.safetensors"])
        self.assertEqual(payload["text_encoders"], ["clip.safetensors"])
        self.assertEqual(payload["vae_models"], ["vae.safetensors"])
