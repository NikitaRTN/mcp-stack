//! Non-destructive import of legacy configuration into an in-memory candidate.
//! The caller must not write a candidate until the complete runtime is ready.
use serde_json::Value;
use std::collections::HashSet;
use std::io::Read;

const MAX_CONFIG_BYTES: u64 = 4_000_000;
const UNSUPPORTED_BUILTINS: [&str; 2] = ["windows", "stabilitymatrix"];

pub struct ImportCandidate {
    // Intentionally no Debug/Display: configuration can contain secrets.
    config: Value,
    disabled: Vec<&'static str>,
}
impl ImportCandidate {
    /// Reads without mutating any files; unknown fields and credentials are preserved.
    pub fn read(reader: impl Read) -> Result<Self, &'static str> {
        let mut data = Vec::new();
        reader
            .take(MAX_CONFIG_BYTES + 1)
            .read_to_end(&mut data)
            .map_err(|_| "cannot read config")?;
        if data.len() as u64 > MAX_CONFIG_BYTES {
            return Err("config too large");
        }
        let mut config: Value = serde_json::from_slice(&data).map_err(|_| "invalid config JSON")?;
        let root = config.as_object_mut().ok_or("config must be an object")?;
        let mut disabled = Vec::new();
        let mut seen = HashSet::new();
        if let Some(services) = root.get_mut("services") {
            let services = services.as_array_mut().ok_or("services must be an array")?;
            for service in services {
                let object = service.as_object_mut().ok_or("invalid service")?;
                let id = object
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or("missing service id")?;
                if id.is_empty() || id.len() > 128 {
                    return Err("invalid service id");
                }
                if !seen.insert(id.to_owned()) {
                    return Err("duplicate service id");
                }
                if let Some(enabled) = object.get("enabled") {
                    if !enabled.is_boolean() {
                        return Err("invalid enabled flag");
                    }
                }
                if let Some(&excluded) = UNSUPPORTED_BUILTINS.iter().find(|&&x| x == id) {
                    object.insert("enabled".into(), Value::Bool(false));
                    disabled.push(excluded);
                }
            }
        }
        Ok(Self { config, disabled })
    }
    /// Reports only built-in IDs, never configuration values or credentials.
    pub fn disabled_builtins(&self) -> &[&'static str] {
        &self.disabled
    }
    /// Allows a future migration transaction to use the candidate, without writing it here.
    pub fn into_value(self) -> Value {
        self.config
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn preserves_non_excluded_configuration_and_disables_only_requested_builtins() {
        let input = json!({"token":"example-secret","auth":{"hash":"example-hash"},"custom":{"future":42},
            "services":[{"id":"windows","enabled":true,"command":"unchanged"},
                        {"id":"stabilitymatrix","enabled":true,"upstreamToken":"private"},
                        {"id":"dc","enabled":true,"command":"node example"}]});
        let original = input.to_string();
        let candidate = ImportCandidate::read(original.as_bytes()).unwrap();
        assert_eq!(
            candidate.disabled_builtins(),
            &["windows", "stabilitymatrix"]
        );
        let result = candidate.into_value();
        assert_eq!(result["services"][0]["enabled"], false);
        assert_eq!(result["services"][1]["enabled"], false);
        assert_eq!(result["services"][2], input["services"][2]);
        assert_eq!(result["token"], input["token"]);
        assert_eq!(result["auth"], input["auth"]);
        assert_eq!(result["custom"], input["custom"]);
        assert_eq!(result["services"][1]["upstreamToken"], "private");
        assert_eq!(original, input.to_string());
    }
    #[test]
    fn rejects_invalid_or_duplicate_services_without_echoing_values() {
        for v in [
            json!({"services":[{"id":"same"},{"id":"same"}]}),
            json!({"services":[{"id":"service", "enabled":"true"}]}),
            json!({"services":{}}),
            json!([]),
        ] {
            let err = ImportCandidate::read(v.to_string().as_bytes())
                .err()
                .unwrap();
            assert!(!err.contains("same"));
        }
        let err = ImportCandidate::read(&b"{secret"[..]).err().unwrap();
        assert!(!err.contains("secret"));
    }
    #[test]
    fn rejects_oversized_input_and_accepts_no_services() {
        let oversized = vec![b' '; MAX_CONFIG_BYTES as usize + 1];
        assert_eq!(
            ImportCandidate::read(&oversized[..]).err().unwrap(),
            "config too large"
        );
        let config = ImportCandidate::read(&b"{}"[..]).unwrap();
        assert!(config.disabled_builtins().is_empty());
    }
}
