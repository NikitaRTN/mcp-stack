//! Small, explicit configuration for mixed local stdio and remote HTTP services.
use crate::{managed, process::ProcessSpec, server::RouteSpec};
use serde::Deserialize;
use serde_json::json;
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    io::Read,
    path::{Path, PathBuf},
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    services: Vec<Service>,
}
#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum Service {
    Http {
        id: String,
        url: String,
        token_env: Option<String>,
    },
    Stdio {
        id: String,
        command: String,
        #[serde(default)]
        args: Vec<String>,
        cwd: Option<PathBuf>,
        #[serde(default)]
        env: BTreeMap<String, String>,
    },
}
/// Reads trusted commands without executing them. root is the configuration directory.
pub fn read_config(
    reader: impl Read,
    root: &Path,
) -> Result<(Vec<RouteSpec>, HashMap<String, ProcessSpec>), String> {
    if !root.is_absolute() {
        return Err("config directory must be absolute".into());
    }
    let mut bytes = Vec::new();
    reader
        .take(1_000_001)
        .read_to_end(&mut bytes)
        .map_err(|_| "cannot read hub config")?;
    if bytes.len() > 1_000_000 {
        return Err("hub config too large".into());
    }
    let config: Config = serde_json::from_slice(&bytes).map_err(|_| "invalid hub config")?;
    if config.services.is_empty() || config.services.len() > 100 {
        return Err("invalid number of services".into());
    }
    let mut routes = Vec::new();
    let mut processes = HashMap::new();
    let mut ids = HashSet::new();
    for service in config.services {
        let (id, url, token, process) = match service {
            Service::Http { id, url, token_env } => {
                let token = match token_env {
                    Some(name) => {
                        if name.is_empty()
                            || name.len() > 128
                            || !name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
                        {
                            return Err("invalid upstream token environment variable".into());
                        }
                        Some(
                            std::env::var(name)
                                .map_err(|_| "missing upstream token environment variable")?,
                        )
                    }
                    None => None,
                };
                (id, url, token, None)
            }
            Service::Stdio {
                id,
                command,
                args,
                cwd,
                env,
            } => {
                // Reuse the strict process validation; log paths are generated from validated IDs.
                if id.is_empty()
                    || id.len() > 128
                    || !id
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
                {
                    return Err("invalid service id".into());
                }
                let log = root.join("logs").join(format!("{id}.log"));
                let raw = json!({"services":[{"id":id,"program":command,"args":args,"cwd":cwd,"env":env,"log":log}]}).to_string();
                let mut specs = managed::read_config(raw.as_bytes())?;
                let process = specs.remove(&id).ok_or("invalid stdio service")?;
                (id, String::new(), None, Some(process))
            }
        };
        if id.is_empty()
            || id.len() > 128
            || !id
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
            || !ids.insert(id.clone())
        {
            return Err("invalid or duplicate service id".into());
        }
        let path = if id == "default" {
            "/mcp".to_owned()
        } else {
            format!("/mcp/{id}")
        };
        if let Some(process) = process {
            processes.insert(id.clone(), process);
        }
        routes.push(RouteSpec {
            id,
            path,
            upstream: url,
            upstream_token: token,
        });
    }
    Ok((routes, processes))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_and_builds_without_starting_processes() {
        let root = std::env::temp_dir();
        let parse = |v: serde_json::Value| read_config(v.to_string().as_bytes(), &root);
        let (routes, processes) = parse(json!({"services":[{"id":"default","type":"stdio","command":"not-started"},{"id":"remote","type":"http","url":"https://example.com/mcp"}]})).unwrap();
        assert_eq!(routes.len(), 2);
        assert_eq!(routes[0].path, "/mcp");
        assert_eq!(routes[1].path, "/mcp/remote");
        assert_eq!(processes.len(), 1);
        for v in [
            json!({"services":[]}),
            json!({"services":[{"id":"../bad","type":"stdio","command":"x"}]}),
            json!({"services":[{"id":"x","type":"http","url":"http://localhost"},{"id":"x","type":"http","url":"http://localhost"}]}),
            json!({"services":[{"id":"x","type":"stdio","command":"x","oops":true}]}),
        ] {
            assert!(parse(v).is_err());
        }
    }
}
