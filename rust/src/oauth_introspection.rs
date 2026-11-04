//! Experimental inbound RFC 7662 validation. External responses are never forwarded to clients.
use crate::oauth_client::{basic_component, valid_endpoint, valid_value};
use base64::{engine::general_purpose::STANDARD, Engine};
use ring::digest::{digest, SHA256};
use serde_json::Value;
use std::{
    collections::HashMap,
    io::Read,
    sync::Mutex,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

pub struct Settings {
    pub endpoint: String,
    pub client_id: String,
    pub client_secret: String,
    pub auth_method: String,
    pub required_scopes: Vec<String>,
    pub audience: Option<String>,
    pub issuer: Option<String>,
}
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum Denied {
    Invalid,
    Scope,
    Unavailable,
}
struct Cached {
    until: Instant,
    expires: u64,
}
pub struct Introspection {
    settings: Settings,
    agent: ureq::Agent,
    cache: Mutex<HashMap<[u8; 32], Cached>>,
}
fn now() -> Result<u64, Denied> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|v| v.as_secs())
        .map_err(|_| Denied::Unavailable)
}
fn scope(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value
            .bytes()
            .all(|c| c == 0x21 || (0x23..=0x5b).contains(&c) || (0x5d..=0x7e).contains(&c))
}
impl Introspection {
    pub fn new(settings: Settings) -> Result<Self, &'static str> {
        if !valid_endpoint(&settings.endpoint)
            || !valid_value(&settings.client_id)
            || !valid_value(&settings.client_secret)
            || !matches!(settings.auth_method.as_str(), "basic" | "body")
            || settings.required_scopes.len() > 64
            || settings.required_scopes.iter().any(|v| !scope(v))
            || settings
                .audience
                .as_deref()
                .is_some_and(|s| !valid_value(s))
            || settings.issuer.as_deref().is_some_and(|s| !valid_value(s))
        {
            return Err("invalid inbound OAuth introspection configuration");
        }
        Ok(Self {
            settings,
            agent: ureq::AgentBuilder::new()
                .redirects(0)
                .timeout(Duration::from_secs(8))
                .build(),
            cache: Mutex::new(HashMap::new()),
        })
    }
    pub fn validate(&self, authorization: &str) -> Result<(), Denied> {
        let (scheme, token) = authorization.split_once(' ').ok_or(Denied::Invalid)?;
        if !scheme.eq_ignore_ascii_case("bearer")
            || token.is_empty()
            || token.len() > 4096
            || !token
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"-._~+/=".contains(&c))
        {
            return Err(Denied::Invalid);
        }
        let mut key = [0u8; 32];
        key.copy_from_slice(digest(&SHA256, token.as_bytes()).as_ref());
        let mut cache = self.cache.lock().map_err(|_| Denied::Unavailable)?;
        let time = now()?;
        if cache
            .get(&key)
            .is_some_and(|v| Instant::now() < v.until && time < v.expires)
        {
            return Ok(());
        }
        cache.retain(|_, v| Instant::now() < v.until && time < v.expires);
        let mut fields = vec![("token", token), ("token_type_hint", "access_token")];
        let mut request = self
            .agent
            .post(&self.settings.endpoint)
            .set("Accept", "application/json");
        if self.settings.auth_method == "body" {
            fields.push(("client_id", &self.settings.client_id));
            fields.push(("client_secret", &self.settings.client_secret));
        } else {
            let credentials = format!(
                "{}:{}",
                basic_component(&self.settings.client_id),
                basic_component(&self.settings.client_secret)
            );
            request = request.set(
                "Authorization",
                &format!("Basic {}", STANDARD.encode(credentials)),
            );
        }
        let requested = Instant::now();
        let response = request
            .send_form(&fields)
            .map_err(|_| Denied::Unavailable)?;
        if !(200..300).contains(&response.status()) {
            return Err(Denied::Unavailable);
        }
        let mut body = Vec::new();
        response
            .into_reader()
            .take(1_000_001)
            .read_to_end(&mut body)
            .map_err(|_| Denied::Unavailable)?;
        if body.len() > 1_000_000 {
            return Err(Denied::Unavailable);
        }
        let value: Value = serde_json::from_slice(&body).map_err(|_| Denied::Unavailable)?;
        if !value.is_object() {
            return Err(Denied::Unavailable);
        }
        if value.get("active") != Some(&Value::Bool(true)) {
            return Err(Denied::Invalid);
        }
        let time = now()?;
        let expires = match value.get("exp") {
            Some(v) => v
                .as_u64()
                .filter(|exp| *exp > time)
                .ok_or(Denied::Invalid)?,
            None => time, // An active token without exp is accepted, but never cached.
        };
        if value
            .get("nbf")
            .is_some_and(|v| v.as_u64().is_none_or(|nbf| nbf > time))
        {
            return Err(Denied::Invalid);
        }
        if let Some(expected) = &self.settings.issuer {
            if value.get("iss").and_then(Value::as_str) != Some(expected) {
                return Err(Denied::Invalid);
            }
        }
        if let Some(expected) = &self.settings.audience {
            let matches = match value.get("aud") {
                Some(Value::String(aud)) => aud == expected,
                Some(Value::Array(aud)) => {
                    aud.iter().all(Value::is_string)
                        && aud.iter().any(|v| v.as_str() == Some(expected))
                }
                _ => false,
            };
            if !matches {
                return Err(Denied::Invalid);
            }
        }
        let granted = value.get("scope").and_then(Value::as_str).unwrap_or("");
        if !self
            .settings
            .required_scopes
            .iter()
            .all(|s| granted.split_ascii_whitespace().any(|g| g == s))
        {
            return Err(Denied::Scope);
        }
        let ttl = expires.saturating_sub(time).min(20);
        if ttl > 0 && requested.elapsed() < Duration::from_secs(ttl) {
            if cache.len() >= 256 {
                cache.clear();
            }
            cache.insert(
                key,
                Cached {
                    until: requested + Duration::from_secs(ttl),
                    expires,
                },
            );
        }
        Ok(())
    }
}
