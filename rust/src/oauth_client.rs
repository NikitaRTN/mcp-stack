//! Experimental OAuth 2.0 client-credentials support for upstream services.
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::Value;
use std::{
    io::Read,
    net::IpAddr,
    sync::Mutex,
    time::{Duration, Instant},
};
use url::{form_urlencoded, Url};

pub struct Settings {
    pub token_url: String,
    pub client_id: String,
    pub client_secret: String,
    pub scope: Option<String>,
    pub audience: Option<String>,
    pub auth_method: String,
}
struct Cached {
    token: String,
    until: Instant,
}
pub struct ClientCredentials {
    settings: Settings,
    agent: ureq::Agent,
    cache: Mutex<Option<Cached>>,
}
fn loopback(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost")
        || host.parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback())
}
fn valid_endpoint(raw: &str) -> bool {
    Url::parse(raw).is_ok_and(|url| {
        matches!(url.scheme(), "http" | "https")
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.fragment().is_none()
            && (url.scheme() == "https" || url.host_str().is_some_and(loopback))
    })
}
fn valid_value(s: &str) -> bool {
    !s.is_empty() && s.len() <= 4096 && !s.chars().any(char::is_control)
}
fn basic_component(s: &str) -> String {
    form_urlencoded::byte_serialize(s.as_bytes()).collect()
}
impl ClientCredentials {
    pub fn new(settings: Settings) -> Result<Self, &'static str> {
        if !valid_endpoint(&settings.token_url)
            || !valid_value(&settings.client_id)
            || !valid_value(&settings.client_secret)
            || !matches!(settings.auth_method.as_str(), "basic" | "body")
            || settings.scope.as_deref().is_some_and(|v| !valid_value(v))
            || settings
                .audience
                .as_deref()
                .is_some_and(|v| !valid_value(v))
        {
            return Err("invalid OAuth client configuration");
        }
        Ok(Self {
            settings,
            agent: ureq::AgentBuilder::new()
                .redirects(0)
                .timeout(Duration::from_secs(12))
                .build(),
            cache: Mutex::new(None),
        })
    }
    pub fn token(&self) -> Result<String, &'static str> {
        let mut cache = self.cache.lock().map_err(|_| "OAuth cache unavailable")?;
        if let Some(cached) = cache.as_ref().filter(|entry| Instant::now() < entry.until) {
            return Ok(cached.token.clone());
        }
        let mut fields = vec![("grant_type", "client_credentials")];
        if let Some(scope) = &self.settings.scope {
            fields.push(("scope", scope));
        }
        if let Some(audience) = &self.settings.audience {
            fields.push(("audience", audience));
        }
        let mut request = self
            .agent
            .post(&self.settings.token_url)
            .set("Accept", "application/json");
        if self.settings.auth_method == "body" {
            fields.push(("client_id", &self.settings.client_id));
            fields.push(("client_secret", &self.settings.client_secret));
        } else {
            let raw = format!(
                "{}:{}",
                basic_component(&self.settings.client_id),
                basic_component(&self.settings.client_secret)
            );
            request = request.set("Authorization", &format!("Basic {}", STANDARD.encode(raw)));
        }
        let requested_at = Instant::now();
        let response = request
            .send_form(&fields)
            .map_err(|_| "OAuth token request failed")?;
        if !(200..300).contains(&response.status()) {
            return Err("OAuth token request failed");
        }
        let mut body = Vec::new();
        response
            .into_reader()
            .take(1_000_001)
            .read_to_end(&mut body)
            .map_err(|_| "cannot read OAuth response")?;
        if body.len() > 1_000_000 {
            return Err("OAuth response too large");
        }
        let value: Value = serde_json::from_slice(&body).map_err(|_| "invalid OAuth response")?;
        let token = value
            .get("access_token")
            .and_then(Value::as_str)
            .filter(|v| {
                !v.is_empty() && v.len() <= 4096 && v.bytes().all(|b| (0x21..=0x7e).contains(&b))
            })
            .ok_or("invalid OAuth access token")?;
        if !value
            .get("token_type")
            .and_then(Value::as_str)
            .is_some_and(|v| v.eq_ignore_ascii_case("bearer"))
        {
            return Err("unsupported OAuth token type");
        }
        let ttl = value
            .get("expires_in")
            .and_then(Value::as_u64)
            .unwrap_or(0)
            .min(86400);
        let usable = ttl.saturating_sub((ttl / 10).clamp(1, 30));
        if usable > 0 {
            *cache = Some(Cached {
                token: token.into(),
                until: requested_at + Duration::from_secs(usable),
            });
        } else {
            *cache = None;
        }
        Ok(token.into())
    }
}
