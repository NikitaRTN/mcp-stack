//! Explicit experimental service process configuration. Loading never starts commands.
use crate::process::ProcessSpec;
use serde::Deserialize;
use std::{
    collections::{BTreeMap, HashMap},
    io::Read,
    path::PathBuf,
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    services: Vec<Service>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Service {
    id: String,
    program: String,
    #[serde(default)]
    args: Vec<String>,
    cwd: Option<PathBuf>,
    #[serde(default)]
    env: BTreeMap<String, String>,
    log: PathBuf,
}
pub fn read_config(reader: impl Read) -> Result<HashMap<String, ProcessSpec>, String> {
    let mut data = Vec::new();
    reader
        .take(1_000_001)
        .read_to_end(&mut data)
        .map_err(|_| "cannot read process config")?;
    if data.len() > 1_000_000 {
        return Err("process config too large".into());
    }
    let config: Config = serde_json::from_slice(&data).map_err(|_| "invalid process config")?;
    if config.services.len() > 100 {
        return Err("too many processes".into());
    }
    let mut result = HashMap::new();
    for s in config.services {
        if s.id.is_empty()
            || s.id.len() > 128
            || !s
                .id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
            || s.program.is_empty()
            || !s.log.is_absolute()
            || s.cwd.as_ref().is_some_and(|p| !p.is_absolute())
            || s.args.len() > 256
            || s.env.len() > 256
        {
            return Err("invalid process specification".into());
        }
        let spec = ProcessSpec {
            program: s.program.into(),
            args: s.args.into_iter().map(Into::into).collect(),
            cwd: s.cwd,
            env: s
                .env
                .into_iter()
                .map(|(k, v)| (k.into(), v.into()))
                .collect(),
            log: s.log,
        };
        if result.insert(s.id, spec).is_some() {
            return Err("duplicate process id".into());
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn rejects_invalid_or_duplicate_specs_without_launching() {
        let path = std::env::temp_dir().join("mcp-config-test.log");
        let spec = json!({"id":"test", "program":"test-program", "log":path});
        let read = |v: serde_json::Value| read_config(v.to_string().as_bytes());
        assert_eq!(read(json!({"services":[spec.clone()]})).unwrap().len(), 1);
        assert!(read(json!({"services":[spec.clone(),spec.clone()]})).is_err());
        assert!(read(json!({"services":[{"id":"../bad", "program":"x", "log":path}]})).is_err());
        assert!(
            read(json!({"services":[{"id":"test", "program":"x", "log":"relative.log"}]})).is_err()
        );
        assert!(read(
            json!({"services":[{"id":"test", "program":"x", "log":path, "unknown":true}]})
        )
        .is_err());
        assert!(read_config(&vec![b' '; 1_000_001][..]).is_err());
    }
}
