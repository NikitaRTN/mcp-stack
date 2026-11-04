//! Experimental loopback-only HTTP wrapper around the built-in OAuth core.
use crate::{
    auth::{AuthGate, PasswordRecord},
    oauth_core::{Authorization, Error, Exchange, OAuthCore, Settings},
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use ring::{
    hmac,
    rand::{SecureRandom, SystemRandom},
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::Read,
    path::PathBuf,
    sync::Mutex,
    time::{Duration, Instant},
};
use tiny_http::{Header, Method, Request, Response};
use url::Url;

pub struct Config {
    pub database: PathBuf,
    pub signing_key: String,
    pub auth: Value,
    pub base: String,
    pub path: String,
    pub scopes: Vec<String>,
    /// Secret shared exclusively with a trusted HTTPS reverse proxy; None means loopback only.
    pub proxy_key: Option<String>,
}
struct Pending {
    client: String,
    redirect: String,
    challenge: String,
    scopes: Vec<String>,
    state: String,
    until: Instant,
}
pub struct Builtin {
    core: OAuthCore,
    base: String,
    path: String,
    resource: String,
    scopes: Vec<String>,
    username: String,
    gate: AuthGate,
    pending: Mutex<HashMap<String, Pending>>,
    trusted_proxy: Option<(String, hmac::Key, hmac::Tag)>,
}
fn hdr(name: &str, value: &str) -> Header {
    Header::from_bytes(name, value).expect("constant header or validated value")
}
fn respond(req: Request, status: u16, value: Value) {
    let _ = req.respond(
        Response::from_string(value.to_string())
            .with_status_code(status)
            .with_header(hdr("Content-Type", "application/json"))
            .with_header(hdr("Cache-Control", "no-store"))
            .with_header(hdr("Pragma", "no-cache")),
    );
}
fn reject(req: Request, err: Error) {
    let (status, code) = match err {
        Error::InvalidClient => (400, "invalid_client"),
        Error::InvalidRedirect => (400, "invalid_redirect_uri"),
        Error::InvalidScope => (400, "invalid_scope"),
        Error::InvalidTarget => (400, "invalid_target"),
        Error::InvalidGrant => (400, "invalid_grant"),
        Error::Capacity | Error::Unavailable => (503, "temporarily_unavailable"),
        Error::InvalidRequest => (400, "invalid_request"),
    };
    respond(req, status, json!({"error":code}));
}
fn header(req: &Request, name: &'static str) -> Result<Option<String>, Error> {
    let mut found = None;
    for h in req.headers() {
        if h.field.equiv(name) {
            if found.is_some() {
                return Err(Error::InvalidRequest);
            }
            found = Some(h.value.as_str().to_owned());
        }
    }
    Ok(found)
}
fn body(req: &mut Request, content_type: &str) -> Result<Vec<u8>, Error> {
    if header(req, "Transfer-Encoding")?.is_some()
        || header(req, "Content-Length")?.is_none()
        || !header(req, "Content-Type")?
            .as_deref()
            .and_then(|s| s.split(';').next())
            .map(str::trim)
            .is_some_and(|s| s.eq_ignore_ascii_case(content_type))
    {
        return Err(Error::InvalidRequest);
    }
    let len = req.body_length().ok_or(Error::InvalidRequest)?;
    if len == 0 || len > 100_000 {
        return Err(Error::InvalidRequest);
    }
    let mut bytes = Vec::new();
    req.as_reader()
        .take(100_001)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::InvalidRequest)?;
    if bytes.len() != len {
        return Err(Error::InvalidRequest);
    }
    Ok(bytes)
}
fn form(raw: &[u8]) -> Result<HashMap<String, String>, Error> {
    if std::str::from_utf8(raw).is_err() {
        return Err(Error::InvalidRequest);
    }
    let mut map = HashMap::new();
    for (key, value) in url::form_urlencoded::parse(raw) {
        if map.len() >= 32
            || key.len() > 128
            || value.len() > 8192
            || map.insert(key.into_owned(), value.into_owned()).is_some()
        {
            return Err(Error::InvalidRequest);
        }
    }
    Ok(map)
}
fn text<'a>(map: &'a HashMap<String, String>, key: &str) -> &'a str {
    map.get(key).map(String::as_str).unwrap_or("")
}
fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}
fn nonce() -> Result<String, Error> {
    let mut b = [0u8; 32];
    SystemRandom::new()
        .fill(&mut b)
        .map_err(|_| Error::Unavailable)?;
    Ok(URL_SAFE_NO_PAD.encode(b))
}
impl Builtin {
    pub fn new(config: Config) -> Result<Self, String> {
        let url = Url::parse(&config.base).map_err(|_| "invalid OAuth base")?;
        let normalized = url.origin().ascii_serialization();
        if url.host_str().is_none()
            || url.path() != "/"
            || url.query().is_some()
            || url.fragment().is_some()
            || !url.username().is_empty()
            || url.password().is_some()
            || config.base != normalized
            || !config.path.starts_with('/')
        {
            return Err("invalid OAuth base".into());
        }
        let trusted_proxy = match (&config.proxy_key, url.scheme()) {
            (None, "http")
                if matches!(url.host_str(), Some("localhost" | "127.0.0.1"))
                    && url.port().is_some() =>
            {
                None
            }
            (Some(secret), "https")
                if (32..=1024).contains(&secret.len())
                    && secret
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"-_".contains(&c)) =>
            {
                let host = config
                    .base
                    .strip_prefix("https://")
                    .ok_or("invalid OAuth base")?
                    .to_string();
                let key = hmac::Key::generate(hmac::HMAC_SHA256, &SystemRandom::new())
                    .map_err(|_| "random generator unavailable")?;
                let tag = hmac::sign(&key, secret.as_bytes());
                Some((host, key, tag))
            }
            _ => {
                return Err("OAuth requires loopback HTTP or HTTPS with a trusted proxy key".into())
            }
        };
        let base = config.base.clone();
        let resource = format!("{base}{}", config.path);
        let username = config
            .auth
            .get("username")
            .and_then(Value::as_str)
            .unwrap_or("admin")
            .to_string();
        let password = PasswordRecord::from_legacy(&config.auth)
            .map_err(|_| "invalid OAuth owner password record")?;
        let core = OAuthCore::open(
            &config.database,
            Settings {
                resource: resource.clone(),
                scopes: config.scopes.clone(),
                signing_key: config.signing_key,
            },
        )
        .map_err(|_| "invalid OAuth core configuration")?;
        Ok(Self {
            core,
            base,
            path: config.path,
            resource,
            scopes: config.scopes,
            username,
            gate: AuthGate::new(password),
            pending: Mutex::new(HashMap::new()),
            trusted_proxy,
        })
    }
    /// A public host is accepted only when a trusted reverse proxy supplies its secret.
    pub fn public_host(&self) -> Option<&str> {
        self.trusted_proxy
            .as_ref()
            .map(|(host, _, _)| host.as_str())
    }
    pub fn trusted_proxy(&self, secret: &str) -> bool {
        self.trusted_proxy
            .as_ref()
            .is_some_and(|(_, key, tag)| hmac::verify(key, secret.as_bytes(), tag.as_ref()).is_ok())
    }
    pub fn origin(&self) -> &str {
        &self.base
    }
    fn cookie_secure(&self) -> &'static str {
        if self.trusted_proxy.is_some() {
            "; Secure"
        } else {
            ""
        }
    }
    pub fn challenge(&self, error: Option<&str>) -> String {
        let suffix = error
            .map(|v| format!(", error=\"{v}\""))
            .unwrap_or_default();
        format!("Bearer resource_metadata=\"{}/.well-known/oauth-protected-resource{}\", scope=\"{}\"{}", self.base, self.path, self.scopes.join(" "), suffix)
    }
    pub fn path(&self) -> &str {
        &self.path
    }
    pub fn validate(&self, authorization: &str) -> Result<(), Error> {
        let (scheme, token) = authorization.split_once(' ').ok_or(Error::InvalidGrant)?;
        if !scheme.eq_ignore_ascii_case("bearer")
            || token.is_empty()
            || token.bytes().any(|b| b <= 32 || b >= 127)
        {
            return Err(Error::InvalidGrant);
        }
        self.core.validate_access(token, &self.resource)
    }
    pub fn handles(&self, path: &str) -> bool {
        matches!(
            path,
            "/.well-known/oauth-authorization-server"
                | "/.well-known/openid-configuration"
                | "/oauth/register"
                | "/oauth/token"
                | "/oauth/authorize"
        ) || path == format!("/.well-known/oauth-protected-resource{}", self.path)
            || path == "/.well-known/oauth-protected-resource"
    }
    pub fn handle(&self, mut req: Request) {
        let path = req.url().split('?').next().unwrap_or("").to_owned();
        if req.method() == &Method::Get
            && matches!(
                path.as_str(),
                "/.well-known/oauth-authorization-server" | "/.well-known/openid-configuration"
            )
        {
            return respond(
                req,
                200,
                json!({"issuer":self.base,"authorization_endpoint":format!("{}/oauth/authorize",self.base),"token_endpoint":format!("{}/oauth/token",self.base),"registration_endpoint":format!("{}/oauth/register",self.base),"response_types_supported":["code"],"grant_types_supported":["authorization_code","refresh_token"],"token_endpoint_auth_methods_supported":["none"],"code_challenge_methods_supported":["S256"],"scopes_supported":self.scopes,"resource_parameter_supported":true}),
            );
        }
        if req.method() == &Method::Get
            && (path == "/.well-known/oauth-protected-resource"
                || path == format!("/.well-known/oauth-protected-resource{}", self.path))
        {
            return respond(
                req,
                200,
                json!({"resource":self.resource,"authorization_servers":[self.base],"bearer_methods_supported":["header"],"scopes_supported":self.scopes,"resource_name":"MCP Hub"}),
            );
        }
        if req.method() == &Method::Post && path == "/oauth/register" {
            let result = (|| {
                let v: Value = serde_json::from_slice(&body(&mut req, "application/json")?)
                    .map_err(|_| Error::InvalidRequest)?;
                let obj = v.as_object().ok_or(Error::InvalidRequest)?;
                let redirects: Vec<String> = serde_json::from_value(
                    obj.get("redirect_uris")
                        .cloned()
                        .ok_or(Error::InvalidRedirect)?,
                )
                .map_err(|_| Error::InvalidRedirect)?;
                if obj
                    .get("token_endpoint_auth_method")
                    .is_some_and(|v| v.as_str() != Some("none"))
                {
                    return Err(Error::InvalidRequest);
                }
                if obj
                    .get("response_types")
                    .is_some_and(|v| v != &json!(["code"]))
                {
                    return Err(Error::InvalidRequest);
                }
                let allow_refresh = if let Some(g) = obj.get("grant_types") {
                    let a = g.as_array().ok_or(Error::InvalidRequest)?;
                    if !(1..=2).contains(&a.len())
                        || !a.iter().any(|v| v == "authorization_code")
                        || a.iter()
                            .any(|v| v != "authorization_code" && v != "refresh_token")
                        || (a.len() == 2 && !a.iter().any(|v| v == "refresh_token"))
                    {
                        return Err(Error::InvalidRequest);
                    }
                    a.iter().any(|v| v == "refresh_token")
                } else {
                    true
                };
                let name = obj
                    .get("client_name")
                    .map(|v| v.as_str().ok_or(Error::InvalidRequest))
                    .transpose()?
                    .unwrap_or("MCP client");
                self.core.register(name, &redirects, allow_refresh)
            })();
            return match result {
                Ok(v) => respond(req, 201, v),
                Err(e) => reject(req, e),
            };
        }
        if req.method() == &Method::Get && path == "/oauth/authorize" {
            let result = (|| {
                let query = req.url().split_once('?').map(|(_, q)| q).unwrap_or("");
                if query.len() > 10_000 {
                    return Err(Error::InvalidRequest);
                }
                let m = form(query.as_bytes())?;
                if text(&m, "response_type") != "code"
                    || text(&m, "code_challenge_method") != "S256"
                    || text(&m, "state").len() > 2048
                {
                    return Err(Error::InvalidRequest);
                }
                let resource = if text(&m, "resource").is_empty() {
                    self.resource.as_str()
                } else {
                    text(&m, "resource")
                };
                let scopes: Vec<String> = if text(&m, "scope").is_empty() {
                    self.scopes.clone()
                } else {
                    text(&m, "scope")
                        .split_ascii_whitespace()
                        .map(str::to_owned)
                        .collect()
                };
                let auth = Authorization {
                    client_id: text(&m, "client_id"),
                    redirect_uri: text(&m, "redirect_uri"),
                    resource,
                    scopes: &scopes,
                    challenge: text(&m, "code_challenge"),
                };
                self.core.validate_authorization(&auth)?;
                let client_name = auth.client_id.to_string();
                let n = nonce()?;
                let mut pending = self.pending.lock().map_err(|_| Error::Unavailable)?;
                let now = Instant::now();
                pending.retain(|_, p| p.until > now);
                if pending.len() >= 256 {
                    return Err(Error::Capacity);
                }
                pending.insert(
                    n.clone(),
                    Pending {
                        client: auth.client_id.into(),
                        redirect: auth.redirect_uri.into(),
                        challenge: auth.challenge.into(),
                        scopes,
                        state: text(&m, "state").into(),
                        until: now + Duration::from_secs(300),
                    },
                );
                Ok((n, client_name, resource.to_string()))
            })();
            return match result {
                Ok((n, client, resource)) => {
                    let html=format!("<!doctype html><html><head><meta charset=\"utf-8\"><title>Authorize MCP client</title></head><body><h1>Authorize MCP client</h1><p>Client: {}</p><p>Resource: {}</p><p>Scopes: {}</p><form method=\"post\" action=\"/oauth/authorize\"><input type=\"hidden\" name=\"nonce\" value=\"{}\"><label>Owner password <input type=\"password\" name=\"password\" required autocomplete=\"current-password\"></label><button type=\"submit\" name=\"action\" value=\"allow\">Authorize</button><button type=\"submit\" name=\"action\" value=\"deny\" formnovalidate>Deny</button></form></body></html>",esc(&client),esc(&resource),esc(&self.scopes.join(" ")),n);
                    let cookie=format!("mcp_oauth_nonce={n}; HttpOnly; SameSite=Strict; Path=/oauth/authorize; Max-Age=300{}", self.cookie_secure());
                    let _=req.respond(Response::from_string(html).with_header(hdr("Content-Type","text/html; charset=utf-8")).with_header(hdr("Cache-Control","no-store")).with_header(hdr("Set-Cookie",&cookie)).with_header(hdr("Content-Security-Policy","default-src 'none'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; base-uri 'none'; frame-ancestors 'none'")).with_header(hdr("Referrer-Policy","no-referrer")).with_header(hdr("X-Content-Type-Options","nosniff")));
                }
                Err(e) => reject(req, e),
            };
        }
        if req.method() == &Method::Post && path == "/oauth/authorize" {
            let result = (|| {
                let m = form(&body(&mut req, "application/x-www-form-urlencoded")?)?;
                let cookie = header(&req, "Cookie")?.ok_or(Error::InvalidRequest)?;
                let n = text(&m, "nonce");
                if n.len() != 43
                    || !cookie
                        .split(';')
                        .map(str::trim)
                        .any(|p| p == format!("mcp_oauth_nonce={n}"))
                {
                    return Err(Error::InvalidRequest);
                }
                let p = self
                    .pending
                    .lock()
                    .map_err(|_| Error::Unavailable)?
                    .remove(n)
                    .ok_or(Error::InvalidRequest)?;
                if p.until <= Instant::now() {
                    return Err(Error::InvalidRequest);
                }
                let mut redirect = Url::parse(&p.redirect).map_err(|_| Error::InvalidRedirect)?;
                if text(&m, "action") == "deny" {
                    redirect
                        .query_pairs_mut()
                        .append_pair("error", "access_denied");
                } else if text(&m, "action") == "allow" {
                    let session = self
                        .gate
                        .login(&self.username, text(&m, "password"))
                        .map_err(|_| Error::InvalidRequest)?;
                    self.gate.logout(&session);
                    let code = self.core.issue_code_after_consent(&Authorization {
                        client_id: &p.client,
                        redirect_uri: &p.redirect,
                        resource: &self.resource,
                        scopes: &p.scopes,
                        challenge: &p.challenge,
                    })?;
                    redirect.query_pairs_mut().append_pair("code", &code);
                } else {
                    return Err(Error::InvalidRequest);
                }
                if !p.state.is_empty() {
                    redirect.query_pairs_mut().append_pair("state", &p.state);
                }
                Ok(redirect.to_string())
            })();
            return match result {
                Ok(location) => {
                    let _=req.respond(Response::empty(303).with_header(hdr("Location",&location)).with_header(hdr("Cache-Control","no-store")).with_header(hdr("Referrer-Policy","no-referrer")).with_header(hdr("Set-Cookie",&format!("mcp_oauth_nonce=; HttpOnly; SameSite=Strict; Path=/oauth/authorize; Max-Age=0{}",self.cookie_secure()))));
                }
                Err(e) => reject(req, e),
            };
        }
        if req.method() == &Method::Post && path == "/oauth/token" {
            let result = (|| {
                if header(&req, "Authorization")?.is_some() {
                    return Err(Error::InvalidClient);
                }
                let m = form(&body(&mut req, "application/x-www-form-urlencoded")?)?;
                let resource = if text(&m, "resource").is_empty() {
                    self.resource.as_str()
                } else {
                    text(&m, "resource")
                };
                match text(&m, "grant_type") {
                    "authorization_code" => self.core.exchange(&Exchange {
                        code: text(&m, "code"),
                        client_id: text(&m, "client_id"),
                        redirect_uri: text(&m, "redirect_uri"),
                        resource,
                        verifier: text(&m, "code_verifier"),
                    }),
                    "refresh_token" => {
                        let scope: Vec<String> = text(&m, "scope")
                            .split_ascii_whitespace()
                            .map(str::to_owned)
                            .collect();
                        self.core.refresh(
                            text(&m, "refresh_token"),
                            text(&m, "client_id"),
                            resource,
                            &scope,
                        )
                    }
                    _ => Err(Error::InvalidRequest),
                }
            })();
            return match result {
                Ok(v) => respond(req, 200, v),
                Err(e) => reject(req, e),
            };
        }
        respond(req, 405, json!({"error":"method_not_allowed"}));
    }
}
