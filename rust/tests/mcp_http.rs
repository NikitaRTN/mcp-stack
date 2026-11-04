use mcp_hub_rs::Client;
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;

fn read_request(stream: &mut TcpStream) -> (String, Vec<u8>) {
    let mut all = Vec::new();
    let mut buf = [0u8; 4096];
    let end = loop {
        let n = stream.read(&mut buf).unwrap();
        assert!(n > 0, "unexpected EOF");
        all.extend_from_slice(&buf[..n]);
        if let Some(pos) = all.windows(4).position(|x| x == b"\r\n\r\n") {
            break pos + 4;
        }
    };
    let head = String::from_utf8(all[..end].to_vec()).unwrap();
    let length: usize = head
        .lines()
        .find_map(|s| {
            let (k, v) = s.split_once(':')?;
            if k.eq_ignore_ascii_case("content-length") {
                Some(v.trim().parse().unwrap())
            } else {
                None
            }
        })
        .unwrap_or(0);
    while all.len() - end < length {
        let n = stream.read(&mut buf).unwrap();
        assert!(n > 0);
        all.extend_from_slice(&buf[..n]);
    }
    (head, all[end..end + length].to_vec())
}

#[test]
fn initializes_pages_and_closes_a_real_session() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let worker = thread::spawn(move || {
        for index in 0..5 {
            let (mut stream, _) = listener.accept().unwrap();
            let (head, body) = read_request(&mut stream);
            let lower = head.to_ascii_lowercase();
            assert!(lower.contains("authorization: bearer test-token\r\n"));
            if index > 0 {
                assert!(lower.contains("mcp-session-id: session-123\r\n"));
            }
            if index == 4 {
                assert!(head.starts_with("DELETE "));
            } else {
                let req: Value = serde_json::from_slice(&body).unwrap();
                let method = req["method"].as_str().unwrap();
                assert_eq!(
                    method,
                    [
                        "initialize",
                        "notifications/initialized",
                        "tools/list",
                        "tools/list"
                    ][index]
                );
                if index == 3 {
                    assert_eq!(req["params"]["cursor"], "page-2");
                }
            }
            let (status, content_type, payload) = match index {
                0 => ("200 OK", "application/json", json!({"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26"}}).to_string()),
                1 | 4 => ("202 Accepted", "application/json", String::new()),
                2 => ("200 OK", "text/event-stream", "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\"}\n\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[{\"name\":\"first\"}],\"nextCursor\":\"page-2\"}}\n\n".into()),
                _ => ("200 OK", "application/json", json!({"jsonrpc":"2.0","id":3,"result":{"tools":[{"name":"second"}]}}).to_string()),
            };
            write!(stream, "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nMcp-Session-Id: session-123\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", payload.len(), payload).unwrap();
        }
    });
    let mut client = Client::new(&format!("http://{addr}/mcp"), Some("test-token".into())).unwrap();
    let tools = client.list_tools().unwrap();
    assert_eq!(tools.len(), 2);
    assert_eq!(tools[0]["name"], "first");
    assert_eq!(tools[1]["name"], "second");
    client.close();
    worker.join().unwrap();
}

#[test]
fn executes_a_tool_call_with_json_arguments() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let worker = thread::spawn(move || {
        for index in 0..4 {
            let (mut stream, _) = listener.accept().unwrap();
            let (head, body) = read_request(&mut stream);
            if index == 3 {
                assert!(head.starts_with("DELETE "));
            }
            let body = if index == 3 {
                json!({})
            } else {
                serde_json::from_slice::<Value>(&body).unwrap()
            };
            if index == 2 {
                assert_eq!(body["method"], "tools/call");
                assert_eq!(body["params"]["name"], "echo");
                assert_eq!(body["params"]["arguments"]["text"], "hello");
            }
            let payload = match index {
                0 => json!({"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26"}}).to_string(),
                2 => json!({"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"hello"}]}}).to_string(),
                _ => String::new(),
            };
            let status = if index == 1 || index == 3 {
                "202 Accepted"
            } else {
                "200 OK"
            };
            write!(stream, "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nMcp-Session-Id: 123\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", payload.len(), payload).unwrap();
        }
    });
    let mut client = Client::new(&format!("http://{addr}/mcp"), None).unwrap();
    let result = client.call_tool("echo", json!({"text":"hello"})).unwrap();
    assert_eq!(result["content"][0]["text"], "hello");
    client.close();
    worker.join().unwrap();
}

#[test]
fn does_not_follow_redirects_with_credentials() {
    let target = TcpListener::bind("127.0.0.1:0").unwrap();
    target.set_nonblocking(true).unwrap();
    let destination = target.local_addr().unwrap();
    let origin = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = origin.local_addr().unwrap();
    let worker = thread::spawn(move || {
        let (mut stream, _) = origin.accept().unwrap();
        let _ = read_request(&mut stream);
        write!(stream, "HTTP/1.1 302 Found\r\nLocation: http://{destination}/stolen\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
    });
    let mut client = Client::new(&format!("http://{addr}/mcp"), Some("secret".into())).unwrap();
    assert!(client.rpc("initialize", json!({})).is_err());
    worker.join().unwrap();
    assert_eq!(
        target.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}
