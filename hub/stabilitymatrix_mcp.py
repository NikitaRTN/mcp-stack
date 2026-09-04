# -*- coding: utf-8 -*-
"""Built-in MCP adapter for Stability Matrix' local ComfyUI package.

The Stability Matrix inference screen stores UI state in its own application
database.  That database is intentionally not modified here.  Instead this
adapter keeps a small, explicit MCP profile and submits standard ComfyUI API
workflows.  Config/prompt operations therefore remain available while ComfyUI
is stopped, and generation can start the installed package on demand.
"""

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import mimetypes
import os
import random
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener, urlopen

from . import config
from . import oauth
from .logbook import LOG


SERVER_NAME = "mcp-hub-comfyui"
SERVER_VERSION = "1.7.0"
PROTOCOL_VERSION = "2025-06-18"
API_URL = "http://127.0.0.1:8188"
COMFYUI_LISTEN = "127.0.0.1"
COMFYUI_PORT = 8188
MAX_BODY = 2 * 1024 * 1024
MAX_EMBEDDED_IMAGE_BYTES = 20 * 1024 * 1024
MAX_EMBEDDED_IMAGES = 4
MAX_EMBEDDED_IMAGES_TOTAL = 40 * 1024 * 1024
MAX_EMBEDDED_RESOURCE_BYTES = 8 * 1024 * 1024
MAX_EMBEDDED_RESOURCES_TOTAL = 16 * 1024 * 1024
MAX_SEED = 0x7FFFFFFFFFFFFFFF
DEFAULT_WAIT_SECONDS = 240
IMAGE_UI_URI = "ui://stabilitymatrix/generated-image-v6.html"
IMAGE_UI_ALIASES = frozenset({
    IMAGE_UI_URI,
    "ui://stabilitymatrix/generated-image-v1.html",
    "ui://stabilitymatrix/generated-image-v2.html",
    "ui://stabilitymatrix/generated-image-v3.html",
    "ui://stabilitymatrix/generated-image-v4.html",
    "ui://stabilitymatrix/generated-image-v5.html",
})
IMAGE_OUTPUT_PATH = "/stabilitymatrix-output/"
IMAGE_RESPONSE_HEADERS = {
    "Content-Disposition": "inline",
    "Access-Control-Allow-Origin": "*",
    "Cross-Origin-Resource-Policy": "cross-origin",
    "X-Content-Type-Options": "nosniff",
}
DOWNLOAD_UI_URI = "ui://stabilitymatrix/model-download-v1.html"
DOWNLOAD_OUTPUT_PATH = "/stabilitymatrix-download/"
COMPARISON_UI_URI = "ui://stabilitymatrix/image-comparison-v7.html"
COMPARISON_UI_ALIASES = frozenset({
    COMPARISON_UI_URI,
    "ui://stabilitymatrix/image-comparison-v1.html",
    "ui://stabilitymatrix/image-comparison-v2.html",
    "ui://stabilitymatrix/image-comparison-v3.html",
    "ui://stabilitymatrix/image-comparison-v4.html",
    "ui://stabilitymatrix/image-comparison-v5.html",
    "ui://stabilitymatrix/image-comparison-v6.html",
})
MAX_MODEL_DOWNLOAD = 100 * 1024 * 1024 * 1024
MODEL_EXTENSIONS = frozenset({
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf",
})
MODEL_DESTINATIONS = {
    "checkpoints": "StableDiffusion",
    "diffusion_models": "DiffusionModels",
    "text_encoders": "TextEncoders",
    "vae": "VAE",
    "loras": "Lora",
    "controlnet": "ControlNet",
    "clip_vision": "ClipVision",
    "embeddings": "Embeddings",
    "upscale_models": "ESRGAN",
    "hypernetworks": "Hypernetwork",
    "style_models": "StyleModels",
    "audio_encoders": "AudioEncoders",
    "model_patches": "ModelPatches",
    "ipadapter": "IpAdapter",
    "gligen": "GLIGEN",
    "sams": "Sams",
    "background_removal": "BackgroundRemoval",
    "prompt_expansion": "PromptExpansion",
}
_image_key_lock = threading.Lock()

IMAGE_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root { color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif;
            --zoom: 1; --pan-x: 0px; --pan-y: 0px; }
    body { margin: 0; background: transparent; color: CanvasText; }
    .card { overflow: hidden; border: 1px solid color-mix(in srgb, CanvasText 16%, transparent);
            border-radius: 14px; background: color-mix(in srgb, Canvas 96%, transparent); }
    .toolbar { display: flex; align-items: center; justify-content: flex-end; gap: 6px;
               min-height: 34px; padding: 7px 9px; border-bottom: 1px solid
               color-mix(in srgb, CanvasText 10%, transparent); }
    .toolbar button { min-width: 34px; height: 32px; padding: 0 9px; border: 1px solid
                      color-mix(in srgb, CanvasText 18%, transparent); border-radius: 9px;
                      background: color-mix(in srgb, CanvasText 7%, transparent);
                      color: inherit; font: inherit; cursor: pointer; }
    .toolbar button:hover { background: color-mix(in srgb, CanvasText 13%, transparent); }
    .toolbar button:focus-visible { outline: 2px solid Highlight; outline-offset: 2px; }
    #zoom { min-width: 58px; font-size: 12px; }
    #close { font-size: 20px; line-height: 1; }
    .status { padding: 14px 16px; font-size: 14px; }
    .viewport { overflow: hidden; min-height: 240px; background: #111;
                position: relative; touch-action: none; }
    .viewport.is-zoomed { cursor: grab; }
    .viewport.is-dragging { cursor: grabbing; }
    img { display: block; width: 100%; height: auto; max-height: 720px; object-fit: contain;
          transform: translate(var(--pan-x), var(--pan-y)) scale(var(--zoom));
          transform-origin: 0 0; transition: transform 100ms ease-out;
          user-select: none; -webkit-user-drag: none; }
    .viewport.is-dragging img { transition: none; }
    .actions { display: flex; align-items: center; justify-content: space-between;
               gap: 12px; padding: 10px 14px; font-size: 13px; }
    .links { display: flex; align-items: center; gap: 14px; }
    a { color: LinkText; font-weight: 600; text-decoration: none; }
    [hidden] { display: none !important; }
  </style>
</head>
<body>
  <section class="card">
    <div class="toolbar">
      <button id="zoom" type="button" title="Сбросить масштаб" hidden>100%</button>
      <button id="fullscreen" type="button" title="Открыть на весь экран"
              aria-label="Открыть изображение на весь экран">⛶</button>
      <button id="close" type="button" title="Закрыть"
              aria-label="Закрыть изображение">×</button>
    </div>
    <div id="status" class="status">Waiting for Stability Matrix…</div>
    <div id="viewport" class="viewport" hidden>
      <img id="image" alt="Image generated by Stability Matrix">
    </div>
    <div id="actions" class="actions" hidden>
      <span id="details"></span>
      <span class="links">
        <a id="download" target="_blank" rel="noreferrer">Open image</a>
        <a id="comfyui" href="#" hidden>Open in ComfyUI</a>
      </span>
    </div>
  </section>
  <script>
    const statusNode = document.getElementById("status");
    const viewportNode = document.getElementById("viewport");
    const imageNode = document.getElementById("image");
    const actionsNode = document.getElementById("actions");
    const detailsNode = document.getElementById("details");
    const downloadNode = document.getElementById("download");
    const comfyuiNode = document.getElementById("comfyui");
    const zoomNode = document.getElementById("zoom");
    const fullscreenNode = document.getElementById("fullscreen");
    const closeNode = document.getElementById("close");
    let displayMode = window.openai?.displayMode || "inline";
    let zoom = 1;
    let panX = 0;
    let panY = 0;
    let dragPointerId = null;
    let lastPointerX = 0;
    let lastPointerY = 0;

    function clampAxis(value, viewportSize, contentSize) {
      if (contentSize <= viewportSize) return (viewportSize - contentSize) / 2;
      return Math.min(0, Math.max(viewportSize - contentSize, value));
    }
    function clampPan() {
      panX = clampAxis(panX, viewportNode.clientWidth, imageNode.clientWidth * zoom);
      panY = clampAxis(panY, viewportNode.clientHeight, imageNode.clientHeight * zoom);
    }
    function applyTransform() {
      document.documentElement.style.setProperty("--zoom", String(zoom));
      document.documentElement.style.setProperty("--pan-x", `${panX}px`);
      document.documentElement.style.setProperty("--pan-y", `${panY}px`);
      zoomNode.textContent = `${Math.round(zoom * 100)}%`;
      viewportNode.classList.toggle("is-zoomed", zoom > 1);
    }
    function setZoom(value, clientX = null, clientY = null) {
      const nextZoom = Math.max(1, Math.min(5, Math.round(value * 10) / 10));
      if (clientX !== null && clientY !== null && nextZoom !== zoom) {
        const rect = viewportNode.getBoundingClientRect();
        const localX = clientX - rect.left;
        const localY = clientY - rect.top;
        const focalX = (localX - panX) / zoom;
        const focalY = (localY - panY) / zoom;
        panX = localX - focalX * nextZoom;
        panY = localY - focalY * nextZoom;
      } else if (nextZoom === 1) {
        panX = 0;
        panY = 0;
      }
      zoom = nextZoom;
      clampPan();
      applyTransform();
    }
    viewportNode.addEventListener("wheel", (event) => {
      event.preventDefault();
      setZoom(zoom + (event.deltaY < 0 ? 0.1 : -0.1), event.clientX, event.clientY);
    }, { passive: false });
    viewportNode.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || zoom <= 1) return;
      event.preventDefault();
      dragPointerId = event.pointerId;
      lastPointerX = event.clientX;
      lastPointerY = event.clientY;
      viewportNode.setPointerCapture(event.pointerId);
      viewportNode.classList.add("is-dragging");
    });
    viewportNode.addEventListener("pointermove", (event) => {
      if (event.pointerId !== dragPointerId) return;
      panX += event.clientX - lastPointerX;
      panY += event.clientY - lastPointerY;
      lastPointerX = event.clientX;
      lastPointerY = event.clientY;
      clampPan();
      applyTransform();
    });
    function stopDragging(event) {
      if (event.pointerId !== dragPointerId) return;
      dragPointerId = null;
      viewportNode.classList.remove("is-dragging");
    }
    viewportNode.addEventListener("pointerup", stopDragging);
    viewportNode.addEventListener("pointercancel", stopDragging);
    imageNode.addEventListener("dragstart", (event) => event.preventDefault());
    imageNode.addEventListener("load", () => setZoom(1));
    window.addEventListener("resize", () => { clampPan(); applyTransform(); });
    zoomNode.addEventListener("click", () => setZoom(1));
    function updateCloseButton() {
      closeNode.hidden = false;
      closeNode.title = "Вернуться в чат";
      closeNode.setAttribute("aria-label", "Вернуться к изображению в чате");
    }
    function syncDisplayMode(event) {
      const nextMode = event?.detail?.globals?.displayMode;
      if (nextMode) displayMode = nextMode;
      else if (window.openai?.displayMode) displayMode = window.openai.displayMode;
      updateCloseButton();
    }
    window.addEventListener("openai:set_globals", syncDisplayMode);
    syncDisplayMode();
    fullscreenNode.addEventListener("click", async () => {
      if (window.openai && typeof window.openai.requestDisplayMode === "function") {
        displayMode = "fullscreen";
        updateCloseButton();
        await window.openai.requestDisplayMode({ mode: "fullscreen" });
      } else if (document.documentElement.requestFullscreen) {
        await document.documentElement.requestFullscreen();
      }
    });
    closeNode.addEventListener("click", async () => {
      if (window.openai && typeof window.openai.requestDisplayMode === "function") {
        await window.openai.requestDisplayMode({ mode: "inline" });
        displayMode = "inline";
        updateCloseButton();
        return;
      }
      if (document.fullscreenElement && document.exitFullscreen) {
        await document.exitFullscreen();
      }
    });

    comfyuiNode.addEventListener("click", async (event) => {
      event.preventDefault();
      const href = comfyuiNode.dataset.href;
      if (!href) return;
      if (window.openai && typeof window.openai.openExternal === "function") {
        await window.openai.openExternal({ href, redirectUrl: false });
      } else {
        window.open(href, "_blank", "noopener,noreferrer");
      }
    });

    let terminal = false;
    let hydrationTimer = null;
    function render(value) {
      if (!value || typeof value !== "object") return;
      const urls = Array.isArray(value.image_urls) ? value.image_urls : [];
      if (value.state === "completed" && urls.length) {
        statusNode.hidden = true;
        imageNode.src = urls[0];
        viewportNode.hidden = false;
        actionsNode.hidden = false;
        zoomNode.hidden = false;
        setZoom(1);
        detailsNode.textContent = urls.length > 1 ? `${urls.length} images generated` : "Generated image";
        downloadNode.href = urls[0];
        comfyuiNode.dataset.href = value.comfyui_url || "";
        comfyuiNode.hidden = !value.comfyui_url;
        terminal = true;
        if (hydrationTimer) clearInterval(hydrationTimer);
      } else {
        statusNode.hidden = false;
        statusNode.textContent = value.state === "error"
          ? (value.error?.message || "Generation failed")
          : (value.message || `Generation status: ${value.state || "unknown"}`);
        viewportNode.hidden = true;
        actionsNode.hidden = true;
        zoomNode.hidden = true;
        comfyuiNode.hidden = true;
        if (value.state === "error") {
          terminal = true;
          if (hydrationTimer) clearInterval(hydrationTimer);
        }
      }
    }

    function unwrapOutput(value) {
      if (typeof value === "string") {
        try { return unwrapOutput(JSON.parse(value)); } catch { return null; }
      }
      if (!value || typeof value !== "object") return null;
      if (Array.isArray(value)) {
        for (const item of value) {
          const found = unwrapOutput(item);
          if (found) return found;
        }
        return null;
      }
      if (typeof value.state === "string") return value;
      for (const key of [
        "structuredContent", "data", "content", "text", "result",
        "mcp_tool_result", "call_tool_result", "stabilitymatrix/result",
        "toolOutput", "toolResponseMetadata"
      ]) {
        const found = unwrapOutput(value[key]);
        if (found) return found;
      }
      return null;
    }
    function renderCompatibilityOutput(event) {
      const globals = event?.detail?.globals;
      render(unwrapOutput(globals?.toolOutput)
        || unwrapOutput(globals?.toolResponseMetadata)
        || unwrapOutput(window.openai?.toolOutput)
        || unwrapOutput(window.openai?.toolResponseMetadata));
    }
    window.addEventListener("openai:set_globals", renderCompatibilityOutput);
    window.addEventListener("message", (event) => {
      const message = event.data;
      if (message && message.method === "ui/notifications/tool-result") {
        render(unwrapOutput(message.params));
      }
    });
    renderCompatibilityOutput();
    hydrationTimer = setInterval(() => {
      if (terminal) {
        clearInterval(hydrationTimer);
        hydrationTimer = null;
        return;
      }
      renderCompatibilityOutput();
    }, 500);
    setTimeout(() => {
      if (hydrationTimer) clearInterval(hydrationTimer);
      hydrationTimer = null;
    }, 10 * 60 * 1000);
  </script>
</body>
</html>"""


DOWNLOAD_UI_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root { color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }
    body { margin: 0; background: transparent; color: CanvasText; }
    .card { padding: 16px; border: 1px solid color-mix(in srgb, CanvasText 16%, transparent);
            border-radius: 14px; background: color-mix(in srgb, Canvas 96%, transparent); }
    .top { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
    .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            font-weight: 650; }
    .state { flex: none; font-size: 12px; opacity: .75; }
    .track { height: 10px; margin: 14px 0 9px; overflow: hidden; border-radius: 999px;
             background: color-mix(in srgb, CanvasText 12%, transparent); }
    .bar { width: 0; height: 100%; border-radius: inherit; background: #10a37f;
           transition: width .25s ease; }
    .bar.indeterminate { width: 35%; animation: move 1.4s ease-in-out infinite alternate; }
    .meta { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; opacity: .8; }
    .error { margin-top: 10px; color: #e5484d; font-size: 13px; }
    @keyframes move { from { transform: translateX(-100%); } to { transform: translateX(285%); } }
  </style>
</head>
<body>
  <section class="card">
    <div class="top"><div id="name" class="name">Подготовка загрузки…</div><div id="state" class="state">queued</div></div>
    <div class="track"><div id="bar" class="bar indeterminate"></div></div>
    <div class="meta"><span id="amount">0 B</span><span id="speed"></span><span id="eta"></span></div>
    <div id="error" class="error" hidden></div>
  </section>
  <script>
    const nodes = Object.fromEntries(["name","state","bar","amount","speed","eta","error"]
      .map((id) => [id, document.getElementById(id)]));
    let timer = null;
    let progressUrl = "";

    function bytes(value) {
      if (!Number.isFinite(value) || value < 0) return "—";
      const units = ["B", "KiB", "MiB", "GiB", "TiB"];
      let index = 0;
      while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
      return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
    }
    function duration(value) {
      if (!Number.isFinite(value) || value < 0) return "";
      const seconds = Math.round(value);
      if (seconds < 60) return `ETA ${seconds}s`;
      const minutes = Math.floor(seconds / 60);
      return `ETA ${minutes}m ${seconds % 60}s`;
    }
    function render(value) {
      if (!value || typeof value !== "object") return;
      nodes.name.textContent = value.filename || "Загрузка модели";
      nodes.state.textContent = value.state || "unknown";
      const total = value.bytes_total == null ? Number.NaN : Number(value.bytes_total);
      const done = Number(value.bytes_downloaded || 0);
      const percent = value.percent == null ? Number.NaN : Number(value.percent);
      if (Number.isFinite(percent)) {
        nodes.bar.classList.remove("indeterminate");
        nodes.bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
      } else {
        nodes.bar.classList.add("indeterminate");
      }
      nodes.amount.textContent = Number.isFinite(total) && total > 0
        ? `${bytes(done)} / ${bytes(total)} (${percent.toFixed(1)}%)`
        : bytes(done);
      nodes.speed.textContent = value.speed_bps > 0 ? `${bytes(Number(value.speed_bps))}/s` : "";
      nodes.eta.textContent = duration(
        value.eta_seconds == null ? Number.NaN : Number(value.eta_seconds)
      );
      nodes.error.hidden = !value.error;
      nodes.error.textContent = value.error?.message || "";
      progressUrl = value.progress_url || progressUrl;
      if (["completed", "error"].includes(value.state)) {
        if (timer) clearTimeout(timer);
        timer = null;
      } else if (progressUrl && !timer) {
        timer = setTimeout(poll, 2000);
      }
    }
    async function poll() {
      timer = null;
      try {
        const response = await fetch(progressUrl, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json());
      } catch (error) {
        nodes.state.textContent = "reconnecting";
        nodes.error.hidden = false;
        nodes.error.textContent = `Статус временно недоступен: ${error.message}`;
        timer = setTimeout(poll, 4000);
      }
    }
    function unwrapOutput(value) {
      if (!value || typeof value !== "object") return null;
      if (typeof value.state === "string") return value;
      for (const key of ["structuredContent", "result", "mcp_tool_result", "call_tool_result"]) {
        const found = unwrapOutput(value[key]);
        if (found) return found;
      }
      return null;
    }
    function compatibilityOutput(event) {
      const globals = event?.detail?.globals;
      render(unwrapOutput(globals?.toolOutput)
        || unwrapOutput(globals?.toolResponseMetadata)
        || unwrapOutput(window.openai?.toolOutput)
        || unwrapOutput(window.openai?.toolResponseMetadata));
    }
    window.addEventListener("openai:set_globals", compatibilityOutput);
    window.addEventListener("message", (event) => {
      const message = event.data;
      if (message && message.method === "ui/notifications/tool-result") {
        render(unwrapOutput(message.params));
      }
    });
    compatibilityOutput();
    window.parent.postMessage({
      jsonrpc: "2.0", id: 1, method: "ui/initialize",
      params: { protocolVersion: "2025-06-18", appInfo: { name: "Stability Matrix Download", version: "1.0.0" }, appCapabilities: {} }
    }, "*");
  </script>
</body>
</html>"""


COMPARISON_UI_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root { color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif;
            --split: 50%; --zoom: 1; --pan-x: 0px; --pan-y: 0px; }
    body { margin: 0; background: transparent; color: CanvasText; }
    .card { overflow: hidden; border: 1px solid color-mix(in srgb, CanvasText 16%, transparent);
            border-radius: 14px; background: color-mix(in srgb, Canvas 96%, transparent); }
    .toolbar { display: flex; align-items: center; justify-content: flex-end; gap: 6px;
               min-height: 34px; padding: 7px 9px; border-bottom: 1px solid
               color-mix(in srgb, CanvasText 10%, transparent); }
    .toolbar button { min-width: 34px; height: 32px; padding: 0 9px; border: 1px solid
                      color-mix(in srgb, CanvasText 18%, transparent); border-radius: 9px;
                      background: color-mix(in srgb, CanvasText 7%, transparent);
                      color: inherit; font: inherit; cursor: pointer; }
    .toolbar button:hover { background: color-mix(in srgb, CanvasText 13%, transparent); }
    .toolbar button:focus-visible { outline: 2px solid Highlight; outline-offset: 2px; }
    #zoom { min-width: 58px; font-size: 12px; }
    #close { font-size: 20px; line-height: 1; }
    .status { padding: 15px 16px; font-size: 14px; }
    .compare { position: relative; overflow: hidden; min-height: 240px; background: #111;
               touch-action: none; }
    .compare.is-zoomed { cursor: grab; }
    .compare.is-dragging { cursor: grabbing; }
    .compare img { display: block; width: 100%; height: auto; max-height: 760px; object-fit: contain;
                   transform: translate(var(--pan-x), var(--pan-y)) scale(var(--zoom));
                   transform-origin: 0 0; transition: transform 100ms ease-out;
                   user-select: none; -webkit-user-drag: none; }
    .compare.is-dragging img { transition: none; }
    .top { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
           clip-path: inset(0 calc(100% - var(--split)) 0 0); }
    .divider { position: absolute; top: 0; bottom: 0; left: var(--split); width: 2px;
               transform: translateX(-1px); background: #fff; box-shadow: 0 0 0 1px #0008; pointer-events: none; }
    .handle { position: absolute; top: 50%; left: var(--split); width: 34px; height: 34px;
              transform: translate(-50%, -50%); border: 2px solid #fff; border-radius: 50%;
              background: #111b; color: #fff; display: grid; place-items: center; pointer-events: none; }
    .label { position: absolute; top: 12px; padding: 5px 9px; border-radius: 999px;
             background: #000a; color: #fff; font-size: 12px; font-weight: 650; }
    .label-a { left: 12px; } .label-b { right: 12px; }
    input[type=range] { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0;
                        opacity: 0; cursor: ew-resize; }
    .compare.is-zoomed input[type=range] { pointer-events: none; }
    .footer { display: flex; justify-content: space-between; gap: 12px; padding: 10px 14px;
              font-size: 12px; opacity: .8; }
    [hidden] { display: none !important; }
  </style>
</head>
<body>
  <section class="card">
    <div class="toolbar">
      <button id="zoom" type="button" title="Сбросить масштаб" hidden>100%</button>
      <button id="fullscreen" type="button" title="Открыть на весь экран"
              aria-label="Открыть сравнение на весь экран">⛶</button>
      <button id="close" type="button" title="Закрыть"
              aria-label="Закрыть сравнение">×</button>
    </div>
    <div id="status" class="status">Stability Matrix готовит сравнение…</div>
    <div id="compare" class="compare" hidden>
      <img id="right" alt="Comparison image B">
      <img id="left" class="top" alt="Comparison image A">
      <div class="divider"></div><div class="handle">↔</div>
      <span id="labelA" class="label label-a">A</span>
      <span id="labelB" class="label label-b">B</span>
      <input id="slider" type="range" min="0" max="100" value="50"
             aria-label="Сравнить изображения A и B">
    </div>
    <div id="footer" class="footer" hidden><span>Слева: A</span><span>Справа: B</span></div>
  </section>
  <script>
    const root = document.documentElement;
    const statusNode = document.getElementById("status");
    const compareNode = document.getElementById("compare");
    const footerNode = document.getElementById("footer");
    const leftNode = document.getElementById("left");
    const rightNode = document.getElementById("right");
    const labelANode = document.getElementById("labelA");
    const labelBNode = document.getElementById("labelB");
    const zoomNode = document.getElementById("zoom");
    const fullscreenNode = document.getElementById("fullscreen");
    const closeNode = document.getElementById("close");
    let displayMode = window.openai?.displayMode || "inline";
    let zoom = 1;
    let panX = 0;
    let panY = 0;
    let dragPointerId = null;
    let lastPointerX = 0;
    let lastPointerY = 0;
    function clampAxis(value, viewportSize, contentSize) {
      if (contentSize <= viewportSize) return (viewportSize - contentSize) / 2;
      return Math.min(0, Math.max(viewportSize - contentSize, value));
    }
    function clampPan() {
      panX = clampAxis(panX, compareNode.clientWidth, rightNode.clientWidth * zoom);
      panY = clampAxis(panY, compareNode.clientHeight, rightNode.clientHeight * zoom);
    }
    function applyTransform() {
      root.style.setProperty("--zoom", String(zoom));
      root.style.setProperty("--pan-x", `${panX}px`);
      root.style.setProperty("--pan-y", `${panY}px`);
      zoomNode.textContent = `${Math.round(zoom * 100)}%`;
      compareNode.classList.toggle("is-zoomed", zoom > 1);
    }
    function setZoom(value, clientX = null, clientY = null) {
      const nextZoom = Math.max(1, Math.min(5, Math.round(value * 10) / 10));
      if (clientX !== null && clientY !== null && nextZoom !== zoom) {
        const rect = compareNode.getBoundingClientRect();
        const localX = clientX - rect.left;
        const localY = clientY - rect.top;
        const focalX = (localX - panX) / zoom;
        const focalY = (localY - panY) / zoom;
        panX = localX - focalX * nextZoom;
        panY = localY - focalY * nextZoom;
      } else if (nextZoom === 1) {
        panX = 0;
        panY = 0;
      }
      zoom = nextZoom;
      clampPan();
      applyTransform();
    }
    compareNode.addEventListener("wheel", (event) => {
      event.preventDefault();
      setZoom(zoom + (event.deltaY < 0 ? 0.1 : -0.1), event.clientX, event.clientY);
    }, { passive: false });
    compareNode.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || zoom <= 1) return;
      event.preventDefault();
      dragPointerId = event.pointerId;
      lastPointerX = event.clientX;
      lastPointerY = event.clientY;
      compareNode.setPointerCapture(event.pointerId);
      compareNode.classList.add("is-dragging");
    });
    compareNode.addEventListener("pointermove", (event) => {
      if (event.pointerId !== dragPointerId) return;
      panX += event.clientX - lastPointerX;
      panY += event.clientY - lastPointerY;
      lastPointerX = event.clientX;
      lastPointerY = event.clientY;
      clampPan();
      applyTransform();
    });
    function stopDragging(event) {
      if (event.pointerId !== dragPointerId) return;
      dragPointerId = null;
      compareNode.classList.remove("is-dragging");
    }
    compareNode.addEventListener("pointerup", stopDragging);
    compareNode.addEventListener("pointercancel", stopDragging);
    leftNode.addEventListener("dragstart", (event) => event.preventDefault());
    rightNode.addEventListener("dragstart", (event) => event.preventDefault());
    rightNode.addEventListener("load", () => setZoom(1));
    window.addEventListener("resize", () => { clampPan(); applyTransform(); });
    zoomNode.addEventListener("click", () => setZoom(1));
    function updateCloseButton() {
      closeNode.hidden = false;
      closeNode.title = "Вернуться в чат";
      closeNode.setAttribute("aria-label", "Вернуться к сравнению в чате");
    }
    function syncDisplayMode(event) {
      const nextMode = event?.detail?.globals?.displayMode;
      if (nextMode) displayMode = nextMode;
      else if (window.openai?.displayMode) displayMode = window.openai.displayMode;
      updateCloseButton();
    }
    window.addEventListener("openai:set_globals", syncDisplayMode);
    syncDisplayMode();
    fullscreenNode.addEventListener("click", async () => {
      if (window.openai && typeof window.openai.requestDisplayMode === "function") {
        displayMode = "fullscreen";
        updateCloseButton();
        await window.openai.requestDisplayMode({ mode: "fullscreen" });
      } else if (document.documentElement.requestFullscreen) {
        await document.documentElement.requestFullscreen();
      }
    });
    closeNode.addEventListener("click", async () => {
      if (window.openai && typeof window.openai.requestDisplayMode === "function") {
        await window.openai.requestDisplayMode({ mode: "inline" });
        displayMode = "inline";
        updateCloseButton();
        return;
      }
      if (document.fullscreenElement && document.exitFullscreen) {
        await document.exitFullscreen();
      }
    });
    document.getElementById("slider").addEventListener("input", (event) => {
      root.style.setProperty("--split", `${event.target.value}%`);
    });
    let terminal = false;
    let hydrationTimer = null;
    function render(value) {
      if (!value || typeof value !== "object") return;
      const left = value.left || {};
      const right = value.right || {};
      if (value.state === "completed" && left.image_url && right.image_url) {
        leftNode.src = left.image_url;
        rightNode.src = right.image_url;
        labelANode.textContent = left.label || "A";
        labelBNode.textContent = right.label || "B";
        footerNode.children[0].textContent = `A: ${left.label || "A"}`;
        footerNode.children[1].textContent = `B: ${right.label || "B"}`;
        statusNode.hidden = true;
        compareNode.hidden = false;
        footerNode.hidden = false;
        zoomNode.hidden = false;
        setZoom(1);
        terminal = true;
        if (hydrationTimer) clearInterval(hydrationTimer);
      } else {
        statusNode.hidden = false;
        statusNode.textContent = value.message || `Comparison status: ${value.state || "unknown"}`;
        compareNode.hidden = true;
        footerNode.hidden = true;
        zoomNode.hidden = true;
        if (value.state === "error") {
          terminal = true;
          if (hydrationTimer) clearInterval(hydrationTimer);
        }
      }
    }
    function unwrapOutput(value) {
      if (typeof value === "string") {
        try { return unwrapOutput(JSON.parse(value)); } catch { return null; }
      }
      if (!value || typeof value !== "object") return null;
      if (Array.isArray(value)) {
        for (const item of value) {
          const found = unwrapOutput(item);
          if (found) return found;
        }
        return null;
      }
      if (typeof value.state === "string") return value;
      for (const key of [
        "structuredContent", "data", "content", "text", "result",
        "mcp_tool_result", "call_tool_result", "stabilitymatrix/result",
        "toolOutput", "toolResponseMetadata"
      ]) {
        const found = unwrapOutput(value[key]);
        if (found) return found;
      }
      return null;
    }
    function compatibilityOutput(event) {
      const globals = event?.detail?.globals;
      render(unwrapOutput(globals?.toolOutput)
        || unwrapOutput(globals?.toolResponseMetadata)
        || unwrapOutput(window.openai?.toolOutput)
        || unwrapOutput(window.openai?.toolResponseMetadata));
    }
    window.addEventListener("openai:set_globals", compatibilityOutput);
    window.addEventListener("message", (event) => {
      const message = event.data;
      if (message && message.method === "ui/notifications/tool-result") {
        render(unwrapOutput(message.params));
      }
    });
    compatibilityOutput();
    hydrationTimer = setInterval(() => {
      if (terminal) {
        clearInterval(hydrationTimer);
        hydrationTimer = null;
        return;
      }
      compatibilityOutput();
    }, 500);
    setTimeout(() => {
      if (hydrationTimer) clearInterval(hydrationTimer);
      hydrationTimer = null;
    }, 10 * 60 * 1000);
  </script>
</body>
</html>"""


def _image_signing_key():
    path = config.DATA / "stabilitymatrix-output.key"
    with _image_key_lock:
        try:
            key = base64.urlsafe_b64decode(path.read_text(encoding="ascii").strip())
            if len(key) == 32:
                return key
        except (OSError, ValueError, TypeError):
            pass
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="ascii")
        os.replace(str(tmp), str(path))
        return key


def _b64url(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _image_token(image):
    payload = json.dumps({
        "filename": image["filename"],
        "subfolder": image.get("subfolder") or "",
        "type": image.get("type") or "output",
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64url(payload)
    signature = _b64url(hmac.new(_image_signing_key(), encoded.encode("ascii"),
                                  hashlib.sha256).digest())
    return encoded + "." + signature


def _image_from_token(token):
    try:
        encoded, signature = str(token or "").split(".", 1)
        expected = _b64url(hmac.new(_image_signing_key(), encoded.encode("ascii"),
                                     hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        image = json.loads(_b64url_decode(encoded).decode("utf-8"))
        filename = str(image.get("filename") or "")
        subfolder = str(image.get("subfolder") or "")
        image_type = str(image.get("type") or "")
        if (not filename or len(filename) > 240 or "\x00" in filename or
                len(subfolder) > 240 or "\x00" in subfolder or
                image_type not in ("output", "temp", "input")):
            raise ValueError("payload")
        return {"filename": filename, "subfolder": subfolder, "type": image_type}
    except (AttributeError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        raise ToolError("image_not_found", "Изображение не найдено")


def _download_token(job_id):
    payload = json.dumps({
        "job_id": str(job_id),
        "expires": int(time.time()) + 24 * 3600,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64url(payload)
    signature = _b64url(hmac.new(_image_signing_key(), encoded.encode("ascii"),
                                  hashlib.sha256).digest())
    return encoded + "." + signature


def _download_job_from_token(token):
    try:
        encoded, signature = str(token or "").split(".", 1)
        expected = _b64url(hmac.new(_image_signing_key(), encoded.encode("ascii"),
                                     hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        value = json.loads(_b64url_decode(encoded).decode("utf-8"))
        job_id = str(value.get("job_id") or "")
        expires = int(value.get("expires") or 0)
        if len(job_id) != 32 or expires < time.time():
            raise ValueError("payload")
        return job_id
    except (AttributeError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        raise ToolError("download_not_found", "Задача загрузки не найдена")

DEFAULT_PROFILE = {
    "config": {
        "model": "",
        "sampler": "euler_ancestral",
        "scheduler": "normal",
        "steps": 20,
        "cfg_scale": 5.0,
        "width": 1024,
        "height": 1024,
        "seed": -1,
        "batch_size": 1,
        "batches": 1,
    },
    "prompt": {"positive": "", "negative": ""},
}

SAMPLERS = {
    "euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp",
    "heun", "heunpp2", "dpm_2", "dpm_2_ancestral", "lms", "dpm_fast",
    "dpm_adaptive", "dpmpp_2s_ancestral", "dpmpp_2s_ancestral_cfg_pp",
    "dpmpp_sde", "dpmpp_sde_gpu", "dpmpp_2m", "dpmpp_2m_cfg_pp",
    "dpmpp_2m_sde", "dpmpp_2m_sde_gpu", "dpmpp_3m_sde",
    "dpmpp_3m_sde_gpu", "ddpm", "lcm", "ipndm", "ipndm_v",
    "deis", "res_multistep", "res_multistep_cfg_pp", "gradient_estimation",
    "gradient_estimation_cfg_pp", "er_sde", "seeds_2", "seeds_3",
    "sa_solver", "sa_solver_pece", "ddim", "uni_pc", "uni_pc_bh2",
}
SCHEDULERS = {
    "normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform",
    "beta", "linear_quadratic", "kl_optimal",
}


class ToolError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details

    def payload(self):
        value = {"success": False, "error": {"code": self.code, "message": str(self)}}
        if self.details is not None:
            value["error"]["details"] = self.details
        return value


def _profile_path():
    return config.DATA / "stabilitymatrix.json"


_profile_lock = threading.RLock()


def load_profile():
    with _profile_lock:
        value = deepcopy(DEFAULT_PROFILE)
        path = _profile_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raw = {}
        if isinstance(raw.get("config"), dict):
            value["config"].update(raw["config"])
        if isinstance(raw.get("prompt"), dict):
            value["prompt"].update(raw["prompt"])
        return value


def save_profile(value):
    with _profile_lock:
        path = _profile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
        return value


def _number(name, value, minimum, maximum, integer=False):
    if isinstance(value, bool):
        raise ToolError("invalid_config", "%s: требуется число" % name)
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise ToolError("invalid_config", "%s: требуется число" % name)
    if parsed < minimum or parsed > maximum:
        raise ToolError("invalid_config", "%s: допустимо от %s до %s" %
                        (name, minimum, maximum))
    return parsed


def validate_config(patch, base=None):
    if not isinstance(patch, dict):
        raise ToolError("invalid_config", "config должен быть объектом")
    unknown = sorted(set(patch) - set(DEFAULT_PROFILE["config"]))
    if unknown:
        raise ToolError("invalid_config", "Неизвестные поля config", unknown)
    result = dict(base or DEFAULT_PROFILE["config"])
    for key, value in patch.items():
        if key == "model":
            value = str(value or "").strip()
            if len(value) > 240 or "\x00" in value:
                raise ToolError("invalid_config", "model имеет некорректное значение")
            result[key] = value
        elif key == "sampler":
            value = str(value or "").strip()
            if value not in SAMPLERS:
                raise ToolError("invalid_config", "Неизвестный sampler", sorted(SAMPLERS))
            result[key] = value
        elif key == "scheduler":
            value = str(value or "").strip()
            if value not in SCHEDULERS:
                raise ToolError("invalid_config", "Неизвестный scheduler", sorted(SCHEDULERS))
            result[key] = value
        elif key == "steps":
            result[key] = _number(key, value, 1, 150, integer=True)
        elif key == "cfg_scale":
            result[key] = _number(key, value, 0, 30)
        elif key in ("width", "height"):
            size = _number(key, value, 64, 4096, integer=True)
            if size % 8:
                raise ToolError("invalid_config", "%s должно быть кратно 8" % key)
            result[key] = size
        elif key == "seed":
            result[key] = _number(key, value, -1, MAX_SEED, integer=True)
        elif key in ("batch_size", "batches"):
            result[key] = _number(key, value, 1, 16, integer=True)
    return result


def set_config(patch):
    value = load_profile()
    value["config"] = validate_config(patch, value["config"])
    save_profile(value)
    return deepcopy(value["config"])


def set_prompt(arguments):
    if not isinstance(arguments, dict):
        raise ToolError("invalid_prompt", "prompt должен быть объектом")
    unknown = sorted(set(arguments) - {"positive", "negative"})
    if unknown:
        raise ToolError("invalid_prompt", "Неизвестные поля prompt", unknown)
    value = load_profile()
    for key in ("positive", "negative"):
        if key in arguments:
            text = str(arguments[key] or "")
            if len(text) > 100000:
                raise ToolError("invalid_prompt", "%s prompt слишком длинный" % key)
            value["prompt"][key] = text
    save_profile(value)
    return deepcopy(value["prompt"])


def _api_request(path, method="GET", payload=None, timeout=3.0, raw=False):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    api_url = comfyui_api_url()
    request = Request(api_url + path, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            if raw:
                return body, response.headers.get("Content-Type")
            return json.loads(body.decode("utf-8")) if body else {}
    except HTTPError as exc:
        detail = exc.read(8192).decode("utf-8", errors="replace")
        raise ToolError("backend_http_error", "ComfyUI вернул HTTP %d" % exc.code, detail)
    except (URLError, OSError, TimeoutError) as exc:
        raise ToolError("backend_offline", "ComfyUI API недоступен на %s" % api_url,
                        str(exc))
    except ValueError as exc:
        raise ToolError("backend_invalid_response", "ComfyUI вернул невалидный JSON", str(exc))


def comfyui_api_url():
    """Адрес API задаёт владелец в конфиге; MCP-клиент не может его менять."""
    value = str(config.load().get("comfyuiApiUrl") or API_URL).strip().rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ToolError("invalid_backend_url", "Некорректный comfyuiApiUrl")
    try:
        parsed.port
    except ValueError:
        raise ToolError("invalid_backend_url", "Некорректный порт comfyuiApiUrl")
    return value


def comfyui_web_url():
    """Return the browser URL advertised to MCP clients."""
    value = str(config.load().get("stabilityMatrixWebUrl") or
                "http://localhost:%d" % COMFYUI_PORT).strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
        return "http://localhost:%d" % COMFYUI_PORT
    return value


def comfyui_web_origin():
    parsed = urlsplit(comfyui_web_url())
    return "%s://%s" % (parsed.scheme, parsed.netloc)


def backend_online(timeout=0.6):
    try:
        _api_request("/system_stats", timeout=timeout)
        return True
    except ToolError:
        return False


def _package_candidates():
    custom = config.load().get("comfyuiPath") or os.environ.get("COMFYUI_PATH") or os.environ.get("STABILITY_MATRIX_COMFYUI")
    if custom:
        yield Path(custom)
    yield Path.home() / "ComfyUI"
    yield config.ROOT / "ComfyUI"


def find_package():
    seen = set()
    for candidate in _package_candidates():
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        python = candidate / "venv" / "Scripts" / "python.exe"
        if not python.exists():
            python = candidate / "venv" / "bin" / "python"
        if (candidate / "main.py").is_file() and python.is_file():
            return candidate, python
    return None, None


def model_destination(model_type):
    model_type = str(model_type or "").strip().lower()
    folder = MODEL_DESTINATIONS.get(model_type)
    if folder is None:
        raise ToolError("invalid_model_type", "Неизвестный тип модели",
                        sorted(MODEL_DESTINATIONS))
    package, _python = find_package()
    if package is None:
        raise ToolError("package_not_found",
                        "Не найден пакет ComfyUI, установленный через Stability Matrix")
    custom = os.environ.get("STABILITY_MATRIX_MODELS")
    if custom:
        root = Path(custom)
    elif package.parent.name.lower() == "packages":
        root = package.parent.parent / "Models"
    else:
        # Custom portable ComfyUI installations normally keep models locally.
        return package / "models" / model_type
    return root / folder


def _safe_model_filename(filename, source_url):
    value = str(filename or "").strip()
    if not value:
        value = unquote(urlsplit(source_url).path.rsplit("/", 1)[-1]).strip()
    if (not value or len(value) > 240 or "\x00" in value or "/" in value or
            "\\" in value or value in (".", "..") or value.endswith((" ", "."))):
        raise ToolError("invalid_filename",
                        "Укажите безопасное имя файла модели с расширением")
    if Path(value).suffix.lower() not in MODEL_EXTENSIONS:
        raise ToolError("invalid_model_extension",
                        "Неподдерживаемое расширение модели", sorted(MODEL_EXTENSIONS))
    if Path(value).stem.upper() in {
            "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
            "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
            "LPT6", "LPT7", "LPT8", "LPT9"}:
        raise ToolError("invalid_filename", "Зарезервированное имя файла Windows")
    return value


def _validate_download_url(value, return_addresses=False):
    url = str(value or "").strip()
    if not url or len(url) > 4096:
        raise ToolError("invalid_download_url", "URL модели пуст или слишком длинный")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None:
        raise ToolError("invalid_download_url",
                        "Разрешены только публичные HTTPS URL без встроенной авторизации")
    try:
        port = parsed.port or 443
    except ValueError:
        raise ToolError("invalid_download_url", "Некорректный порт в URL")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise ToolError("download_dns_failed", "Не удалось разрешить адрес модели", str(exc))
    checked = set()
    for address in addresses:
        ip_text = address[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            raise ToolError("invalid_download_url", "DNS вернул некорректный IP")
        checked.add(str(ip))
        if not ip.is_global:
            raise ToolError("download_private_address",
                            "Загрузка из локальных, приватных и служебных сетей запрещена")
    if not checked:
        raise ToolError("download_dns_failed", "DNS не вернул адресов")
    return (url, sorted(checked)) if return_addresses else url


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


class _PinnedHTTPSHandler(HTTPSHandler):
    """Соединяемся с проверенным IP, сохраняя hostname для TLS и Host."""
    def __init__(self, addresses):
        super().__init__()
        self.addresses = addresses

    def https_open(self, request):
        def connection(host, **kwargs):
            conn = http.client.HTTPSConnection(host, **kwargs)

            def connect(address, timeout, source_address=None):
                last_error = None
                for ip in self.addresses:
                    try:
                        return socket.create_connection((ip, address[1]), timeout, source_address)
                    except OSError as exc:
                        last_error = exc
                raise last_error

            conn._create_connection = connect
            return conn
        return self.do_open(connection, request)


def _open_download(source_url, max_redirects=5):
    current = source_url
    for redirect_count in range(max_redirects + 1):
        _url, addresses = _validate_download_url(current, return_addresses=True)
        opener = build_opener(ProxyHandler({}), _NoRedirectHandler(), _PinnedHTTPSHandler(addresses))
        request = Request(current, headers={
            "Accept": "application/octet-stream,*/*;q=0.8",
            "User-Agent": "%s/%s" % (SERVER_NAME, SERVER_VERSION),
        })
        try:
            return opener.open(request, timeout=30.0), current
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                if location and redirect_count < max_redirects:
                    current = urljoin(current, location)
                    continue
            detail = exc.read(8192).decode("utf-8", errors="replace")
            raise ToolError("download_http_error",
                            "Сервер модели вернул HTTP %d" % exc.code, detail)
        except (URLError, OSError, TimeoutError) as exc:
            raise ToolError("download_connection_failed",
                            "Не удалось подключиться к серверу модели", str(exc))
    raise ToolError("download_redirect_limit", "Слишком много перенаправлений")


_backend_lock = threading.Lock()
_backend_process = None


def start_backend():
    """Start the installed package once. Readiness is awaited by the job worker."""
    global _backend_process
    if backend_online():
        return {"state": "online", "api": comfyui_api_url(), "started": False}
    with _backend_lock:
        if backend_online():
            return {"state": "online", "api": comfyui_api_url(), "started": False}
        if _backend_process is not None and _backend_process.poll() is None:
            return {"state": "starting", "api": comfyui_api_url(), "started": False,
                    "pid": _backend_process.pid}
        cfg = config.load()
        if not cfg.get("comfyuiAutoStart", True):
            raise ToolError("backend_offline", "ComfyUI выключен, автоматический запуск отключён")
        if urlsplit(comfyui_api_url()).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ToolError("backend_offline", "Удалённый ComfyUI нужно запустить на его хосте")
        package, python = find_package()
        if package is None:
            raise ToolError(
                "package_not_found",
                "Не найден пакет ComfyUI, установленный через Stability Matrix",
                "Задайте STABILITY_MATRIX_COMFYUI или установите ComfyUI в Stability Matrix",
            )
        log_path = config.LOGS / "stabilitymatrix-comfyui.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(str(log_path), "ab", buffering=0)
        command = [str(python), str(package / "main.py"), "--listen",
                   str(config.load().get("comfyuiListen") or COMFYUI_LISTEN),
                   "--port", str(urlsplit(comfyui_api_url()).port or 8188),
                   "--disable-auto-launch"]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            _backend_process = subprocess.Popen(
                command, cwd=str(package), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, creationflags=creationflags,
            )
        except OSError as exc:
            log.close()
            raise ToolError("backend_start_failed", "Не удалось запустить ComfyUI", str(exc))
        log.close()
        return {"state": "starting", "api": comfyui_api_url(), "started": True,
                "pid": _backend_process.pid}


def wait_for_backend(timeout=120.0):
    started = start_backend()
    if started["state"] == "online":
        return started
    deadline = time.time() + timeout
    while time.time() < deadline:
        if backend_online(timeout=1.0):
            started["state"] = "online"
            return started
        with _backend_lock:
            process = _backend_process
            if process is not None and process.poll() is not None:
                raise ToolError("backend_start_failed", "ComfyUI завершился при запуске",
                                {"exitCode": process.returncode,
                                 "log": str(config.LOGS / "stabilitymatrix-comfyui.log")})
        time.sleep(1.0)
    raise ToolError("backend_start_timeout", "ComfyUI не запустился за %d секунд" % timeout,
                    {"log": str(config.LOGS / "stabilitymatrix-comfyui.log")})


def list_model_folder(folder):
    models = _api_request("/models/%s" % folder, timeout=5.0)
    if not isinstance(models, list):
        raise ToolError("backend_invalid_response",
                        "ComfyUI вернул неверный список моделей: %s" % folder)
    return [str(item) for item in models]


def list_models():
    """Checkpoint models accepted by the current CheckpointLoaderSimple workflow."""
    return list_model_folder("checkpoints")


def list_diffusion_models():
    """Standalone UNet/diffusion models exposed by the installed ComfyUI package."""
    return list_model_folder("diffusion_models")


def list_text_encoders():
    """Standalone text encoders accepted by ComfyUI loader nodes."""
    return list_model_folder("text_encoders")


def list_vae_models():
    """Standalone VAE models accepted by ComfyUI loader nodes."""
    return list_model_folder("vae")


def resolve_config(overrides=None):
    overrides = overrides or {}
    profile = load_profile()
    current = validate_config(overrides, profile["config"])
    checkpoints = list_models()
    diffusion_models = list_diffusion_models()
    if not current["model"]:
        available = checkpoints or diffusion_models
        if not available:
            raise ToolError("model_not_found", "В Stability Matrix не установлены модели")
        current["model"] = available[0]
    if current["model"] in checkpoints:
        current["_model_kind"] = "checkpoint"
        return current
    if current["model"] not in diffusion_models:
        raise ToolError("model_not_found", "Модель не найдена в ComfyUI", {
            "requested": current["model"],
            "checkpoints": checkpoints,
            "diffusion_models": diffusion_models,
        })

    # Anima split models use the native ComfyUI workflow published by Comfy Org:
    # UNETLoader + Qwen 3 0.6B text encoder + Qwen Image VAE.
    if "anima" not in current["model"].lower():
        raise ToolError("diffusion_model_unsupported",
                        "Для этой standalone diffusion model не задан совместимый workflow", {
                            "requested": current["model"],
                            "supported_family": "Anima",
                        })
    text_encoder = "qwen_3_06b_base.safetensors"
    vae = "qwen_image_vae.safetensors"
    missing = []
    if text_encoder not in list_text_encoders():
        missing.append("models/text_encoders/%s" % text_encoder)
    if vae not in list_vae_models():
        missing.append("models/vae/%s" % vae)
    if missing:
        raise ToolError("model_components_missing",
                        "Для Anima не установлены обязательные компоненты", {"missing": missing})
    current.update({
        "_model_kind": "diffusion_model",
        "_text_encoder": text_encoder,
        "_clip_type": "stable_diffusion",
        "_vae": vae,
    })
    return current


def build_workflow(generation_config, positive, negative, seed, filename_prefix):
    if generation_config.get("_model_kind") == "diffusion_model":
        return {
            "1": {"class_type": "UNETLoader",
                  "inputs": {"unet_name": generation_config["model"],
                             "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": generation_config["_text_encoder"],
                             "type": generation_config["_clip_type"]}},
            "3": {"class_type": "VAELoader",
                  "inputs": {"vae_name": generation_config["_vae"]}},
            "4": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": positive, "clip": ["2", 0]}},
            "5": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": negative, "clip": ["2", 0]}},
            "6": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": generation_config["width"],
                             "height": generation_config["height"],
                             "batch_size": generation_config["batch_size"]}},
            "7": {"class_type": "KSampler",
                  "inputs": {"seed": seed, "steps": generation_config["steps"],
                             "cfg": generation_config["cfg_scale"],
                             "sampler_name": generation_config["sampler"],
                             "scheduler": generation_config["scheduler"], "denoise": 1.0,
                             "model": ["1", 0], "positive": ["4", 0],
                             "negative": ["5", 0], "latent_image": ["6", 0]}},
            "8": {"class_type": "VAEDecode",
                  "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
            "9": {"class_type": "SaveImage",
                  "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]}},
        }
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": generation_config["model"]}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": generation_config["width"],
                         "height": generation_config["height"],
                         "batch_size": generation_config["batch_size"]}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": generation_config["steps"],
                         "cfg": generation_config["cfg_scale"],
                         "sampler_name": generation_config["sampler"],
                         "scheduler": generation_config["scheduler"], "denoise": 1.0,
                         "model": ["1", 0], "positive": ["2", 0],
                         "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": filename_prefix, "images": ["6", 0]}},
    }


UI_NODE_SPECS = {
    "CheckpointLoaderSimple": {
        "widgets": ("ckpt_name",),
        "inputs": (),
        "outputs": (("MODEL", "MODEL"), ("CLIP", "CLIP"), ("VAE", "VAE")),
    },
    "UNETLoader": {
        "widgets": ("unet_name", "weight_dtype"),
        "inputs": (),
        "outputs": (("MODEL", "MODEL"),),
    },
    "CLIPLoader": {
        "widgets": ("clip_name", "type"),
        "inputs": (),
        "outputs": (("CLIP", "CLIP"),),
    },
    "VAELoader": {
        "widgets": ("vae_name",),
        "inputs": (),
        "outputs": (("VAE", "VAE"),),
    },
    "CLIPTextEncode": {
        "widgets": ("text",),
        "inputs": (("clip", "CLIP"),),
        "outputs": (("CONDITIONING", "CONDITIONING"),),
    },
    "EmptyLatentImage": {
        "widgets": ("width", "height", "batch_size"),
        "inputs": (),
        "outputs": (("LATENT", "LATENT"),),
    },
    "KSampler": {
        "widgets": ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"),
        "inputs": (("model", "MODEL"), ("positive", "CONDITIONING"),
                   ("negative", "CONDITIONING"), ("latent_image", "LATENT")),
        "outputs": (("LATENT", "LATENT"),),
    },
    "VAEDecode": {
        "widgets": (),
        "inputs": (("samples", "LATENT"), ("vae", "VAE")),
        "outputs": (("IMAGE", "IMAGE"),),
    },
    "SaveImage": {
        "widgets": ("filename_prefix",),
        "inputs": (("images", "IMAGE"),),
        "outputs": (),
    },
}


UI_NODE_POSITIONS = {
    "CheckpointLoaderSimple": (40, 40),
    "UNETLoader": (40, 40),
    "CLIPLoader": (40, 190),
    "VAELoader": (40, 340),
    "CLIPTextEncode": (390, 40),
    "EmptyLatentImage": (390, 490),
    "KSampler": (760, 190),
    "VAEDecode": (1110, 230),
    "SaveImage": (1430, 230),
}


def build_ui_workflow(api_workflow):
    """Convert the supported API graph to an editable ComfyUI frontend workflow."""
    nodes = []
    nodes_by_id = {}
    type_counts = {}
    numeric_ids = [int(node_id) for node_id in api_workflow]

    for order, node_id in enumerate(sorted(api_workflow, key=lambda value: int(value))):
        api_node = api_workflow[node_id]
        node_type = api_node["class_type"]
        spec = UI_NODE_SPECS[node_type]
        occurrence = type_counts.get(node_type, 0)
        type_counts[node_type] = occurrence + 1
        base_x, base_y = UI_NODE_POSITIONS[node_type]
        inputs = [{"name": name, "type": value_type, "link": None}
                  for name, value_type in spec["inputs"]]
        outputs = [{"name": name, "type": value_type, "links": []}
                   for name, value_type in spec["outputs"]]
        widget_values = [api_node["inputs"][name] for name in spec["widgets"]]
        if node_type == "KSampler":
            widget_values.insert(1, "fixed")
        node = {
            "id": int(node_id),
            "type": node_type,
            "pos": [base_x, base_y + occurrence * 260],
            "size": [315, max(82, 58 + len(widget_values) * 28)],
            "flags": {},
            "order": order,
            "mode": 0,
            "inputs": inputs,
            "outputs": outputs,
            "properties": {"Node name for S&R": node_type},
            "widgets_values": widget_values,
        }
        nodes.append(node)
        nodes_by_id[node_id] = node

    links = []
    link_id = 0
    for target_id in sorted(api_workflow, key=lambda value: int(value)):
        api_node = api_workflow[target_id]
        spec = UI_NODE_SPECS[api_node["class_type"]]
        target_slots = {name: index for index, (name, _value_type) in enumerate(spec["inputs"])}
        for input_name, value in api_node["inputs"].items():
            if not (isinstance(value, list) and len(value) == 2 and input_name in target_slots):
                continue
            origin_id, origin_slot = str(value[0]), int(value[1])
            target_slot = target_slots[input_name]
            value_type = nodes_by_id[target_id]["inputs"][target_slot]["type"]
            link_id += 1
            links.append([link_id, int(origin_id), origin_slot,
                          int(target_id), target_slot, value_type])
            nodes_by_id[target_id]["inputs"][target_slot]["link"] = link_id
            nodes_by_id[origin_id]["outputs"][origin_slot]["links"].append(link_id)

    for node in nodes:
        for output in node["outputs"]:
            if not output["links"]:
                output["links"] = None

    return {
        "id": str(uuid.uuid4()),
        "revision": 0,
        "last_node_id": max(numeric_ids),
        "last_link_id": link_id,
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {"ds": {"scale": 0.8, "offset": [40, 40]}},
        "version": 0.4,
    }


def _outputs_from_history(entry):
    images = []
    for node in (entry.get("outputs") or {}).values():
        for image in node.get("images") or []:
            if isinstance(image, dict) and image.get("filename"):
                images.append({
                    "filename": image["filename"],
                    "subfolder": image.get("subfolder") or "",
                    "type": image.get("type") or "output",
                })
    return images


class JobStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}

    def create(self, arguments):
        job_id = uuid.uuid4().hex
        now = time.time()
        with self.lock:
            self._prune(now)
            self.jobs[job_id] = {
                "job_id": job_id, "state": "starting_backend", "created_at": now,
                "updated_at": now, "prompt_id": None, "images": [], "error": None,
            }
        thread = threading.Thread(target=self._run, args=(job_id, deepcopy(arguments)),
                                  name="stabilitymatrix-job-%s" % job_id[:8], daemon=True)
        thread.start()
        return self.get(job_id)

    def _prune(self, now):
        old = [key for key, job in self.jobs.items()
               if now - job.get("updated_at", now) > 24 * 3600]
        for key in old:
            self.jobs.pop(key, None)

    def update(self, job_id, **patch):
        with self.lock:
            job = self.jobs[job_id]
            job.update(patch)
            job["updated_at"] = time.time()

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(str(job_id or ""))
            if job is None:
                raise ToolError("job_not_found", "Задача генерации не найдена")
            return deepcopy(job)

    def wait(self, job_id, timeout):
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            job = self.get(job_id)
            if job["state"] in ("completed", "error") or time.time() >= deadline:
                return job
            time.sleep(0.25)

    def asset(self, token):
        token = str(token or "")
        if len(token) < 64 or len(token) > 1024:
            raise ToolError("image_not_found", "Изображение не найдено")
        return _image_from_token(token)

    def _run(self, job_id, arguments):
        try:
            wait_for_backend()
            self.update(job_id, state="preparing")
            profile = load_profile()
            overrides = arguments.get("config") or {}
            generation = resolve_config(overrides)
            positive = str(arguments.get("positive_prompt", profile["prompt"]["positive"]) or "")
            negative = str(arguments.get("negative_prompt", profile["prompt"]["negative"]) or "")
            if not positive.strip():
                raise ToolError("prompt_empty", "Positive prompt пуст")
            seed = generation["seed"]
            if seed < 0:
                seed = random.SystemRandom().randint(0, MAX_SEED)
            prefix = str(arguments.get("filename_prefix") or "MCP-Hub/StabilityMatrix").strip()
            if not prefix or len(prefix) > 180 or "\x00" in prefix or ".." in prefix:
                raise ToolError("invalid_filename_prefix", "Некорректный filename_prefix")
            workflows = []
            for batch in range(generation["batches"]):
                batch_seed = min(seed + batch, MAX_SEED)
                workflow = build_workflow(generation, positive, negative, batch_seed, prefix)
                ui_workflow = build_ui_workflow(workflow)
                queued = _api_request("/prompt", method="POST",
                                      payload={
                                          "prompt": workflow,
                                          "client_id": job_id,
                                          "extra_data": {
                                              "extra_pnginfo": {"workflow": ui_workflow},
                                          },
                                      }, timeout=10.0)
                prompt_id = queued.get("prompt_id")
                if not prompt_id:
                    raise ToolError("queue_failed", "ComfyUI не вернул prompt_id", queued)
                workflows.append(str(prompt_id))
            self.update(job_id, state="queued", prompt_id=workflows[0],
                        prompt_ids=workflows, config=generation, seed=seed)
            self._wait_outputs(job_id, workflows)
        except ToolError as exc:
            self.update(job_id, state="error", error=exc.payload()["error"])
        except Exception as exc:  # adapter must turn worker crashes into inspectable jobs
            self.update(job_id, state="error",
                        error={"code": "internal_error", "message": str(exc)})

    def _wait_outputs(self, job_id, prompt_ids, timeout=900.0):
        deadline = time.time() + timeout
        completed = {}
        while time.time() < deadline:
            for prompt_id in prompt_ids:
                if prompt_id in completed:
                    continue
                history = _api_request("/history/%s" % prompt_id, timeout=5.0)
                entry = history.get(prompt_id)
                if entry:
                    status = entry.get("status") or {}
                    if status.get("status_str") == "error" or status.get("completed") is False:
                        raise ToolError("generation_failed", "ComfyUI завершил задачу с ошибкой",
                                        status.get("messages") or status)
                    images = _outputs_from_history(entry)
                    if images:
                        completed[prompt_id] = images
            if len(completed) == len(prompt_ids):
                images = []
                for prompt_id in prompt_ids:
                    images.extend(completed[prompt_id])
                for image in images:
                    image["asset_token"] = _image_token(image)
                self.update(job_id, state="completed", images=images)
                return
            time.sleep(1.0)
        raise ToolError("generation_timeout", "Генерация не завершилась за %d секунд" % timeout)


JOBS = JobStore()


class DownloadStore:
    TERMINAL_STATES = frozenset({"completed", "error"})

    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}
        self.active_targets = set()

    def create(self, arguments):
        if not isinstance(arguments, dict):
            raise ToolError("invalid_download", "Параметры загрузки должны быть объектом")
        unknown = sorted(set(arguments) - {"url", "model_type", "filename"})
        if unknown:
            raise ToolError("invalid_download", "Неизвестные параметры загрузки", unknown)
        source_url = _validate_download_url(arguments.get("url"))
        model_type = str(arguments.get("model_type") or "").strip().lower()
        destination = model_destination(model_type)
        filename = _safe_model_filename(arguments.get("filename"), source_url)
        target = destination / filename
        target_key = str(target.resolve()).lower()
        job_id = uuid.uuid4().hex
        now = time.time()
        token = _download_token(job_id)
        with self.lock:
            self._prune(now)
            if target.exists():
                raise ToolError("model_exists", "Файл модели уже существует",
                                {"model_type": model_type, "filename": filename})
            if target_key in self.active_targets:
                raise ToolError("download_already_running",
                                "Эта модель уже загружается")
            self.active_targets.add(target_key)
            self.jobs[job_id] = {
                "job_id": job_id,
                "state": "queued",
                "model_type": model_type,
                "filename": filename,
                "relative_path": "%s/%s" % (model_type, filename),
                "source_host": urlsplit(source_url).hostname,
                "bytes_downloaded": 0,
                "bytes_total": None,
                "percent": None,
                "speed_bps": 0.0,
                "eta_seconds": None,
                "progress_url": (oauth.public_base() + DOWNLOAD_OUTPUT_PATH.rstrip("/")) + "/" + token,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "source_url": source_url,
                "target_path": str(target),
                "target_key": target_key,
            }
        thread = threading.Thread(target=self._run, args=(job_id,),
                                  name="stabilitymatrix-download-%s" % job_id[:8], daemon=True)
        thread.start()
        return self.get(job_id)

    def _prune(self, now):
        old = [key for key, job in self.jobs.items()
               if job.get("state") in self.TERMINAL_STATES and
               now - job.get("updated_at", now) > 24 * 3600]
        for key in old:
            self.jobs.pop(key, None)

    def update(self, job_id, **patch):
        with self.lock:
            job = self.jobs[job_id]
            job.update(patch)
            job["updated_at"] = time.time()

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(str(job_id or ""))
            if job is None:
                raise ToolError("download_not_found", "Задача загрузки не найдена")
            result = {key: deepcopy(value) for key, value in job.items()
                      if key not in ("source_url", "target_path", "target_key")}
        if result["state"] == "completed":
            result["message"] = "Модель загружена и доступна ComfyUI"
        elif result["state"] == "error":
            result["message"] = "Загрузка модели завершилась с ошибкой"
        else:
            result["message"] = "Загрузка выполняется в фоне; карточка обновляется автоматически"
        return result

    def from_token(self, token):
        return self.get(_download_job_from_token(token))

    def _run(self, job_id):
        part = None
        response = None
        with self.lock:
            internal = dict(self.jobs[job_id])
        target = Path(internal["target_path"])
        try:
            self.update(job_id, state="resolving")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ToolError("model_exists", "Файл модели уже существует")
            response, _final_url = _open_download(internal["source_url"])
            raw_total = response.headers.get("Content-Length")
            try:
                total = int(raw_total) if raw_total is not None else None
            except (TypeError, ValueError):
                raise ToolError("invalid_content_length",
                                "Сервер вернул некорректный Content-Length")
            if total is not None and (total <= 0 or total > MAX_MODEL_DOWNLOAD):
                raise ToolError("model_too_large",
                                "Размер модели превышает лимит 100 GiB")
            if total is not None:
                free = shutil.disk_usage(target.parent).free
                if free < total + 256 * 1024 * 1024:
                    raise ToolError("insufficient_space",
                                    "Недостаточно свободного места для модели",
                                    {"required_bytes": total, "free_bytes": free})
            part = target.with_name(".%s.%s.part" % (target.name, job_id[:12]))
            started = time.monotonic()
            last_update = started
            downloaded = 0
            self.update(job_id, state="downloading", bytes_total=total)
            with response, part.open("xb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > MAX_MODEL_DOWNLOAD:
                        raise ToolError("model_too_large",
                                        "Загрузка превысила лимит 100 GiB")
                    now = time.monotonic()
                    if now - last_update >= 0.25:
                        elapsed = max(now - started, 0.001)
                        speed = downloaded / elapsed
                        percent = (downloaded * 100.0 / total) if total else None
                        eta = ((total - downloaded) / speed) if total and speed > 0 else None
                        self.update(job_id, bytes_downloaded=downloaded,
                                    percent=percent, speed_bps=speed, eta_seconds=eta)
                        last_update = now
                output.flush()
                os.fsync(output.fileno())
            if total is not None and downloaded != total:
                raise ToolError("download_incomplete",
                                "Сервер завершил передачу раньше заявленного размера",
                                {"expected_bytes": total, "received_bytes": downloaded})
            if target.exists():
                raise ToolError("model_exists", "Файл модели появился во время загрузки")
            os.replace(str(part), str(target))
            part = None
            elapsed = max(time.monotonic() - started, 0.001)
            self.update(job_id, state="completed", bytes_downloaded=downloaded,
                        bytes_total=total, percent=100.0, speed_bps=downloaded / elapsed,
                        eta_seconds=0.0)
        except ToolError as exc:
            self.update(job_id, state="error", error=exc.payload()["error"])
        except Exception as exc:
            self.update(job_id, state="error",
                        error={"code": "download_failed", "message": str(exc)})
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if part is not None:
                try:
                    part.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            with self.lock:
                self.active_targets.discard(internal["target_key"])


DOWNLOADS = DownloadStore()


def _json_content(value, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False,
                                                                 indent=2)}],
            "isError": bool(is_error)}


def _image_display_markdown(image_urls, labels=None):
    """Build a transcript-safe preview for hosts that do not mount MCP App UI."""
    blocks = []
    labels = list(labels or [])
    for index, image_url in enumerate(image_urls, 1):
        label = labels[index - 1] if index <= len(labels) else "Результат %d" % index
        blocks.append("![%s](%s)\n[Открыть оригинал](%s)" %
                      (label, image_url, image_url))
    return "\n\n".join(blocks)


def _image_resource_links(images, image_urls, labels=None):
    """Expose generated files to clients that support MCP resources but not App UI."""
    links = []
    labels = list(labels or [])
    for index, (image, image_url) in enumerate(zip(images, image_urls), 1):
        label = labels[index - 1] if index <= len(labels) else "Результат %d" % index
        filename = str(image.get("filename") or "stabilitymatrix-image-%d" % index)
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        links.append({
            "type": "resource_link",
            "name": filename,
            "title": label,
            "uri": image_url,
            "description": "Изображение, созданное локальным Stability Matrix/ComfyUI",
            "mimeType": mime,
            "annotations": {"audience": ["user", "assistant"], "priority": 1},
        })
    return links


def _job_content(job, include_images=True):
    image_urls = []
    if include_images and job["state"] == "completed":
        image_urls = [(oauth.public_base() + IMAGE_OUTPUT_PATH.rstrip("/")) + "/" + image["asset_token"]
                      for image in job.get("images") or [] if image.get("asset_token")]
    structured = {
        "job_id": job["job_id"],
        "state": job["state"],
        "image_urls": image_urls,
        "comfyui_url": comfyui_web_url(),
    }
    if image_urls:
        structured["display_markdown"] = _image_display_markdown(image_urls)
    if job.get("prompt_id"):
        structured["prompt_id"] = job["prompt_id"]
    if job.get("error"):
        structured["error"] = job["error"]
    if job["state"] not in ("completed", "error"):
        structured["message"] = (
            "Генерация ещё выполняется. Повторите stabilitymatrix_job_status позже."
        )
    text = {"success": job["state"] != "error", "data": structured}
    text_content = json.dumps(text, ensure_ascii=False, indent=2)
    if structured.get("display_markdown"):
        text_content = (
            "Desktop fallback: в финальном ответе обязательно вставь значение "
            "data.display_markdown дословно. Не заменяй изображение одним описанием.\n\n" +
            text_content
        )
    content = [{"type": "text", "text": text_content}]
    if include_images and job["state"] == "completed":
        embedded, warnings = _embedded_image_content(job.get("images") or [])
        content.extend(embedded)
        content.extend(_image_resource_links(job.get("images") or [], image_urls))
        if warnings:
            content.append({"type": "text", "text": "\n".join(warnings)})
    return {
        "structuredContent": structured,
        "content": content,
        "_meta": {"stabilitymatrix/result": structured},
        "isError": job["state"] == "error",
    }


def _embedded_image_content(images):
    """Return native MCP image and resource blocks for model and host rendering."""
    parts = []
    warnings = []
    total_size = 0
    resource_total_size = 0
    for index, image in enumerate(images[:MAX_EMBEDDED_IMAGES], 1):
        view = {
            "filename": image.get("filename") or "",
            "subfolder": image.get("subfolder") or "",
            "type": image.get("type") or "output",
            # A bounded visual copy is enough for model vision; UI URLs still serve originals.
            "preview": "webp;90",
        }
        try:
            body, content_type = _api_request(
                "/view?" + urlencode(view), timeout=30.0, raw=True)
            mime = (content_type or "").split(";", 1)[0].strip().lower()
            if not mime.startswith("image/"):
                mime = mimetypes.guess_type(view["filename"])[0] or "image/png"
            if len(body) > MAX_EMBEDDED_IMAGE_BYTES:
                raise ToolError("image_too_large",
                                "Изображение слишком большое для передачи модели")
            if total_size + len(body) > MAX_EMBEDDED_IMAGES_TOTAL:
                warnings.append(
                    "Остальные изображения доступны по image_urls, но не встроены из-за лимита размера."
                )
                break
            encoded = base64.b64encode(body).decode("ascii")
            annotations = {"audience": ["user", "assistant"], "priority": 1}
            parts.append({
                "type": "image",
                "data": encoded,
                "mimeType": mime,
                "annotations": annotations,
            })
            total_size += len(body)
            asset_token = image.get("asset_token")
            if asset_token and len(body) <= MAX_EMBEDDED_RESOURCE_BYTES \
                    and resource_total_size + len(body) <= MAX_EMBEDDED_RESOURCES_TOTAL:
                parts.append({
                    "type": "resource",
                    "resource": {
                        "uri": (oauth.public_base() + IMAGE_OUTPUT_PATH.rstrip("/")) + "/" + asset_token,
                        "mimeType": mime,
                        "blob": encoded,
                    },
                    "annotations": annotations,
                })
                resource_total_size += len(body)
            elif asset_token:
                warnings.append(
                    "Изображение %d передано модели, но не продублировано как Desktop resource "
                    "из-за лимита размера." % index
                )
        except ToolError as exc:
            warnings.append("Изображение %d не удалось встроить: %s" % (index, str(exc)))
    if len(images) > MAX_EMBEDDED_IMAGES:
        warnings.append(
            "В ответ встроены первые %d изображений; остальные доступны по image_urls."
            % MAX_EMBEDDED_IMAGES
        )
    return parts, warnings


def _comparison_label(value, fallback):
    label = str(value or fallback).strip()
    if not label or len(label) > 100 or "\x00" in label:
        raise ToolError("invalid_comparison_label", "Некорректная подпись сравнения")
    return label


def _wait_jobs(job_ids, timeout):
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        jobs = [JOBS.get(job_id) for job_id in job_ids]
        if (all(job["state"] in ("completed", "error") for job in jobs) or
                time.time() >= deadline):
            return jobs
        time.sleep(0.25)


def _comparison_side(job, label):
    images = job.get("images") or []
    image_url = ""
    if job["state"] == "completed" and images and images[0].get("asset_token"):
        image_url = (oauth.public_base() + IMAGE_OUTPUT_PATH.rstrip("/")) + "/" + images[0]["asset_token"]
    side = {
        "job_id": job["job_id"],
        "state": job["state"],
        "label": label,
        "image_url": image_url,
    }
    if job.get("prompt_id"):
        side["prompt_id"] = job["prompt_id"]
    if job.get("error"):
        side["error"] = job["error"]
    return side


def _comparison_content(left_job, right_job, label_a, label_b, include_images=True):
    if left_job["state"] == "error" or right_job["state"] == "error":
        state = "error"
        message = "Одна из генераций сравнения завершилась с ошибкой"
    elif left_job["state"] == "completed" and right_job["state"] == "completed":
        state = "completed"
        message = "Обе генерации готовы: сравните реальные изображения A и B"
    else:
        state = "running"
        message = "Сравнение ещё выполняется; используйте @vs-status позже"
    structured = {
        "state": state,
        "left": _comparison_side(left_job, label_a),
        "right": _comparison_side(right_job, label_b),
        "comfyui_url": comfyui_web_url(),
        "message": message,
    }
    image_urls = [side["image_url"] for side in (structured["left"], structured["right"])
                  if side.get("image_url")]
    labels = [side["label"] for side in (structured["left"], structured["right"])
              if side.get("image_url")]
    if image_urls:
        structured["display_markdown"] = _image_display_markdown(image_urls, labels)
    envelope = {"success": state != "error", "data": structured}
    text_content = json.dumps(envelope, ensure_ascii=False, indent=2)
    if structured.get("display_markdown"):
        text_content = (
            "Desktop fallback: в финальном ответе обязательно вставь значение "
            "data.display_markdown дословно с обеими картинками.\n\n" +
            text_content
        )
    content = [{"type": "text", "text": text_content}]
    if include_images and state == "completed":
        chosen = []
        if left_job.get("images"):
            chosen.append(left_job["images"][0])
        if right_job.get("images"):
            chosen.append(right_job["images"][0])
        embedded, warnings = _embedded_image_content(chosen)
        content.extend(embedded)
        content.extend(_image_resource_links(chosen, image_urls, labels))
        if warnings:
            content.append({"type": "text", "text": "\n".join(warnings)})
    return {
        "structuredContent": structured,
        "content": content,
        "_meta": {"stabilitymatrix/result": structured},
        "isError": state == "error",
    }


def _download_content(job):
    structured = {
        "job_id": job["job_id"],
        "state": job["state"],
        "model_type": job["model_type"],
        "filename": job["filename"],
        "relative_path": job["relative_path"],
        "source_host": job.get("source_host") or "",
        "bytes_downloaded": int(job.get("bytes_downloaded") or 0),
        "bytes_total": job.get("bytes_total"),
        "percent": job.get("percent"),
        "speed_bps": float(job.get("speed_bps") or 0),
        "eta_seconds": job.get("eta_seconds"),
        "progress_url": job["progress_url"],
        "message": job.get("message") or "",
    }
    if job.get("error"):
        structured["error"] = job["error"]
    envelope = {"success": job["state"] != "error", "data": structured}
    return {
        "structuredContent": structured,
        "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False,
                                                            indent=2)}],
        "isError": job["state"] == "error",
    }


JOB_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "state": {"type": "string"},
        "image_urls": {"type": "array", "items": {"type": "string", "format": "uri"}},
        "comfyui_url": {"type": "string", "format": "uri"},
        "prompt_id": {"type": "string"},
        "message": {"type": "string"},
        "error": {"type": "object"},
        "display_markdown": {"type": "string"},
    },
    "required": ["job_id", "state", "image_urls", "comfyui_url"],
    "additionalProperties": False,
}


DOWNLOAD_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "state": {"type": "string"},
        "model_type": {"type": "string"},
        "filename": {"type": "string"},
        "relative_path": {"type": "string"},
        "source_host": {"type": "string"},
        "bytes_downloaded": {"type": "integer"},
        "bytes_total": {"type": ["integer", "null"]},
        "percent": {"type": ["number", "null"]},
        "speed_bps": {"type": "number"},
        "eta_seconds": {"type": ["number", "null"]},
        "progress_url": {"type": "string", "format": "uri"},
        "message": {"type": "string"},
        "error": {"type": "object"},
    },
    "required": ["job_id", "state", "model_type", "filename", "relative_path",
                 "source_host", "bytes_downloaded", "bytes_total", "percent",
                 "speed_bps", "eta_seconds", "progress_url", "message"],
    "additionalProperties": False,
}


COMPARISON_SIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "state": {"type": "string"},
        "label": {"type": "string"},
        "image_url": {"type": "string"},
        "prompt_id": {"type": "string"},
        "error": {"type": "object"},
    },
    "required": ["job_id", "state", "label", "image_url"],
    "additionalProperties": False,
}


COMPARISON_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"type": "string"},
        "left": COMPARISON_SIDE_SCHEMA,
        "right": COMPARISON_SIDE_SCHEMA,
        "comfyui_url": {"type": "string", "format": "uri"},
        "message": {"type": "string"},
        "display_markdown": {"type": "string"},
    },
    "required": ["state", "left", "right", "comfyui_url", "message"],
    "additionalProperties": False,
}

IMAGE_TOOL_META = {
    "ui": {"resourceUri": IMAGE_UI_URI},
    "openai/outputTemplate": IMAGE_UI_URI,
    "openai/toolInvocation/invoking": "Stability Matrix генерирует изображение…",
    "openai/toolInvocation/invoked": "Stability Matrix завершил генерацию",
}


DOWNLOAD_TOOL_META = {
    "ui": {"resourceUri": DOWNLOAD_UI_URI},
    "openai/outputTemplate": DOWNLOAD_UI_URI,
    "openai/toolInvocation/invoking": "Stability Matrix начинает загрузку модели…",
    "openai/toolInvocation/invoked": "Фоновая загрузка модели запущена",
}


COMPARISON_TOOL_META = {
    "ui": {"resourceUri": COMPARISON_UI_URI},
    "openai/outputTemplate": COMPARISON_UI_URI,
    "openai/toolInvocation/invoking": "Stability Matrix генерирует варианты A и B…",
    "openai/toolInvocation/invoked": "Сравнение Stability Matrix готово",
}


APP_INSTRUCTIONS = """ComfyUI MCP: @config и @prompt управляют профилем;
@models показывает модели; @generate запускает генерацию через ComfyUI API.
comfyui_generate_from_chat_image принимает текстовое описание референса от клиента,
а не исходные пиксели: это image-to-prompt, не img2img.
@vs создаёт два варианта с общим seed; @download загружает модель, карточка
показывает прогресс. Эти операции могут расходовать GPU, диск и сетевой трафик.
Генерация ждёт до wait_seconds. Для незавершённых заданий доступны status tools.
Готовые изображения возвращаются как ImageContent; display_markdown служит
резервным отображением для клиентов без MCP Apps. Ошибки ComfyUI возвращаются клиенту."""


READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
}


LOCAL_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "openWorldHint": False,
}


NETWORK_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "openWorldHint": True,
}


GENERATION_CONFIG_PROPERTIES = {
    "model": {"type": "string", "description": "Checkpoint или diffusion model из @models."},
    "sampler": {"type": "string"},
    "scheduler": {"type": "string"},
    "steps": {"type": "integer"},
    "cfg_scale": {"type": "number"},
    "width": {"type": "integer"},
    "height": {"type": "integer"},
    "seed": {"type": "integer"},
    "batch_size": {"type": "integer"},
    "batches": {"type": "integer"},
}


TOOLS = [
    {
        "name": "stabilitymatrix_status",
        "title": "@status — StabilityMatrix status",
        "description": "Use this when пользователь пишет @status/status для StabilityMatrix или просит проверить локальный MCP, установку Stability Matrix либо ComfyUI API.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_config_get",
        "title": "@config — Read StabilityMatrix config",
        "description": "Use this when пользователь пишет @config/config для StabilityMatrix и хочет прочитать активные model, sampler, scheduler, steps, CFG, размер, seed или batch. Не использовать для изменения настроек.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_config_set",
        "title": "@config — Update StabilityMatrix config",
        "description": "Use this when пользователь пишет @config/config для StabilityMatrix и просит изменить настройки. Изменить только переданные поля блока Config; остальные поля сохраняются.",
        "inputSchema": {"type": "object", "properties": {
            "model": {"type": "string", "description": "Имя checkpoint-модели из @models."},
            "sampler": {"type": "string", "description": "ComfyUI sampler, например euler_ancestral."},
            "scheduler": {"type": "string", "description": "ComfyUI scheduler, например normal."},
            "steps": {"type": "integer", "description": "Количество sampling steps."},
            "cfg_scale": {"type": "number", "description": "CFG scale."},
            "width": {"type": "integer", "description": "Ширина изображения в пикселях."},
            "height": {"type": "integer", "description": "Высота изображения в пикселях."},
            "seed": {"type": "integer", "description": "Seed; -1 означает случайный seed."},
            "batch_size": {"type": "integer", "description": "Число изображений в одном batch."},
            "batches": {"type": "integer", "description": "Число последовательных batches."},
        }, "additionalProperties": False},
        "annotations": LOCAL_WRITE_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_prompt_get",
        "title": "@prompt — Read StabilityMatrix prompt",
        "description": "Use this when пользователь пишет @prompt/prompt для StabilityMatrix и хочет прочитать сохранённые positive и negative prompt. Не использовать для изменения prompt.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_prompt_set",
        "title": "@prompt — Update StabilityMatrix prompt",
        "description": "Use this when пользователь пишет @prompt/prompt для StabilityMatrix и просит сохранить или изменить positive и/или negative prompt.",
        "inputSchema": {"type": "object", "properties": {
            "positive": {"type": "string", "description": "Сохраняемый positive prompt."},
            "negative": {"type": "string", "description": "Сохраняемый negative prompt."},
        }, "additionalProperties": False},
        "annotations": LOCAL_WRITE_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_models",
        "title": "@models — StabilityMatrix models",
        "description": "Use this when пользователь пишет @models/models для StabilityMatrix или просит список локальных моделей. Запускает ComfyUI при необходимости и возвращает checkpoints, standalone diffusion_models/UNet, text_encoders и VAE. Anima diffusion models поддерживаются в @generate. Не генерирует изображение.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": READ_ONLY_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_model_download",
        "title": "@download — Download model into StabilityMatrix",
        "description": "Use this when пользователь пишет @download/download, передаёт публичный HTTPS URL файла модели и просит скачать его в Stability Matrix/ComfyUI. Загрузка идёт в фоне в общую библиотеку Stability Matrix; MCP UI автоматически показывает процент, скорость и ETA. Поддерживает checkpoints, diffusion_models, LoRA, VAE, text encoders, ControlNet и другие перечисленные категории. Не вызывать status циклически после старта.",
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "format": "uri",
                    "description": "Публичный прямой HTTPS URL файла модели."},
            "model_type": {"type": "string", "enum": sorted(MODEL_DESTINATIONS),
                           "description": "Категория ComfyUI/Stability Matrix для модели."},
            "filename": {"type": "string",
                         "description": "Необязательное имя файла, если оно не следует из URL."},
        }, "required": ["url", "model_type"], "additionalProperties": False},
        "outputSchema": DOWNLOAD_OUTPUT_SCHEMA,
        "_meta": DOWNLOAD_TOOL_META,
        "annotations": NETWORK_WRITE_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_model_download_status",
        "title": "@download-status — Model download status",
        "description": "Use this only when пользователь явно просит проверить загрузку по job_id или клиент не показывает MCP UI. Возвращает текущие bytes, percent, speed и ETA. Не вызывать циклически: карточка @download обновляется сама без новых model/tool calls.",
        "inputSchema": {"type": "object", "properties": {
            "job_id": {"type": "string", "description": "Job ID, возвращённый @download."},
        }, "required": ["job_id"], "additionalProperties": False},
        "outputSchema": DOWNLOAD_OUTPUT_SCHEMA,
        "_meta": DOWNLOAD_TOOL_META,
        "annotations": READ_ONLY_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_compare",
        "title": "@vs — Compare two StabilityMatrix generations",
        "description": 'Создаёт две генерации ComfyUI для сравнения моделей, prompt или настроек. По умолчанию использует общий seed; возвращает ImageContent и интерактивный слайдер. Если время ожидания истекло, используйте compare_status.',
        "inputSchema": {"type": "object", "properties": {
            "positive_prompt_a": {"type": "string", "description": "Positive prompt варианта A."},
            "positive_prompt_b": {"type": "string", "description": "Positive prompt B; по умолчанию равен A."},
            "negative_prompt_a": {"type": "string", "description": "Negative prompt варианта A."},
            "negative_prompt_b": {"type": "string", "description": "Negative prompt B; по умолчанию равен A."},
            "config_a": {"type": "object", "properties": GENERATION_CONFIG_PROPERTIES,
                         "additionalProperties": False},
            "config_b": {"type": "object", "properties": GENERATION_CONFIG_PROPERTIES,
                         "additionalProperties": False},
            "label_a": {"type": "string", "description": "Короткая подпись A, например имя модели."},
            "label_b": {"type": "string", "description": "Короткая подпись B, например имя модели."},
            "same_seed": {"type": "boolean", "default": True,
                          "description": "Использовать одинаковый seed в A и B."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 600,
                             "default": DEFAULT_WAIT_SECONDS},
        }, "required": ["positive_prompt_a"], "additionalProperties": False},
        "outputSchema": COMPARISON_OUTPUT_SCHEMA,
        "_meta": COMPARISON_TOOL_META,
        "annotations": LOCAL_WRITE_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_compare_status",
        "title": "@vs-status — Comparison status",
        "description": "Use this only when @vs вернул незавершённое состояние или пользователь явно просит проверить сравнение. После завершения возвращает оба изображения как MCP ImageContent и тот же Image Comparison Slider; в финальном ответе обязательно вставить result.display_markdown дословно для Desktop fallback.",
        "inputSchema": {"type": "object", "properties": {
            "job_id_a": {"type": "string"},
            "job_id_b": {"type": "string"},
            "label_a": {"type": "string"},
            "label_b": {"type": "string"},
        }, "required": ["job_id_a", "job_id_b"], "additionalProperties": False},
        "outputSchema": COMPARISON_OUTPUT_SCHEMA,
        "_meta": COMPARISON_TOOL_META,
        "annotations": READ_ONLY_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_generate",
        "title": "@generate — Text-only generation via StabilityMatrix",
        "description": 'Генерирует изображение из текста через ComfyUI. Поддерживает checkpoints и Anima diffusion models. Возвращает ImageContent, ссылки и редактируемый workflow. После истечения wait_seconds используйте job_status.',
        "inputSchema": {"type": "object", "properties": {
            "positive_prompt": {"type": "string", "description": "Что сгенерировать; текст после @generate."},
            "negative_prompt": {"type": "string", "description": "Что исключить из изображения."},
            "filename_prefix": {"type": "string", "description": "Безопасный префикс имени выходного файла."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 600,
                             "default": DEFAULT_WAIT_SECONDS,
                             "description": "Сколько секунд ждать готовое изображение в этом вызове."},
            "config": {"type": "object", "properties": {
                "model": {"type": "string", "description": "Checkpoint из @models."},
                "sampler": {"type": "string"}, "scheduler": {"type": "string"},
                "steps": {"type": "integer"}, "cfg_scale": {"type": "number"},
                "width": {"type": "integer"}, "height": {"type": "integer"},
                "seed": {"type": "integer"}, "batch_size": {"type": "integer"},
                "batches": {"type": "integer"},
            }, "additionalProperties": False,
                "description": "Необязательные настройки только для этой генерации."},
        }, "additionalProperties": False},
        "outputSchema": JOB_OUTPUT_SCHEMA,
        "_meta": IMAGE_TOOL_META,
        "annotations": LOCAL_WRITE_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_generate_from_chat_image",
        "title": "@reference — Chat image to prompt via StabilityMatrix",
        "description": 'Генерирует через ComfyUI по текстовому описанию референса, составленному клиентом. Это image-to-prompt: пиксели референса в ComfyUI не передаются. Возвращает ImageContent и ссылки на результат.',
        "inputSchema": {"type": "object", "properties": {
            "reference_analysis": {
                "type": "string", "minLength": 1, "maxLength": 20000,
                "description": "Собственный визуальный анализ реально приложенной картинки: персонаж/объекты, поза, композиция, ракурс, пропорции, палитра, свет, фон, техника и отличительные признаки стиля. Не просить это описание у пользователя.",
            },
            "positive_prompt": {
                "type": "string", "minLength": 1, "maxLength": 100000,
                "description": "Подробный English txt2img prompt, который ChatGPT самостоятельно составила по визуальному анализу прикреплённой картинки и текстовым правкам пользователя. Именно этот текст уйдёт в локальный ComfyUI.",
            },
            "negative_prompt": {
                "type": "string", "maxLength": 100000,
                "description": "English negative prompt, самостоятельно составленный для устранения артефактов и нежелательных отличий от референса.",
            },
            "filename_prefix": {"type": "string", "description": "Безопасный префикс имени выходного файла."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 600,
                             "default": DEFAULT_WAIT_SECONDS,
                             "description": "Сколько секунд ждать готовое изображение в этом вызове."},
            "config": {"type": "object", "properties": {
                "model": {"type": "string", "description": "Checkpoint или поддерживаемая diffusion model из @models."},
                "sampler": {"type": "string"}, "scheduler": {"type": "string"},
                "steps": {"type": "integer"}, "cfg_scale": {"type": "number"},
                "width": {"type": "integer"}, "height": {"type": "integer"},
                "seed": {"type": "integer"}, "batch_size": {"type": "integer"},
                "batches": {"type": "integer"},
            }, "additionalProperties": False,
                "description": "Необязательные настройки только для этой генерации."},
        }, "required": ["reference_analysis", "positive_prompt"],
            "additionalProperties": False},
        "outputSchema": JOB_OUTPUT_SCHEMA,
        "_meta": IMAGE_TOOL_META,
        "annotations": LOCAL_WRITE_ANNOTATIONS,
    },
    {
        "name": "stabilitymatrix_job_status",
        "title": "@job — StabilityMatrix job status",
        "description": "Use this only when пользователь пишет @job/job status с конкретным job_id либо предыдущий @generate вернул незавершённое состояние из-за wait_seconds. Не вызывать после state=completed или state=error. Если задача завершилась, tool возвращает готовые пиксели как MCP ImageContent для vision-анализа; в финальном ответе обязательно вставить result.display_markdown дословно для Desktop fallback.",
        "inputSchema": {"type": "object", "properties": {
            "job_id": {"type": "string", "description": "Job ID, возвращённый @generate."},
            "include_images": {"type": "boolean", "default": True,
                               "description": "Вернуть подписанные URL готовых изображений."},
        }, "required": ["job_id"], "additionalProperties": False},
        "outputSchema": JOB_OUTPUT_SCHEMA,
        "_meta": IMAGE_TOOL_META,
        "annotations": READ_ONLY_ANNOTATIONS,
    },
]


def _comfyui_labels(value):
    """Новые имена интерфейса; URI, OAuth и файлы состояния совместимы со старыми чатами."""
    if isinstance(value, str):
        return value.replace("Stability Matrix/ComfyUI", "ComfyUI").replace("Stability Matrix", "ComfyUI").replace("StabilityMatrix", "ComfyUI").replace("stabilitymatrix_", "comfyui_")
    if isinstance(value, dict):
        return {key: _comfyui_labels(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_comfyui_labels(item) for item in value]
    return value


TOOLS = _comfyui_labels(TOOLS)
APP_INSTRUCTIONS = _comfyui_labels(APP_INSTRUCTIONS)
IMAGE_UI_HTML = IMAGE_UI_HTML.replace("Stability Matrix", "ComfyUI")
COMPARISON_UI_HTML = COMPARISON_UI_HTML.replace("Stability Matrix", "ComfyUI")


def call_tool(name, arguments):
    if name.startswith("comfyui_"):
        name = "stabilitymatrix_" + name[len("comfyui_"):]
    arguments = arguments or {}
    if name == "stabilitymatrix_status":
        package, _python = find_package()
        profile = load_profile()
        return _json_content({"success": True, "data": {
            "backend": "online" if backend_online() else "offline",
            "api": comfyui_api_url(), "web_url": comfyui_web_url(),
            "listen": config.load().get("comfyuiListen") or COMFYUI_LISTEN,
            "package_found": package is not None,
            "package_path": str(package) if package else None, "profile": profile,
        }})
    if name == "stabilitymatrix_config_get":
        return _json_content({"success": True, "data": load_profile()["config"]})
    if name == "stabilitymatrix_config_set":
        return _json_content({"success": True, "data": set_config(arguments)})
    if name == "stabilitymatrix_prompt_get":
        return _json_content({"success": True, "data": load_profile()["prompt"]})
    if name == "stabilitymatrix_prompt_set":
        return _json_content({"success": True, "data": set_prompt(arguments)})
    if name == "stabilitymatrix_models":
        if not backend_online():
            started = start_backend()
            return _json_content({"success": True, "data": {
                "backend": started["state"], "models": [], "checkpoints": [],
                "diffusion_models": [], "text_encoders": [], "vae_models": [], "retry": True,
                "message": "ComfyUI запускается; повторите stabilitymatrix_models через несколько секунд",
            }})
        checkpoints = list_models()
        diffusion_models = list_diffusion_models()
        text_encoders = list_text_encoders()
        vae_models = list_vae_models()
        return _json_content({"success": True, "data": {
            # Keep `models` as a compatibility alias for existing clients.
            "models": checkpoints,
            "checkpoints": checkpoints,
            "diffusion_models": diffusion_models,
            "text_encoders": text_encoders,
            "vae_models": vae_models,
        }})
    if name == "stabilitymatrix_model_download":
        return _download_content(DOWNLOADS.create(arguments))
    if name == "stabilitymatrix_model_download_status":
        return _download_content(DOWNLOADS.get(arguments.get("job_id")))
    if name == "stabilitymatrix_compare":
        profile = load_profile()
        positive_a = str(arguments.get("positive_prompt_a") or "").strip()
        if not positive_a:
            raise ToolError("prompt_empty", "Positive prompt варианта A пуст")
        positive_b = str(arguments.get("positive_prompt_b") or positive_a).strip()
        negative_a = str(arguments.get("negative_prompt_a",
                                       profile["prompt"]["negative"]) or "")
        negative_b = str(arguments.get("negative_prompt_b", negative_a) or "")
        if any(len(value) > 100000 for value in
               (positive_a, positive_b, negative_a, negative_b)):
            raise ToolError("prompt_too_long", "Prompt сравнения слишком длинный")
        config_a = validate_config(arguments.get("config_a") or {}, profile["config"])
        config_b = validate_config(arguments.get("config_b") or {}, profile["config"])
        if arguments.get("same_seed", True) is not False:
            shared_seed = config_a["seed"] if config_a["seed"] >= 0 else config_b["seed"]
            if shared_seed < 0:
                shared_seed = random.SystemRandom().randint(0, MAX_SEED)
            config_a["seed"] = shared_seed
            config_b["seed"] = shared_seed
        label_a = _comparison_label(arguments.get("label_a"), config_a["model"] or "A")
        label_b = _comparison_label(arguments.get("label_b"), config_b["model"] or "B")
        wait_seconds = _number("wait_seconds",
                               arguments.get("wait_seconds", DEFAULT_WAIT_SECONDS),
                               0, 600, integer=True)
        comparison_id = uuid.uuid4().hex[:12]
        common_prefix = "MCP-Hub/StabilityMatrix-VS/%s" % comparison_id
        left = JOBS.create({
            "positive_prompt": positive_a,
            "negative_prompt": negative_a,
            "config": config_a,
            "filename_prefix": common_prefix + "/A",
        })
        right = JOBS.create({
            "positive_prompt": positive_b,
            "negative_prompt": negative_b,
            "config": config_b,
            "filename_prefix": common_prefix + "/B",
        })
        left, right = _wait_jobs([left["job_id"], right["job_id"]], wait_seconds)
        return _comparison_content(left, right, label_a, label_b)
    if name == "stabilitymatrix_compare_status":
        left = JOBS.get(arguments.get("job_id_a"))
        right = JOBS.get(arguments.get("job_id_b"))
        label_a = _comparison_label(arguments.get("label_a"),
                                    (left.get("config") or {}).get("model") or "A")
        label_b = _comparison_label(arguments.get("label_b"),
                                    (right.get("config") or {}).get("model") or "B")
        return _comparison_content(left, right, label_a, label_b)
    if name in ("stabilitymatrix_generate", "stabilitymatrix_generate_from_chat_image"):
        if name == "stabilitymatrix_generate_from_chat_image":
            if not str(arguments.get("reference_analysis") or "").strip():
                raise ToolError("reference_analysis_empty",
                                "ChatGPT должна передать визуальный анализ приложенной картинки")
            if not str(arguments.get("positive_prompt") or "").strip():
                raise ToolError("prompt_empty",
                                "ChatGPT должна составить positive_prompt по приложенной картинке")
        # Validate overrides before returning a job id; backend/model validation happens in worker.
        validate_config(arguments.get("config") or {}, load_profile()["config"])
        wait_seconds = _number("wait_seconds", arguments.get("wait_seconds", DEFAULT_WAIT_SECONDS),
                               0, 600, integer=True)
        job = JOBS.create(arguments)
        job = JOBS.wait(job["job_id"], wait_seconds)
        return _job_content(job, include_images=True)
    if name == "stabilitymatrix_job_status":
        return _job_content(JOBS.get(arguments.get("job_id")),
                            include_images=arguments.get("include_images", True) is not False)
    raise ToolError("tool_not_found", "Неизвестный инструмент: %s" % name)


def dispatch(request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    rpc_id = request.get("id")
    method = request.get("method")
    params = request.get("params", {})
    if not isinstance(method, str) or not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32600, "message": "Invalid Request"}}
    if method == "tools/call" and (not isinstance(params.get("name"), str) or
                                   not isinstance(params.get("arguments", {}), dict)):
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32602, "message": "Invalid params"}}
    if rpc_id is None:
        return None
    try:
        if method == "initialize":
            requested = (request.get("params") or {}).get("protocolVersion")
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {
                "protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False},
                                 "resources": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": APP_INSTRUCTIONS,
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": TOOLS}}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"resources": [
                {
                    "uri": IMAGE_UI_URI,
                    "name": "Stability Matrix generated image",
                    "description": "Inline preview for generated images",
                    "mimeType": "text/html;profile=mcp-app",
                },
                {
                    "uri": DOWNLOAD_UI_URI,
                    "name": "Stability Matrix model download",
                    "description": "Live progress for background model downloads",
                    "mimeType": "text/html;profile=mcp-app",
                },
                {
                    "uri": COMPARISON_UI_URI,
                    "name": "Stability Matrix image comparison",
                    "description": "Interactive before/after slider for two generations",
                    "mimeType": "text/html;profile=mcp-app",
                },
            ]}}
        if method == "resources/read":
            uri = (request.get("params") or {}).get("uri")
            if (uri not in IMAGE_UI_ALIASES
                    and uri != DOWNLOAD_UI_URI
                    and uri not in COMPARISON_UI_ALIASES):
                raise ToolError("resource_not_found", "UI resource не найден")
            if uri == DOWNLOAD_UI_URI:
                return {"jsonrpc": "2.0", "id": rpc_id, "result": {"contents": [{
                    "uri": DOWNLOAD_UI_URI,
                    "mimeType": "text/html;profile=mcp-app",
                    "text": DOWNLOAD_UI_HTML,
                    "_meta": {
                        "ui": {
                            "prefersBorder": True,
                            "domain": oauth.public_base(),
                            "csp": {"resourceDomains": [],
                                    "connectDomains": [oauth.public_base()]},
                        },
                        "openai/widgetCSP": {
                            "resource_domains": [],
                            "connect_domains": [oauth.public_base()],
                        },
                        "openai/widgetDescription": (
                            "Показывает живой прогресс фоновой загрузки модели в Stability Matrix."
                        ),
                    },
                }]}}
            if uri in COMPARISON_UI_ALIASES:
                return {"jsonrpc": "2.0", "id": rpc_id, "result": {"contents": [{
                    "uri": uri,
                    "mimeType": "text/html;profile=mcp-app",
                    "text": COMPARISON_UI_HTML,
                    "_meta": {
                        "ui": {
                            "prefersBorder": True,
                            "domain": oauth.public_base(),
                            "csp": {"resourceDomains": [oauth.public_base()],
                                    "connectDomains": []},
                        },
                        "openai/widgetCSP": {
                            "resource_domains": [oauth.public_base()],
                            "connect_domains": [],
                        },
                        "openai/widgetDescription": (
                            "Интерактивный Image Comparison Slider для вариантов A и B."
                        ),
                    },
                }]}}
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"contents": [{
                "uri": uri,
                "mimeType": "text/html;profile=mcp-app",
                "text": IMAGE_UI_HTML,
                "_meta": {
                    "ui": {
                        "prefersBorder": True,
                        "domain": oauth.public_base(),
                        "csp": {"resourceDomains": [oauth.public_base()], "connectDomains": []},
                    },
                    "openai/widgetCSP": {
                        "resource_domains": [oauth.public_base()],
                        "connect_domains": [],
                        "redirect_domains": [comfyui_web_origin()],
                    },
                    "openai/widgetDescription": "Показывает результат генерации Stability Matrix.",
                },
            }]}}
        if method == "tools/call":
            params = request.get("params") or {}
            result = call_tool(str(params.get("name") or ""), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": "Method not found"}}
    except ToolError as exc:
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": rpc_id,
                    "result": _json_content(exc.payload(), is_error=True)}
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32000, "message": str(exc), "data": exc.payload()["error"]}}
    except Exception:
        LOG.error("comfyui", "Внутренняя ошибка MCP-запроса", event="comfyui.dispatchFailed")
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32603, "message": "Internal error"}}


class Handler(BaseHTTPRequestHandler):
    server_version = "MCPHubStabilityMatrix/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    def _send(self, status, payload=None, headers=None, content_type=None):
        if isinstance(payload, bytes):
            body = payload
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type or "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith(DOWNLOAD_OUTPUT_PATH):
            token = path[len(DOWNLOAD_OUTPUT_PATH):]
            if not token or "/" in token:
                return self._send(404, {"error": "download not found"})
            try:
                job = DOWNLOADS.from_token(token)
                return self._send(200, _download_content(job)["structuredContent"],
                                  headers={"Access-Control-Allow-Origin": "*"})
            except ToolError:
                return self._send(404, {"error": "download not found"},
                                  headers={"Access-Control-Allow-Origin": "*"})
        if path.startswith(IMAGE_OUTPUT_PATH):
            token = path[len(IMAGE_OUTPUT_PATH):]
            if not token or "/" in token:
                return self._send(404, {"error": "image not found"})
            try:
                image = JOBS.asset(token)
                body, content_type = _api_request(
                    "/view?" + urlencode(image), timeout=30.0, raw=True)
                mime = (content_type or "").split(";", 1)[0]
                if not mime.startswith("image/"):
                    mime = mimetypes.guess_type(image["filename"])[0] or "image/png"
                return self._send(200, body, content_type=mime,
                                  headers=IMAGE_RESPONSE_HEADERS)
            except ToolError:
                return self._send(404, {"error": "image not found"})
        if path in ("/", "/healthz"):
            package, _python = find_package()
            return self._send(200, {"ok": True, "role": SERVER_NAME,
                                    "backend": backend_online(), "package": package is not None})
        return self._send(405, {"error": "SSE GET transport is not supported; use POST"})

    def do_DELETE(self):
        return self._send(204)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/", "/mcp"):
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "invalid content length"})
        if length <= 0 or length > MAX_BODY:
            self.close_connection = True
            return self._send(413 if length > MAX_BODY else 400,
                              {"error": "invalid request size"})
        raw = self.rfile.read(length)
        # Публичный доступ проверяет общий inspector; адаптер слушает loopback.
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._send(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "Parse error"}})
        if isinstance(value, list):
            if not value:
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32600, "message": "Invalid Request"}})
            replies = [reply for reply in (dispatch(item) for item in value) if reply is not None]
            return self._send(200 if replies else 202, replies if replies else None)
        reply = dispatch(value)
        return self._send(200 if reply is not None else 202, reply)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port=8780, host="127.0.0.1"):
    server = Server((host, int(port)), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.4},
                              name="stabilitymatrix-mcp", daemon=True)
    thread.start()
    return server, thread


_adapter_lock = threading.RLock()
_adapter = None


def start_adapter():
    """Запускает один loopback-адаптер; сам ComfyUI стартует по требованию."""
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            try:
                _adapter, _thread = serve(int(config.load().get("stabilityMatrixPort") or 8780))
            except OSError as exc:
                raise ValueError("Не удалось открыть порт адаптера ComfyUI: %s" % exc)
        return _adapter


def stop_adapter():
    """Останавливает MCP-адаптер, сохраняя работающий ComfyUI и его задания."""
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            return False
        _adapter.shutdown()
        _adapter.server_close()
        _adapter = None
        return True
