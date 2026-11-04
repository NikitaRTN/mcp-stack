//! Bounded, bidirectional JSON-RPC stdio transport for owned MCP processes.
use crate::{process::ProcessSpec, tree::ProcessTree};
use serde_json::Value;
use std::{
    fs::OpenOptions,
    io::{BufRead, BufReader, Read, Write},
    process::{Child, Command, Stdio},
    sync::{
        mpsc::{self, Receiver, SyncSender},
        Mutex,
    },
    thread,
    time::Duration,
};
const MAX_MESSAGE: u64 = 4_000_000;
const QUEUE_SIZE: usize = 32;

pub(crate) fn valid_message(value: &Value) -> bool {
    if let Some(items) = value.as_array() {
        return !items.is_empty() && items.iter().all(valid_single);
    }
    valid_single(value)
}
fn valid_single(value: &Value) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return false;
    }
    if object
        .get("id")
        .is_some_and(|id| !(id.is_string() || id.is_i64() || id.is_u64()))
    {
        return false;
    }
    match object.get("method") {
        Some(method) => {
            method.as_str().is_some_and(|s| !s.is_empty())
                && !object.contains_key("result")
                && !object.contains_key("error")
                && object
                    .get("params")
                    .is_none_or(|p| p.is_object() || p.is_array())
        }
        None => {
            object.contains_key("id")
                && (object.contains_key("result") ^ object.contains_key("error"))
                && object.get("error").is_none_or(|e| {
                    e.get("code").is_some_and(Value::is_i64)
                        && e.get("message").is_some_and(Value::is_string)
                })
        }
    }
}
type WriteJob = (Vec<u8>, mpsc::Sender<Result<(), &'static str>>);
/// Owns a child process, writes messages atomically and receives server messages in order.
pub struct StdioTransport {
    child: Mutex<Option<Child>>,
    tree: ProcessTree,
    input: SyncSender<WriteJob>,
    output: Mutex<Receiver<Result<Value, &'static str>>>,
}
impl StdioTransport {
    pub fn spawn(spec: &ProcessSpec) -> Result<Self, &'static str> {
        if spec.program.is_empty() {
            return Err("invalid stdio program");
        }
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&spec.log)
            .map_err(|_| "cannot open stdio log")?;
        let mut command = Command::new(&spec.program);
        command
            .args(&spec.args)
            .env_remove("MCP_HUB_TOKEN")
            .env_remove("MCP_HUB_ADMIN_TOKEN")
            .env_remove("MCP_HUB_UPSTREAM_TOKEN")
            .env_remove("MCP_HUB_OAUTH_CLIENT_SECRET")
            .env_remove("MCP_HUB_INTROSPECTION_CLIENT_SECRET")
            .env_remove("MCP_HUB_BUILTIN_SIGNING_KEY")
            .env_remove("MCP_HUB_TRUSTED_PROXY_KEY")
            .envs(&spec.env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(log));
        if let Some(cwd) = &spec.cwd {
            command.current_dir(cwd);
        }
        let tree = ProcessTree::new()?;
        tree.configure(&mut command);
        let mut child = command.spawn().map_err(|_| "cannot start stdio process")?;
        if let Err(error) = tree.attach(&child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
        let mut input = child.stdin.take().expect("piped stdin");
        let output = child.stdout.take().expect("piped stdout");
        let (sender, receiver) = mpsc::sync_channel(QUEUE_SIZE);
        thread::spawn(move || {
            let mut reader = BufReader::new(output);
            loop {
                let mut line = Vec::new();
                let result = (&mut reader)
                    .take(MAX_MESSAGE + 2)
                    .read_until(b'\n', &mut line);
                let message = match result {
                    Err(_) | Ok(0) => Err("stdio output closed"),
                    Ok(_) if line.len() as u64 > MAX_MESSAGE + 1 || line.last() != Some(&b'\n') => {
                        Err("invalid or oversized stdio message")
                    }
                    Ok(_) => serde_json::from_slice::<Value>(&line)
                        .ok()
                        .filter(valid_message)
                        .ok_or("invalid stdio JSON-RPC message"),
                };
                let done = message.is_err();
                if sender.try_send(message).is_err() || done {
                    break;
                }
            }
        });
        let (writes, pending) = mpsc::sync_channel::<WriteJob>(QUEUE_SIZE);
        thread::spawn(move || {
            while let Ok((data, ack)) = pending.recv() {
                let result = input
                    .write_all(&data)
                    .and_then(|_| input.flush())
                    .map_err(|_| "cannot write stdio message");
                let failed = result.is_err();
                let _ = ack.send(result);
                if failed {
                    break;
                }
            }
        });
        Ok(Self {
            child: Mutex::new(Some(child)),
            tree,
            input: writes,
            output: Mutex::new(receiver),
        })
    }
    pub fn send(&self, value: &Value) -> Result<(), &'static str> {
        if !valid_message(value) {
            return Err("invalid stdio JSON-RPC message");
        }
        let mut data = serde_json::to_vec(value).map_err(|_| "cannot encode stdio message")?;
        if data.len() as u64 > MAX_MESSAGE {
            return Err("stdio message too large");
        }
        data.push(b'\n');
        let (ack, result) = mpsc::channel();
        if self.input.try_send((data, ack)).is_err() {
            let _ = self.shutdown();
            return Err("stdio write queue full or closed");
        }
        match result.recv_timeout(Duration::from_secs(5)) {
            Ok(Ok(())) => Ok(()),
            _ => {
                let _ = self.shutdown();
                Err("stdio write failed or timed out")
            }
        }
    }

    pub(crate) fn poll(&self, timeout: Duration) -> Result<Option<Value>, &'static str> {
        match self
            .output
            .lock()
            .map_err(|_| "stdio output unavailable")?
            .recv_timeout(timeout)
        {
            Ok(Ok(value)) => Ok(Some(value)),
            Ok(Err(err)) => Err(err),
            Err(mpsc::RecvTimeoutError::Timeout) => Ok(None),
            Err(mpsc::RecvTimeoutError::Disconnected) => Err("stdio output closed"),
        }
    }

    pub fn receive(&self, timeout: Duration) -> Result<Value, &'static str> {
        self.output
            .lock()
            .map_err(|_| "stdio output unavailable")?
            .recv_timeout(timeout)
            .map_err(|_| "stdio receive timed out or closed")?
    }
}
impl StdioTransport {
    pub fn shutdown(&self) -> Result<(), &'static str> {
        let mut owned = self.child.lock().map_err(|_| "stdio process unavailable")?;
        let Some(child) = owned.as_mut() else {
            return Ok(());
        };
        // No other code reaps this child. Its PID/group identity remains owned until wait.
        // Terminating the tree also removes descendants whose parent already exited.
        if self.tree.terminate(child).is_err() && child.kill().is_err() {
            return Err("cannot terminate stdio process");
        }
        child.wait().map_err(|_| "cannot reap stdio process")?;
        *owned = None;
        Ok(())
    }
}
impl Drop for StdioTransport {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}
