//! Protocol parsing for future inspector telemetry. This does not alter the
//! forwarded bytes: callers must treat parse failures as observation failures.
use serde_json::Value;
const MAX_FRAME: usize = 4_000_000;

#[derive(Default)]
pub struct SseScanner {
    line: Vec<u8>,
    data: Vec<Vec<u8>>,
    frame_bytes: usize,
}
impl SseScanner {
    pub fn feed(&mut self, chunk: &[u8]) -> Result<Vec<Value>, &'static str> {
        let mut out = Vec::new();
        for &byte in chunk {
            self.frame_bytes += 1;
            if self.frame_bytes > MAX_FRAME {
                self.line.clear();
                self.data.clear();
                self.frame_bytes = 0;
                return Err("SSE frame exceeds observation limit");
            }
            if byte != b'\n' {
                self.line.push(byte);
                continue;
            }
            if self.line.last() == Some(&b'\r') {
                self.line.pop();
            }
            if self.line.is_empty() {
                if !self.data.is_empty() {
                    let bytes = self.data.join(&b'\n');
                    if let Ok(value) = serde_json::from_slice::<Value>(&bytes) {
                        out.push(value);
                    }
                    self.data.clear();
                }
                self.frame_bytes = 0;
            } else if let Some(data) = self.line.strip_prefix(b"data:") {
                self.data
                    .push(data.strip_prefix(b" ").unwrap_or(data).to_vec());
            } else if self.line == b"data" {
                self.data.push(Vec::new());
            }
            self.line.clear();
        }
        Ok(out)
    }
}

/// Extract request objects without modifying the body. Non-JSON is ignored.
pub fn requests(body: &[u8]) -> Vec<Value> {
    let Ok(value) = serde_json::from_slice::<Value>(body) else {
        return Vec::new();
    };
    let values = match value {
        Value::Array(values) => values,
        value => vec![value],
    };
    values
        .into_iter()
        .filter(|v| {
            v.is_object()
                && v.get("method")
                    .and_then(Value::as_str)
                    .is_some_and(|v| !v.is_empty())
        })
        .collect()
}

#[derive(Debug, PartialEq, Eq)]
pub enum Outcome {
    Ok,
    Error,
}
/// MCP tools may return isError inside a successful JSON-RPC envelope.
pub fn classify(response: &Value) -> Outcome {
    if response.get("error").is_some_and(Value::is_object)
        || response
            .get("result")
            .and_then(|r| r.get("isError"))
            .and_then(Value::as_bool)
            == Some(true)
    {
        Outcome::Error
    } else {
        Outcome::Ok
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn reads_split_utf8_and_multiple_events_without_changing_bytes() {
        let body =
            "data: {\"id\":1,\"text\":\"тест\"}\r\n\r\n: ping\n\ndata: {\"id\":2}\n\n".as_bytes();
        let mut scanner = SseScanner::default();
        let mut results = Vec::new();
        for byte in body {
            results.extend(scanner.feed(&[*byte]).unwrap());
        }
        assert_eq!(
            results,
            vec![json!({"id":1,"text":"тест"}), json!({"id":2})]
        );
    }
    #[test]
    fn supports_multiline_and_ignores_malformed_events() {
        let mut scanner = SseScanner::default();
        let events = scanner
            .feed(b"data: not-json\n\ndata: {\"id\":1,\ndata: \"result\":{}}\n\n")
            .unwrap();
        assert_eq!(events, vec![json!({"id":1,"result":{}})]);
    }
    #[test]
    fn bounds_observation_buffer() {
        let mut scanner = SseScanner::default();
        assert!(scanner.feed(&vec![b'x'; MAX_FRAME + 1]).is_err());
    }
    #[test]
    fn detects_jsonrpc_and_mcp_errors() {
        assert_eq!(classify(&json!({"error":{"code":-1}})), Outcome::Error);
        assert_eq!(
            classify(&json!({"result":{"isError":true}})),
            Outcome::Error
        );
        assert_eq!(classify(&json!({"result":{"isError":false}})), Outcome::Ok);
    }
    #[test]
    fn extracts_only_requests_from_batches() {
        let input =
            br#"[{"method":"tools/call","id":1},{"id":1,"result":{}},null,{"method":"ping"}]"#;
        assert_eq!(requests(input).len(), 2);
        assert!(requests(b"not json").is_empty());
    }
}
