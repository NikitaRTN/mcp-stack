//! Experimental, explicitly configured OAuth core; no HTTP endpoints or consent UI yet.
//! The caller must validate the authorization request and obtain owner consent before issuing a code.
use crate::oauth_tokens::{TokenCodec, TokenKind};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use ring::{
    digest,
    rand::{SecureRandom, SystemRandom},
};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::{
    collections::HashSet,
    path::Path,
    sync::Mutex,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use url::Url;

const CODE_TTL: u64 = 300;
const MAX_CLIENTS: i64 = 200;
const MAX_CODES: i64 = 2048;
const MAX_REFRESH: i64 = 8192;
const ACCESS_TTL: u64 = 3600;

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    InvalidRequest,
    InvalidClient,
    InvalidRedirect,
    InvalidTarget,
    InvalidScope,
    InvalidGrant,
    Capacity,
    Unavailable,
}

pub struct Settings {
    pub resource: String,
    pub scopes: Vec<String>,
    pub signing_key: String,
}
pub struct Authorization<'a> {
    pub client_id: &'a str,
    pub redirect_uri: &'a str,
    pub resource: &'a str,
    pub scopes: &'a [String],
    pub challenge: &'a str,
}
pub struct Exchange<'a> {
    pub code: &'a str,
    pub client_id: &'a str,
    pub redirect_uri: &'a str,
    pub resource: &'a str,
    pub verifier: &'a str,
}
pub struct OAuthCore {
    conn: Mutex<Connection>,
    codec: TokenCodec,
    resource: String,
    scopes: Vec<String>,
}

fn now() -> Result<u64, Error> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| Error::Unavailable)?
        .as_secs())
}
fn random(size: usize) -> Result<String, Error> {
    let mut bytes = vec![0; size];
    SystemRandom::new()
        .fill(&mut bytes)
        .map_err(|_| Error::Unavailable)?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}
fn hash(value: &str) -> Vec<u8> {
    digest::digest(&digest::SHA256, value.as_bytes())
        .as_ref()
        .to_vec()
}
fn valid_scope(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 256
        && s.bytes()
            .all(|c| c == 0x21 || (0x23..=0x5b).contains(&c) || (0x5d..=0x7e).contains(&c))
}
fn scopes_valid(scopes: &[String]) -> bool {
    !scopes.is_empty()
        && scopes.len() <= 128
        && scopes.iter().all(|s| valid_scope(s))
        && scopes.iter().collect::<HashSet<_>>().len() == scopes.len()
}
fn valid_url(raw: &str) -> bool {
    if raw.len() > 2048 {
        return false;
    }
    let Ok(url) = Url::parse(raw) else {
        return false;
    };
    if url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return false;
    }
    url.scheme() == "https"
        || (url.scheme() == "http"
            && matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]")))
}
fn challenge_valid(v: &str) -> bool {
    v.len() == 43
        && URL_SAFE_NO_PAD
            .decode(v)
            .is_ok_and(|b| b.len() == 32 && URL_SAFE_NO_PAD.encode(b) == v)
}
fn verifier_valid(v: &str) -> bool {
    (43..=128).contains(&v.len())
        && v.bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"-._~".contains(&c))
}
fn db<T>(result: rusqlite::Result<T>) -> Result<T, Error> {
    result.map_err(|_| Error::Unavailable)
}

impl OAuthCore {
    /// Opens only the specified test/new DB; do not point this at a legacy installation.
    /// Protect the database and signing key using OS access controls; key is not stored here.
    pub fn open(path: &Path, settings: Settings) -> Result<Self, Error> {
        if !path.is_absolute() || !valid_url(&settings.resource) || !scopes_valid(&settings.scopes)
        {
            return Err(Error::InvalidRequest);
        }
        let codec = TokenCodec::new(&settings.signing_key).map_err(|_| Error::InvalidRequest)?;
        let conn = db(Connection::open(path))?;
        db(conn.busy_timeout(Duration::from_secs(5)))?;
        db(conn.execute_batch("PRAGMA foreign_keys=ON; CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, name TEXT NOT NULL, redirects TEXT NOT NULL, issued INTEGER NOT NULL, allow_refresh INTEGER NOT NULL DEFAULT 1); CREATE TABLE IF NOT EXISTS oauth_codes (hash BLOB PRIMARY KEY, client_id TEXT NOT NULL, redirect TEXT NOT NULL, resource TEXT NOT NULL, scopes TEXT NOT NULL, challenge TEXT NOT NULL, expires INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS oauth_refresh (hash BLOB PRIMARY KEY, client_id TEXT NOT NULL, resource TEXT NOT NULL, expires INTEGER NOT NULL);"))?;
        let has_refresh = {
            let mut stmt = db(conn.prepare("PRAGMA table_info(oauth_clients)"))?;
            let rows = db(stmt.query_map([], |r| r.get::<_, String>(1)))?;
            db(rows.collect::<rusqlite::Result<Vec<_>>>())?
                .iter()
                .any(|v| v == "allow_refresh")
        };
        if !has_refresh {
            db(conn.execute(
                "ALTER TABLE oauth_clients ADD COLUMN allow_refresh INTEGER NOT NULL DEFAULT 1",
                [],
            ))?;
        }
        Ok(Self {
            conn: Mutex::new(conn),
            codec,
            resource: settings.resource,
            scopes: settings.scopes,
        })
    }
    pub fn register(
        &self,
        name: &str,
        redirects: &[String],
        allow_refresh: bool,
    ) -> Result<Value, Error> {
        if name.len() > 120
            || redirects.is_empty()
            || redirects.len() > 10
            || redirects.iter().any(|r| !valid_url(r))
            || redirects.iter().collect::<HashSet<_>>().len() != redirects.len()
        {
            return Err(Error::InvalidRedirect);
        }
        let id = format!("mcp_{}", random(24)?);
        let timestamp = now()?;
        let conn = self.conn.lock().map_err(|_| Error::Unavailable)?;
        // A single INSERT ensures the limit is checked atomically across DB connections.
        let changes = db(conn.execute("INSERT INTO oauth_clients(client_id,name,redirects,issued,allow_refresh) SELECT ?1,?2,?3,?4,?5 WHERE (SELECT count(*) FROM oauth_clients) < ?6", params![id, name, serde_json::to_string(redirects).map_err(|_| Error::Unavailable)?, timestamp, allow_refresh, MAX_CLIENTS]))?;
        if changes != 1 {
            return Err(Error::Capacity);
        }
        let grants = if allow_refresh {
            vec!["authorization_code", "refresh_token"]
        } else {
            vec!["authorization_code"]
        };
        Ok(
            json!({"client_id":id,"client_name":name,"redirect_uris":redirects,"grant_types":grants,"response_types":["code"],"token_endpoint_auth_method":"none","client_id_issued_at":timestamp}),
        )
    }
    /// Validate before displaying consent, and again immediately before issuing a code.
    pub fn validate_authorization(&self, request: &Authorization<'_>) -> Result<(), Error> {
        if request.client_id.len() > 256 || !challenge_valid(request.challenge) {
            return Err(Error::InvalidRequest);
        }
        if request.resource != self.resource {
            return Err(Error::InvalidTarget);
        }
        if !scopes_valid(request.scopes)
            || request.scopes.iter().collect::<HashSet<_>>()
                != self.scopes.iter().collect::<HashSet<_>>()
        {
            return Err(Error::InvalidScope);
        }
        let conn = self.conn.lock().map_err(|_| Error::Unavailable)?;
        let data: Option<String> = db(conn
            .query_row(
                "SELECT redirects FROM oauth_clients WHERE client_id=?",
                [request.client_id],
                |r| r.get(0),
            )
            .optional())?;
        let redirects: Vec<String> = serde_json::from_str(&data.ok_or(Error::InvalidClient)?)
            .map_err(|_| Error::Unavailable)?;
        if !redirects.iter().any(|r| r == request.redirect_uri) {
            return Err(Error::InvalidRedirect);
        }
        Ok(())
    }
    /// Only call after explicit owner approval. Codes are hashed at rest and single-use.
    pub fn issue_code_after_consent(&self, request: &Authorization<'_>) -> Result<String, Error> {
        self.validate_authorization(request)?;
        let code = random(32)?;
        let timestamp = now()?;
        let mut conn = self.conn.lock().map_err(|_| Error::Unavailable)?;
        let tx = db(conn.transaction())?;
        db(tx.execute("DELETE FROM oauth_codes WHERE expires <= ?", [timestamp]))?;
        let changed = db(tx.execute("INSERT INTO oauth_codes(hash,client_id,redirect,resource,scopes,challenge,expires) SELECT ?1,?2,?3,?4,?5,?6,?7 WHERE (SELECT count(*) FROM oauth_codes) < ?8", params![hash(&code),request.client_id,request.redirect_uri,request.resource,serde_json::to_string(request.scopes).map_err(|_| Error::Unavailable)?,request.challenge,timestamp + CODE_TTL,MAX_CODES]))?;
        if changed != 1 {
            return Err(Error::Capacity);
        }
        db(tx.commit())?;
        Ok(code)
    }
    pub fn exchange(&self, request: &Exchange<'_>) -> Result<Value, Error> {
        if request.code.len() != 43 || request.client_id.len() > 256 {
            return Err(Error::InvalidGrant);
        }
        let mut conn = self.conn.lock().map_err(|_| Error::Unavailable)?;
        let tx = db(conn.transaction())?;
        let row: Option<(String,String,String,String,String,u64)> = db(tx.query_row("SELECT client_id,redirect,resource,scopes,challenge,expires FROM oauth_codes WHERE hash=?", [hash(request.code)], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional())?;
        // Consume even invalid attempts: guessing a verifier must not leave a usable code.
        db(tx.execute("DELETE FROM oauth_codes WHERE hash=?", [hash(request.code)]))?;
        let Some((client, redirect, resource, scope_json, challenge, expiry)) = row else {
            db(tx.commit())?;
            return Err(Error::InvalidGrant);
        };
        let good = expiry > now()?
            && client == request.client_id
            && redirect == request.redirect_uri
            && resource == request.resource
            && resource == self.resource
            && verifier_valid(request.verifier)
            && URL_SAFE_NO_PAD
                .encode(digest::digest(&digest::SHA256, request.verifier.as_bytes()).as_ref())
                == challenge;
        if !good {
            db(tx.commit())?;
            return Err(Error::InvalidGrant);
        }
        let scopes: Vec<String> =
            serde_json::from_str(&scope_json).map_err(|_| Error::Unavailable)?;
        if !scopes_valid(&scopes)
            || scopes.iter().collect::<HashSet<_>>() != self.scopes.iter().collect::<HashSet<_>>()
        {
            db(tx.commit())?;
            return Err(Error::InvalidScope);
        }
        let allowed: Option<bool> = db(tx
            .query_row(
                "SELECT allow_refresh FROM oauth_clients WHERE client_id=?",
                [&client],
                |r| r.get(0),
            )
            .optional())?;
        let Some(allow_refresh) = allowed else {
            db(tx.commit())?;
            return Err(Error::InvalidClient);
        };
        if !allow_refresh {
            let access = self
                .codec
                .issue(TokenKind::Access, &client, &resource, &scopes)
                .map_err(|_| Error::Unavailable)?;
            db(tx.commit())?;
            return Ok(
                json!({"access_token":access,"token_type":"Bearer","expires_in":ACCESS_TTL,"scope":scopes.join(" "),"resource":resource}),
            );
        }
        db(tx.execute("DELETE FROM oauth_refresh WHERE expires <= ?", [now()?]))?;
        let count: i64 = db(tx.query_row("SELECT count(*) FROM oauth_refresh", [], |r| r.get(0)))?;
        if count >= MAX_REFRESH {
            db(tx.commit())?;
            return Err(Error::Capacity);
        }
        let access = self
            .codec
            .issue(TokenKind::Access, &client, &resource, &scopes)
            .map_err(|_| Error::Unavailable)?;
        let refresh = self
            .codec
            .issue(TokenKind::Refresh, &client, &resource, &scopes)
            .map_err(|_| Error::Unavailable)?;
        let claims = self
            .codec
            .verify(&refresh, TokenKind::Refresh)
            .map_err(|_| Error::Unavailable)?;
        db(tx.execute(
            "INSERT INTO oauth_refresh(hash,client_id,resource,expires) VALUES(?,?,?,?)",
            params![hash(&refresh), client, resource, claims.exp],
        ))?;
        db(tx.commit())?;
        Ok(
            json!({"access_token":access,"token_type":"Bearer","expires_in":ACCESS_TTL,"refresh_token":refresh,"scope":scopes.join(" "),"resource":resource}),
        )
    }
    /// Rotate refresh tokens atomically. Replays fail; new scopes may only narrow.
    pub fn refresh(
        &self,
        token: &str,
        client: &str,
        resource: &str,
        scopes: &[String],
    ) -> Result<Value, Error> {
        let claims = self
            .codec
            .verify(token, TokenKind::Refresh)
            .map_err(|_| Error::InvalidGrant)?;
        if claims.client_id != client || claims.resource != resource || resource != self.resource {
            return Err(Error::InvalidGrant);
        }
        let chosen = if scopes.is_empty() {
            claims.scope.clone()
        } else {
            scopes.to_vec()
        };
        if !scopes_valid(&chosen) || chosen.iter().any(|s| !claims.scope.contains(s)) {
            return Err(Error::InvalidScope);
        }
        let mut conn = self.conn.lock().map_err(|_| Error::Unavailable)?;
        let tx = db(conn.transaction())?;
        let changed = db(tx.execute(
            "DELETE FROM oauth_refresh WHERE hash=? AND client_id=? AND resource=? AND expires>? AND EXISTS(SELECT 1 FROM oauth_clients WHERE client_id=? AND allow_refresh=1)",
            params![hash(token), client, resource, now()?, client],
        ))?;
        if changed != 1 {
            return Err(Error::InvalidGrant);
        }
        let access = self
            .codec
            .issue(TokenKind::Access, client, resource, &chosen)
            .map_err(|_| Error::Unavailable)?;
        let refresh = self
            .codec
            .issue(TokenKind::Refresh, client, resource, &chosen)
            .map_err(|_| Error::Unavailable)?;
        let new_claims = self
            .codec
            .verify(&refresh, TokenKind::Refresh)
            .map_err(|_| Error::Unavailable)?;
        db(tx.execute(
            "INSERT INTO oauth_refresh(hash,client_id,resource,expires) VALUES(?,?,?,?)",
            params![hash(&refresh), client, resource, new_claims.exp],
        ))?;
        db(tx.commit())?;
        Ok(
            json!({"access_token":access,"token_type":"Bearer","expires_in":ACCESS_TTL,"refresh_token":refresh,"scope":chosen.join(" "),"resource":resource}),
        )
    }
    pub fn validate_access(&self, token: &str, resource: &str) -> Result<(), Error> {
        let claims = self
            .codec
            .verify(token, TokenKind::Access)
            .map_err(|_| Error::InvalidGrant)?;
        if resource != self.resource || claims.resource != resource {
            return Err(Error::InvalidTarget);
        }
        if self.scopes.iter().any(|s| !claims.scope.contains(s)) {
            return Err(Error::InvalidScope);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        path::PathBuf,
        sync::{Arc, Barrier},
        thread,
    };
    const RESOURCE: &str = "https://example.com/mcp";
    const REDIRECT: &str = "https://client.example/callback";
    fn config() -> Settings {
        Settings {
            resource: RESOURCE.into(),
            scopes: vec!["mcp:tools".into(), "mcp:read".into()],
            signing_key: "test-signing-key".into(),
        }
    }
    struct Temp(PathBuf);
    impl Temp {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!("mcp-oauth-{}.db", random(16).unwrap())))
        }
    }
    impl Drop for Temp {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }
    fn verifier() -> String {
        "a".repeat(64)
    }
    fn challenge() -> String {
        URL_SAFE_NO_PAD.encode(digest::digest(&digest::SHA256, verifier().as_bytes()).as_ref())
    }
    fn client(core: &OAuthCore) -> String {
        core.register("Test", &[REDIRECT.into()], true).unwrap()["client_id"]
            .as_str()
            .unwrap()
            .into()
    }
    fn code(core: &OAuthCore, client: &str) -> String {
        core.issue_code_after_consent(&Authorization {
            client_id: client,
            redirect_uri: REDIRECT,
            resource: RESOURCE,
            scopes: &config().scopes,
            challenge: &challenge(),
        })
        .unwrap()
    }
    fn redeem(core: &OAuthCore, client: &str, code: &str, verifier: &str) -> Result<Value, Error> {
        core.exchange(&Exchange {
            code,
            client_id: client,
            redirect_uri: REDIRECT,
            resource: RESOURCE,
            verifier,
        })
    }
    #[test]
    fn full_flow_persists_hashes_and_rotates_refresh() {
        let tmp = Temp::new();
        let core = OAuthCore::open(&tmp.0, config()).unwrap();
        let client = client(&core);
        let code = code(&core, &client);
        drop(core);
        let core = OAuthCore::open(&tmp.0, config()).unwrap();
        let tokens = redeem(&core, &client, &code, &verifier()).unwrap();
        assert_eq!(
            redeem(&core, &client, &code, &verifier()),
            Err(Error::InvalidGrant)
        );
        let access = tokens["access_token"].as_str().unwrap();
        let refresh = tokens["refresh_token"].as_str().unwrap();
        assert_eq!(core.validate_access(access, RESOURCE), Ok(()));
        assert_eq!(
            core.validate_access(access, "https://example.com/other"),
            Err(Error::InvalidTarget)
        );
        assert_eq!(
            core.validate_access(refresh, RESOURCE),
            Err(Error::InvalidGrant)
        );
        assert_eq!(
            core.refresh(refresh, &client, RESOURCE, &["mcp:admin".into()]),
            Err(Error::InvalidScope)
        );
        assert_eq!(
            core.refresh(refresh, "wrong", RESOURCE, &[]),
            Err(Error::InvalidGrant)
        );
        let rotated = core
            .refresh(refresh, &client, RESOURCE, &["mcp:tools".into()])
            .unwrap();
        assert_eq!(
            core.refresh(refresh, &client, RESOURCE, &[]),
            Err(Error::InvalidGrant)
        );
        assert_eq!(
            core.validate_access(rotated["access_token"].as_str().unwrap(), RESOURCE),
            Err(Error::InvalidScope)
        );
        let next = rotated["refresh_token"].as_str().unwrap();
        drop(core);
        let bytes = std::fs::read(&tmp.0).unwrap();
        for secret in [&code, &refresh.to_owned(), &next.to_owned()] {
            assert!(!bytes.windows(secret.len()).any(|w| w == secret.as_bytes()));
        }
        let core = OAuthCore::open(&tmp.0, config()).unwrap();
        assert!(core.refresh(next, &client, RESOURCE, &[]).is_ok());
    }
    #[test]
    fn authorization_rejects_bad_inputs_and_invalid_code_attempt_consumes_it() {
        let tmp = Temp::new();
        let core = OAuthCore::open(&tmp.0, config()).unwrap();
        for uri in [
            "http://example.com/cb",
            "https://user:pass@example.com/cb",
            "https://example.com/cb#frag",
            "http://localhost.evil/cb",
            "file:///tmp/a",
        ] {
            assert!(core.register("x", &[uri.into()], true).is_err());
        }
        assert!(core
            .register("native", &["http://127.0.0.1:1234/callback".into()], true)
            .is_ok());
        let client = client(&core);
        let scopes = config().scopes;
        let mut req = Authorization {
            client_id: &client,
            redirect_uri: REDIRECT,
            resource: RESOURCE,
            scopes: &scopes,
            challenge: "bad",
        };
        assert_eq!(
            core.validate_authorization(&req),
            Err(Error::InvalidRequest)
        );
        let challenge = challenge();
        req.challenge = &challenge;
        req.redirect_uri = "https://evil.example/cb";
        assert_eq!(
            core.validate_authorization(&req),
            Err(Error::InvalidRedirect)
        );
        req.redirect_uri = REDIRECT;
        req.resource = "https://example.com/other";
        assert_eq!(core.validate_authorization(&req), Err(Error::InvalidTarget));
        req.resource = RESOURCE;
        req.scopes = &[];
        assert_eq!(core.validate_authorization(&req), Err(Error::InvalidScope));
        let code = code(&core, &client);
        assert_eq!(
            redeem(&core, &client, &code, &"b".repeat(64)),
            Err(Error::InvalidGrant)
        );
        assert_eq!(
            redeem(&core, &client, &code, &verifier()),
            Err(Error::InvalidGrant)
        );
    }
    #[test]
    fn concurrent_code_redemption_succeeds_only_once() {
        let tmp = Temp::new();
        let core = Arc::new(OAuthCore::open(&tmp.0, config()).unwrap());
        let client = client(&core);
        let code = code(&core, &client);
        let barrier = Arc::new(Barrier::new(8));
        let mut workers = Vec::new();
        for _ in 0..8 {
            let c = core.clone();
            let b = barrier.clone();
            let id = client.clone();
            let code = code.clone();
            workers.push(thread::spawn(move || {
                b.wait();
                redeem(&c, &id, &code, &verifier()).is_ok()
            }));
        }
        assert_eq!(
            workers
                .into_iter()
                .map(|w| w.join().unwrap())
                .filter(|ok| *ok)
                .count(),
            1
        );
    }
    #[test]
    fn independent_connections_cannot_replay_codes_or_refresh_tokens() {
        let tmp = Temp::new();
        let first = Arc::new(OAuthCore::open(&tmp.0, config()).unwrap());
        let second = Arc::new(OAuthCore::open(&tmp.0, config()).unwrap());
        let id = client(&first);
        let code = code(&first, &id);
        let barrier = Arc::new(Barrier::new(2));
        let mut threads = Vec::new();
        for core in [first.clone(), second.clone()] {
            let b = barrier.clone();
            let id = id.clone();
            let code = code.clone();
            threads.push(thread::spawn(move || {
                b.wait();
                redeem(&core, &id, &code, &verifier()).ok()
            }));
        }
        let tokens: Vec<Value> = threads
            .into_iter()
            .filter_map(|t| t.join().unwrap())
            .collect();
        assert_eq!(tokens.len(), 1);
        let refresh = tokens[0]["refresh_token"].as_str().unwrap().to_string();
        let barrier = Arc::new(Barrier::new(2));
        let mut threads = Vec::new();
        for core in [first, second] {
            let b = barrier.clone();
            let id = id.clone();
            let refresh = refresh.clone();
            threads.push(thread::spawn(move || {
                b.wait();
                core.refresh(&refresh, &id, RESOURCE, &[]).is_ok()
            }));
        }
        assert_eq!(
            threads
                .into_iter()
                .map(|t| t.join().unwrap())
                .filter(|v| *v)
                .count(),
            1
        );
    }
    #[test]
    fn authorization_code_only_client_receives_no_refresh_token() {
        let tmp = Temp::new();
        let core = OAuthCore::open(&tmp.0, config()).unwrap();
        let record = core
            .register("no refresh", &[REDIRECT.into()], false)
            .unwrap();
        assert_eq!(record["grant_types"], json!(["authorization_code"]));
        let id = record["client_id"].as_str().unwrap();
        let code = code(&core, id);
        let token = redeem(&core, id, &code, &verifier()).unwrap();
        assert!(token.get("refresh_token").is_none());
        assert_eq!(
            core.validate_access(token["access_token"].as_str().unwrap(), RESOURCE),
            Ok(())
        );
    }
    #[test]
    fn expired_codes_and_changed_configuration_fail_closed() {
        let tmp = Temp::new();
        let core = OAuthCore::open(&tmp.0, config()).unwrap();
        let client = client(&core);
        let first = code(&core, &client);
        core.conn
            .lock()
            .unwrap()
            .execute("UPDATE oauth_codes SET expires=0", [])
            .unwrap();
        assert_eq!(
            redeem(&core, &client, &first, &verifier()),
            Err(Error::InvalidGrant)
        );
        let next = code(&core, &client);
        drop(core);
        let mut settings = config();
        settings.scopes.push("mcp:extra".into());
        let core = OAuthCore::open(&tmp.0, settings).unwrap();
        assert_eq!(
            redeem(&core, &client, &next, &verifier()),
            Err(Error::InvalidScope)
        );
    }
}
