//! Experimental direct-child process management, not yet integrated with the Hub.
//! Only direct children started by this instance are managed; it does not adopt
//! processes or kill process trees. Do not use this as a production supervisor.
use crate::events::EventBus;
use serde_json::json;
use std::collections::{BTreeMap, HashMap};
use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, PartialEq, Eq)]
pub struct ProcessSpec {
    pub program: OsString,
    pub args: Vec<OsString>,
    pub cwd: Option<PathBuf>,
    pub env: BTreeMap<OsString, OsString>,
    pub log: PathBuf,
}

struct Managed {
    spec: ProcessSpec,
    child: Child,
    started: f64,
}

#[derive(Debug, PartialEq)]
pub struct Status {
    pub pid: u32,
    pub started_at: f64,
}

pub struct ProcessManager {
    children: Mutex<HashMap<String, Managed>>,
    bus: EventBus,
}

fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}

fn open_log(path: &PathBuf) -> Result<File, &'static str> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|_| "cannot open process log")
}

impl ProcessManager {
    pub fn new(bus: EventBus) -> Self {
        Self {
            children: Mutex::new(HashMap::new()),
            bus,
        }
    }

    /// Starting an identical running process is idempotent. A changed command
    /// must be stopped explicitly; no shell is used and values are never logged.
    pub fn start(&self, name: &str, spec: ProcessSpec) -> Result<Status, &'static str> {
        if !valid_name(name) || spec.program.is_empty() {
            return Err("invalid process identity");
        }
        let mut children = self
            .children
            .lock()
            .map_err(|_| "process manager unavailable")?;
        if let Some(existing) = children.get_mut(name) {
            if existing
                .child
                .try_wait()
                .map_err(|_| "cannot inspect process")?
                .is_none()
            {
                if existing.spec != spec {
                    return Err("process configuration changed; stop before starting");
                }
                return Ok(Status {
                    pid: existing.child.id(),
                    started_at: existing.started,
                });
            }
        }
        children.remove(name);
        let stdout = open_log(&spec.log)?;
        let stderr = stdout.try_clone().map_err(|_| "cannot open process log")?;
        let mut command = Command::new(&spec.program);
        command
            .args(&spec.args)
            .envs(&spec.env)
            .stdin(Stdio::null())
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr));
        if let Some(cwd) = &spec.cwd {
            command.current_dir(cwd);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let child = command.spawn().map_err(|_| "cannot start process")?;
        let pid = child.id();
        let started = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        children.insert(
            name.into(),
            Managed {
                spec,
                child,
                started,
            },
        );
        let _ = self
            .bus
            .publish("process.started", json!({"name": name, "pid": pid}));
        Ok(Status {
            pid,
            started_at: started,
        })
    }

    /// Reaps exited direct children; it never trusts a persisted PID.
    pub fn status(&self, name: &str) -> Result<Option<Status>, &'static str> {
        let mut children = self
            .children
            .lock()
            .map_err(|_| "process manager unavailable")?;
        let Some(entry) = children.get_mut(name) else {
            return Ok(None);
        };
        if entry
            .child
            .try_wait()
            .map_err(|_| "cannot inspect process")?
            .is_some()
        {
            children.remove(name);
            return Ok(None);
        }
        Ok(Some(Status {
            pid: entry.child.id(),
            started_at: entry.started,
        }))
    }

    /// Only the direct child is terminated, never an arbitrary or persisted PID.
    pub fn stop(&self, name: &str) -> Result<bool, &'static str> {
        let mut children = self
            .children
            .lock()
            .map_err(|_| "process manager unavailable")?;
        let Some(entry) = children.get_mut(name) else {
            return Ok(false);
        };
        if entry
            .child
            .try_wait()
            .map_err(|_| "cannot inspect process")?
            .is_some()
        {
            children.remove(name);
            return Ok(false);
        }
        if entry.child.kill().is_err() {
            // It may have exited between the check and kill.
            if entry
                .child
                .try_wait()
                .map_err(|_| "cannot inspect process")?
                .is_none()
            {
                return Err("cannot stop process");
            }
            children.remove(name);
            return Ok(false);
        }
        entry.child.wait().map_err(|_| "cannot reap process")?;
        let pid = entry.child.id();
        children.remove(name);
        let _ = self
            .bus
            .publish("process.stopped", json!({"name": name, "pid": pid}));
        Ok(true)
    }
}

impl Drop for ProcessManager {
    fn drop(&mut self) {
        // Best effort: dropping the manager must not leave its direct children behind.
        let children = self.children.get_mut().unwrap_or_else(|e| e.into_inner());
        for (_, entry) in children.iter_mut() {
            if entry.child.kill().is_ok() {
                let _ = entry.child.wait();
            } else {
                let _ = entry.child.try_wait();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn child_helper() {
        if std::env::var_os("MCP_HUB_CHILD_HELPER").is_some()
            && std::env::var_os("MCP_HUB_CHILD_EXIT").is_none()
        {
            std::thread::sleep(Duration::from_secs(30));
        }
    }
    fn spec(exit: bool) -> ProcessSpec {
        let mut env = BTreeMap::new();
        env.insert("MCP_HUB_CHILD_HELPER".into(), "1".into());
        if exit {
            env.insert("MCP_HUB_CHILD_EXIT".into(), "1".into());
        }
        ProcessSpec {
            program: std::env::current_exe().unwrap().into_os_string(),
            args: vec![
                "--exact".into(),
                "process::tests::child_helper".into(),
                "--nocapture".into(),
            ],
            cwd: None,
            env,
            log: std::env::temp_dir().join(format!(
                "mcp-rust-process-{}-{}.log",
                std::process::id(),
                if exit { "exit" } else { "running" }
            )),
        }
    }
    #[test]
    fn starts_once_detects_changes_stops_and_emits_events() {
        let bus = EventBus::default();
        let (subscription, _) = bus.subscribe(0).unwrap();
        let manager = ProcessManager::new(bus);
        let s = spec(false);
        let started = manager.start("test", s.clone()).unwrap();
        assert_eq!(manager.start("test", s.clone()).unwrap(), started);
        let mut changed = s.clone();
        changed.args.push("different".into());
        assert!(manager.start("test", changed).is_err());
        assert_eq!(manager.status("test").unwrap().unwrap().pid, started.pid);
        assert!(manager.stop("test").unwrap());
        assert!(!manager.stop("test").unwrap());
        assert!(manager.status("test").unwrap().is_none());
        assert_eq!(
            subscription.receiver.try_recv().unwrap().kind,
            "process.started"
        );
        assert_eq!(
            subscription.receiver.try_recv().unwrap().kind,
            "process.stopped"
        );
        assert!(subscription.receiver.try_recv().is_err());
        let _ = std::fs::remove_file(s.log);
    }
    #[test]
    fn reaps_naturally_exited_children() {
        let manager = ProcessManager::new(EventBus::default());
        let s = spec(true);
        manager.start("exit", s.clone()).unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        while manager.status("exit").unwrap().is_some() {
            assert!(Instant::now() < deadline, "child did not exit");
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(!manager.stop("exit").unwrap());
        let _ = std::fs::remove_file(s.log);
    }
    #[test]
    fn rejects_invalid_names_without_spawning() {
        let manager = ProcessManager::new(EventBus::default());
        assert!(manager.start("../bad", spec(false)).is_err());
    }
}
