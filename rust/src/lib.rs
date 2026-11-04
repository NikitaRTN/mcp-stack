//! Streamable HTTP MCP client. This is an incremental port, not yet the Hub.
pub mod auth;
pub mod bridge;
pub mod caddy;
pub mod events;
pub mod inspector;
pub mod managed;
pub mod migration;
pub mod oauth_builtin_http;
pub mod oauth_client;
pub mod oauth_core;
pub mod oauth_introspection;
pub mod oauth_tokens;
pub mod process;
pub mod server;
pub mod stdio;
pub mod telemetry;
pub mod trace;
mod tree;

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read};
use std::time::Duration;
use url::Url;

const MAX_RESPONSE: u64 = 4_000_000;
const PROTOCOL: &str = "2025-03-26";

pub struct Client {
    agent: ureq::Agent,
    endpoint: Url,
    token: Option<String>,
    session: Option<String>,
    protocol: String,
    next_id: u64,
}

impl Client {
    pub fn new(endpoint: &str, token: Option<String>) -> Result<Self, String> {
        let endpoint = Url::parse(endpoint).map_err(|_| "Invalid MCP URL".to_owned())?;
        if !matches!(endpoint.scheme(), "http" | "https")
            || endpoint.host_str().is_none()
            || !endpoint.username().is_empty()
            || endpoint.password().is_some()
        {
            return Err("MCP URL must be HTTP(S) without embedded credentials".into());
        }
        if let Some(ref value) = token {
            if value.trim().is_empty() || value.contains(['\r', '\n']) {
                return Err("Invalid bearer token".into());
            }
        }
        // Redirects must not leak Authorization or session IDs to another host.
        let agent = ureq::AgentBuilder::new()
            .redirects(0)
            .timeout(Duration::from_secs(30))
            .build();
        Ok(Self {
            agent,
            endpoint,
            token,
            session: None,
            protocol: PROTOCOL.into(),
            next_id: 1,
        })
    }

    fn request(
        &mut self,
        method: &str,
        body: Option<Value>,
        expected: Option<u64>,
    ) -> Result<Value, String> {
        let mut req = self
            .agent
            .request(method, self.endpoint.as_str())
            .set("Accept", "application/json, text/event-stream")
            .set("MCP-Protocol-Version", &self.protocol);
        if let Some(token) = &self.token {
            req = req.set("Authorization", &format!("Bearer {token}"));
        }
        if let Some(session) = &self.session {
            req = req.set("Mcp-Session-Id", session);
        }
        let response = match body {
            Some(value) => req.send_json(value),
            None => req.call(),
        };
        let response = match response {
            Ok(response) => response,
            Err(ureq::Error::Status(status, _)) => {
                return Err(format!("MCP server returned HTTP {status}"))
            }
            Err(_) => return Err("MCP connection failed".into()),
        };
        if let Some(session) = response.header("Mcp-Session-Id") {
            if session.len() > 1024 || session.contains(['\r', '\n']) {
                return Err("Invalid MCP session ID".into());
            }
            self.session = Some(session.to_owned());
        }
        if matches!(response.status(), 202 | 204) {
            return Ok(json!({}));
        }
        let is_sse = response
            .header("Content-Type")
            .unwrap_or("")
            .to_ascii_lowercase()
            .contains("text/event-stream");
        let reader = response.into_reader().take(MAX_RESPONSE + 1);
        if is_sse {
            return read_sse(reader, expected);
        }
        let mut bytes = Vec::new();
        reader
            .take(MAX_RESPONSE + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "Failed reading MCP response")?;
        if bytes.len() as u64 > MAX_RESPONSE {
            return Err("MCP response exceeds 4 MB".into());
        }
        if bytes.is_empty() {
            return Ok(json!({}));
        }
        serde_json::from_slice(&bytes).map_err(|_| "MCP returned invalid JSON".into())
    }

    pub fn rpc(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id;
        self.next_id = self.next_id.checked_add(1).ok_or("Request ID exhausted")?;
        let response = self.request(
            "POST",
            Some(json!({"jsonrpc":"2.0","id":id,"method":method,"params":params})),
            Some(id),
        )?;
        if response.get("id") != Some(&json!(id)) {
            return Err("MCP response ID mismatch".into());
        }
        if let Some(error) = response.get("error") {
            return Err(format!(
                "MCP JSON-RPC error: {}",
                error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown")
            ));
        }
        response
            .get("result")
            .cloned()
            .ok_or_else(|| "MCP response has no result".into())
    }

    pub fn initialize(&mut self) -> Result<(), String> {
        let info = self.rpc("initialize", json!({"protocolVersion":PROTOCOL,"capabilities":{},"clientInfo":{"name":"mcp-hub-rs","version":env!("CARGO_PKG_VERSION")}}))?;
        if let Some(version) = info.get("protocolVersion").and_then(Value::as_str) {
            if version.len() > 32 || version.contains(['\r', '\n']) {
                return Err("Invalid protocol version".into());
            }
            self.protocol = version.to_owned();
        }
        self.request(
            "POST",
            Some(json!({"jsonrpc":"2.0","method":"notifications/initialized","params":{}})),
            None,
        )?;
        Ok(())
    }

    pub fn list_tools(&mut self) -> Result<Vec<Value>, String> {
        self.initialize()?;
        let mut tools = Vec::new();
        let mut cursor: Option<String> = None;
        for _ in 0..20 {
            let params = match &cursor {
                Some(v) => json!({"cursor":v}),
                None => json!({}),
            };
            let result = self.rpc("tools/list", params)?;
            let page = result
                .get("tools")
                .and_then(Value::as_array)
                .ok_or("Invalid tools/list response")?;
            tools.extend(page.iter().cloned());
            cursor = result
                .get("nextCursor")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned);
            if cursor.is_none() {
                return Ok(tools);
            }
        }
        Err("tools/list exceeded 20 pages".into())
    }

    pub fn call_tool(&mut self, name: &str, arguments: Value) -> Result<Value, String> {
        if name.is_empty() || !arguments.is_object() {
            return Err("Tool name and JSON object arguments are required".into());
        }
        self.initialize()?;
        self.rpc("tools/call", json!({"name":name,"arguments":arguments}))
    }

    pub fn close(&mut self) {
        if self.session.is_some() {
            let _ = self.request("DELETE", None, None);
            self.session = None;
        }
    }
}

fn read_sse(reader: impl Read, expected: Option<u64>) -> Result<Value, String> {
    let mut reader = BufReader::new(reader);
    let mut line = Vec::new();
    let mut data = Vec::<Vec<u8>>::new();
    let mut total = 0usize;
    loop {
        line.clear();
        let n = reader
            .read_until(b'\n', &mut line)
            .map_err(|_| "Failed reading MCP SSE")?;
        total += n;
        if total as u64 > MAX_RESPONSE {
            return Err("MCP SSE response exceeds 4 MB".into());
        }
        if n == 0 {
            break;
        }
        while matches!(line.last(), Some(b'\r' | b'\n')) {
            line.pop();
        }
        if line.is_empty() {
            if !data.is_empty() {
                let payload = data.join(&b'\n');
                data.clear();
                if let Ok(value) = serde_json::from_slice::<Value>(&payload) {
                    if expected.is_none_or(|id| value.get("id") == Some(&json!(id))) {
                        return Ok(value);
                    }
                }
            }
        } else if let Some(value) = line.strip_prefix(b"data:") {
            data.push(value.strip_prefix(b" ").unwrap_or(value).to_vec());
        }
    }
    Err("MCP SSE ended before the expected response".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_credentials_and_non_http_urls() {
        for url in [
            "http://user:pass@example.com/mcp",
            "file:///etc/passwd",
            "not a url",
        ] {
            assert!(Client::new(url, None).is_err());
        }
        assert!(Client::new("http://localhost:1/mcp", Some("bad\r\ntoken".into())).is_err());
    }
    #[test]
    fn sse_ignores_other_messages_and_handles_multiline() {
        let stream = b"data: {\"id\": 7}\n\ndata: {\"id\": 8,\ndata: \"result\":{\"ok\":true}}\n\n";
        assert_eq!(
            read_sse(&stream[..], Some(8)).unwrap()["result"]["ok"],
            true
        );
    }
    #[test]
    fn sse_requires_matching_id() {
        assert!(read_sse(&b"data: {\"id\":1}\n\n"[..], Some(2)).is_err());
    }
    #[test]
    fn sse_respects_size_limit() {
        let input = vec![b'x'; MAX_RESPONSE as usize + 1];
        assert!(read_sse(&input[..], Some(1)).unwrap_err().contains("4 MB"));
    }
}
