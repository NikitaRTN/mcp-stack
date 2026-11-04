//! Experimental owned process management, not yet integrated with the Hub.
//! It never adopts or kills an arbitrary persisted PID. Not a production supervisor.
use crate::events::EventBus;
use crate::tree::ProcessTree;
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
    tree: ProcessTree,
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
            if existing.tree.poll(&mut existing.child)?.is_none() {
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
            .env_remove("MCP_HUB_TOKEN")
            .env_remove("MCP_HUB_ADMIN_TOKEN")
            .env_remove("MCP_HUB_UPSTREAM_TOKEN")
            .env_remove("MCP_HUB_OAUTH_CLIENT_SECRET")
            .env_remove("MCP_HUB_INTROSPECTION_CLIENT_SECRET")
            .env_remove("MCP_HUB_BUILTIN_SIGNING_KEY")
            .env_remove("MCP_HUB_TRUSTED_PROXY_KEY")
            .envs(&spec.env)
            .stdin(Stdio::null())
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr));
        if let Some(cwd) = &spec.cwd {
            command.current_dir(cwd);
        }
        let tree = ProcessTree::new()?;
        tree.configure(&mut command);
        let mut child = command.spawn().map_err(|_| "cannot start process")?;
        if let Err(error) = tree.attach(&child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
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
                tree,
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
        if entry.tree.poll(&mut entry.child)?.is_some() {
            children.remove(name);
            return Ok(None);
        }
        Ok(Some(Status {
            pid: entry.child.id(),
            started_at: entry.started,
        }))
    }

    /// Inspect all owned processes so exited leaders and their descendants are cleaned up.
    pub fn reap_exited(&self) -> Result<usize, &'static str> {
        let mut children = self
            .children
            .lock()
            .map_err(|_| "process manager unavailable")?;
        let mut exited = Vec::new();
        let mut failed = false;
        for (name, entry) in children.iter_mut() {
            match entry.tree.poll(&mut entry.child) {
                Ok(Some(status)) => exited.push((name.clone(), entry.child.id(), status.success())),
                Ok(None) => {}
                Err(_) => failed = true,
            }
        }
        for (name, pid, success) in &exited {
            children.remove(name);
            let _ = self.bus.publish(
                "process.exited",
                json!({"name": name, "pid": pid, "success": success}),
            );
        }
        if failed {
            return Err("cannot inspect some processes");
        }
        Ok(exited.len())
    }

    /// Terminates an owned Windows job or Unix process group, never a persisted PID.
    pub fn stop(&self, name: &str) -> Result<bool, &'static str> {
        let mut children = self
            .children
            .lock()
            .map_err(|_| "process manager unavailable")?;
        let Some(entry) = children.get_mut(name) else {
            return Ok(false);
        };
        if entry.tree.poll(&mut entry.child)?.is_some() {
            children.remove(name);
            return Ok(false);
        }
        if entry.tree.terminate(&entry.child).is_err() {
            // It may have exited between the check and termination.
            if entry.tree.poll(&mut entry.child)?.is_none() {
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
        // Best effort; Windows job handles also kill associated children on close.
        let children = self.children.get_mut().unwrap_or_else(|e| e.into_inner());
        for entry in children.values_mut() {
            match entry.tree.poll(&mut entry.child) {
                Ok(Some(_)) => {}
                Ok(None) => {
                    if entry.tree.terminate(&entry.child).is_ok() || entry.child.kill().is_ok() {
                        let _ = entry.child.wait();
                    }
                }
                Err(_) => {
                    let _ = entry.child.kill();
                }
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
        if std::env::var_os("MCP_HUB_CHILD_HELPER").is_some() {
            if std::env::var_os("MCP_HUB_CHILD_EXIT").is_none() {
                std::thread::sleep(Duration::from_secs(30));
            } else {
                std::thread::sleep(Duration::from_millis(100));
            }
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

    #[test]
    #[allow(clippy::zombie_processes)] // parent-exit deliberately orphans its test child
    fn tree_child_helper() {
        use std::io::Write;
        let Ok(role) = std::env::var("MCP_HUB_TREE_ROLE") else {
            return;
        };
        let activity = std::env::var_os("MCP_HUB_TREE_ACTIVITY").unwrap();
        if role == "leaf" {
            let mut file = OpenOptions::new()
                .create(true)
                .append(true)
                .open(activity)
                .unwrap();
            for _ in 0..500 {
                file.write_all(b"x").unwrap();
                file.flush().unwrap();
                std::thread::sleep(Duration::from_millis(20));
            }
        } else if role == "parent" || role == "parent-exit" {
            let gate = PathBuf::from(std::env::var_os("MCP_HUB_TREE_GATE").unwrap());
            let deadline = Instant::now() + Duration::from_secs(10);
            while !gate.exists() {
                assert!(Instant::now() < deadline);
                std::thread::sleep(Duration::from_millis(10));
            }
            let mut child = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "process::tests::tree_child_helper",
                    "--nocapture",
                ])
                .env("MCP_HUB_TREE_ROLE", "leaf")
                .env("MCP_HUB_TREE_ACTIVITY", &activity)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            if role == "parent-exit" {
                while std::fs::metadata(&activity).map(|m| m.len()).unwrap_or(0) < 3 {
                    assert!(Instant::now() < deadline);
                    std::thread::sleep(Duration::from_millis(10));
                }
                return;
            }
            std::thread::sleep(Duration::from_secs(12));
            let _ = child.kill();
            let _ = child.wait();
        }
    }
    fn check_tree_shutdown(mode: u8) {
        let dir = std::env::temp_dir().join(format!(
            "mcp-tree-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&dir).unwrap();
        let activity = dir.join("activity");
        let gate = dir.join("gate");
        let mut s = spec(false);
        s.args = vec![
            "--exact".into(),
            "process::tests::tree_child_helper".into(),
            "--nocapture".into(),
        ];
        s.env.insert(
            "MCP_HUB_TREE_ROLE".into(),
            if mode >= 2 { "parent-exit" } else { "parent" }.into(),
        );
        s.env
            .insert("MCP_HUB_TREE_GATE".into(), gate.as_os_str().into());
        s.env
            .insert("MCP_HUB_TREE_ACTIVITY".into(), activity.as_os_str().into());
        s.log = dir.join("parent.log");
        let manager = ProcessManager::new(EventBus::default());
        manager.start("tree", s).unwrap();
        std::fs::write(&gate, b"go").unwrap();
        let deadline = Instant::now() + Duration::from_secs(8);
        while std::fs::metadata(&activity).map(|m| m.len()).unwrap_or(0) < 3 {
            assert!(Instant::now() < deadline, "grandchild did not start");
            std::thread::sleep(Duration::from_millis(20));
        }
        if mode == 3 {
            while manager.reap_exited().unwrap() == 0 {
                assert!(Instant::now() < deadline, "monitor did not reap parent");
                std::thread::sleep(Duration::from_millis(20));
            }
            assert!(manager.status("tree").unwrap().is_none());
            drop(manager);
        } else if mode == 2 {
            while manager.status("tree").unwrap().is_some() {
                assert!(Instant::now() < deadline, "parent did not exit");
                std::thread::sleep(Duration::from_millis(20));
            }
            drop(manager);
        } else if mode == 1 {
            drop(manager);
        } else {
            assert!(manager.stop("tree").unwrap());
            drop(manager);
        }
        std::thread::sleep(Duration::from_millis(250));
        let size = std::fs::metadata(&activity).unwrap().len();
        std::thread::sleep(Duration::from_millis(400));
        assert_eq!(
            std::fs::metadata(&activity).unwrap().len(),
            size,
            "grandchild survived shutdown"
        );
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn stop_terminates_spawned_tree() {
        check_tree_shutdown(0);
    }
    #[test]
    fn drop_terminates_spawned_tree() {
        check_tree_shutdown(1);
    }
    #[test]
    #[cfg(any(windows, target_os = "linux"))]
    fn natural_parent_exit_terminates_spawned_tree() {
        check_tree_shutdown(2);
    }
    #[test]
    #[cfg(any(windows, target_os = "linux"))]
    fn monitor_reaps_exited_parent_and_terminates_tree() {
        check_tree_shutdown(3);
    }
}
