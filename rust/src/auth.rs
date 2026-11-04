//! Verification of legacy PBKDF2 password records; no login endpoint is exposed yet.
use base64::{engine::general_purpose::STANDARD, Engine};
use ring::pbkdf2::{self, PBKDF2_HMAC_SHA256};
use ring::rand::{SecureRandom, SystemRandom};
use serde_json::Value;
use std::collections::HashMap;
use std::num::NonZeroU32;
use std::sync::Mutex;
use std::time::{Duration, Instant};

const DEFAULT_ITERATIONS: u32 = 240_000;
const MAX_ITERATIONS: u32 = 2_000_000;
const MAX_PASSWORD_BYTES: usize = 1024 * 1024;

pub struct PasswordRecord {
    username: String,
    iterations: NonZeroU32,
    salt: Vec<u8>,
    digest: Vec<u8>,
}
impl PasswordRecord {
    /// Reads the `auth` object from the existing hub.json without modifying it.
    /// All failures intentionally omit input values, including credentials.
    pub fn from_legacy(auth: &Value) -> Result<Self, &'static str> {
        let auth = auth.as_object().ok_or("invalid password record")?;
        let algorithm = match auth.get("algorithm") {
            None => "pbkdf2_sha256",
            Some(value) => value.as_str().ok_or("invalid password algorithm")?,
        };
        if algorithm != "pbkdf2_sha256" {
            return Err("unsupported password algorithm");
        }
        let username = auth
            .get("username")
            .and_then(Value::as_str)
            .unwrap_or("admin");
        if username.is_empty() || username.len() > 1024 {
            return Err("invalid username");
        }
        let iterations = match auth.get("iterations") {
            Some(value) => value
                .as_u64()
                .and_then(|n| u32::try_from(n).ok())
                .ok_or("invalid iteration count")?,
            None => DEFAULT_ITERATIONS,
        };
        if iterations > MAX_ITERATIONS {
            return Err("invalid iteration count");
        }
        let iterations = NonZeroU32::new(iterations).ok_or("invalid iteration count")?;
        let decode = |name: &str| -> Result<Vec<u8>, &'static str> {
            let encoded = auth
                .get(name)
                .and_then(Value::as_str)
                .ok_or("invalid password record")?;
            if encoded.len() > 2048 {
                return Err("invalid password record");
            }
            STANDARD
                .decode(encoded)
                .map_err(|_| "invalid password record")
        };
        let salt = decode("salt")?;
        let digest = decode("hash")?;
        if salt.is_empty() || salt.len() > 1024 || digest.len() != 32 {
            return Err("invalid password record");
        }
        Ok(Self {
            username: username.to_owned(),
            iterations,
            salt,
            digest,
        })
    }
    pub fn verify(&self, username: &str, password: &str) -> bool {
        if username.len() > 1024 || password.len() > MAX_PASSWORD_BYTES {
            return false;
        }
        let valid_user = username.trim().to_lowercase() == self.username.to_lowercase();
        // Always verify the hash, even for a wrong username, to avoid a fast
        // username-existence oracle at a future HTTP login endpoint.
        let valid_password = pbkdf2::verify(
            PBKDF2_HMAC_SHA256,
            self.iterations,
            &self.salt,
            password.as_bytes(),
            &self.digest,
        )
        .is_ok();
        valid_user && valid_password
    }
}

/// Create a record compatible with the existing Python Hub format.
pub fn create_password(username: &str, password: &str) -> Result<Value, &'static str> {
    let username = username.trim();
    if username.is_empty() || username.len() > 1024 {
        return Err("invalid username");
    }
    if password.chars().count() < 8 || password.len() > MAX_PASSWORD_BYTES {
        return Err("invalid password length");
    }
    let random = SystemRandom::new();
    let mut salt = [0u8; 16];
    random
        .fill(&mut salt)
        .map_err(|_| "random generator unavailable")?;
    let mut hash = [0u8; 32];
    pbkdf2::derive(
        PBKDF2_HMAC_SHA256,
        NonZeroU32::new(DEFAULT_ITERATIONS).unwrap(),
        &salt,
        password.as_bytes(),
        &mut hash,
    );
    Ok(
        serde_json::json!({"username":username,"algorithm":"pbkdf2_sha256", "iterations":DEFAULT_ITERATIONS,
        "salt":STANDARD.encode(salt),"hash":STANDARD.encode(hash)}),
    )
}

const SESSION_TTL: Duration = Duration::from_secs(12 * 60 * 60);
const LOCKOUT: Duration = Duration::from_secs(15 * 60);
const MAX_FAILED: u32 = 6;
const MAX_SESSIONS: usize = 1024;

struct SessionState {
    sessions: HashMap<String, Instant>,
    failed: u32,
    locked_until: Option<Instant>,
}
/// Authentication core. HTTP cookies, CSRF protection and configuration writes
/// must be implemented separately before this can serve the web panel.
pub struct AuthGate {
    password: PasswordRecord,
    state: Mutex<SessionState>,
}
#[derive(Debug, PartialEq, Eq)]
pub enum LoginError {
    Invalid,
    Locked,
    Capacity,
    Unavailable,
}
impl AuthGate {
    pub fn new(password: PasswordRecord) -> Self {
        Self {
            password,
            state: Mutex::new(SessionState {
                sessions: HashMap::new(),
                failed: 0,
                locked_until: None,
            }),
        }
    }
    pub fn login(&self, username: &str, password: &str) -> Result<String, LoginError> {
        self.login_at(username, password, Instant::now())
    }
    fn login_at(&self, username: &str, password: &str, now: Instant) -> Result<String, LoginError> {
        let mut state = self.state.lock().map_err(|_| LoginError::Unavailable)?;
        if state.locked_until.is_some_and(|until| now < until) {
            return Err(LoginError::Locked);
        }
        state.locked_until = None;
        if !self.password.verify(username, password) {
            state.failed += 1;
            if state.failed >= MAX_FAILED {
                state.failed = 0;
                state.locked_until = now.checked_add(LOCKOUT);
            }
            return Err(LoginError::Invalid);
        }
        state.failed = 0;
        state.sessions.retain(|_, expiry| *expiry > now);
        if state.sessions.len() >= MAX_SESSIONS {
            return Err(LoginError::Capacity);
        }
        let mut bytes = [0u8; 32];
        SystemRandom::new()
            .fill(&mut bytes)
            .map_err(|_| LoginError::Unavailable)?;
        let token = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes);
        let expiry = now
            .checked_add(SESSION_TTL)
            .ok_or(LoginError::Unavailable)?;
        state.sessions.insert(token.clone(), expiry);
        Ok(token)
    }
    pub fn valid(&self, token: &str) -> bool {
        self.valid_at(token, Instant::now())
    }
    fn valid_at(&self, token: &str, now: Instant) -> bool {
        if token.len() != 43 {
            return false;
        }
        let Ok(mut state) = self.state.lock() else {
            return false;
        };
        match state.sessions.get(token).copied() {
            Some(expiry) if expiry > now => {
                if let Some(next) = now.checked_add(SESSION_TTL) {
                    state.sessions.insert(token.to_owned(), next);
                    true
                } else {
                    false
                }
            }
            Some(_) => {
                state.sessions.remove(token);
                false
            }
            None => false,
        }
    }
    pub fn logout(&self, token: &str) {
        if let Ok(mut state) = self.state.lock() {
            state.sessions.remove(token);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn fixture() -> Value {
        json!({"username":"admin","algorithm":"pbkdf2_sha256","iterations":240000,"salt":"AAECAwQFBgcICQoLDA0ODw==","hash":"Rg/y24OnVfYeaKw/OlE67hUv+DjeJwrhDfpRti0PwsY="})
    }
    #[test]
    fn verifies_python_legacy_pbkdf2_fixture() {
        let record = PasswordRecord::from_legacy(&fixture()).unwrap();
        assert!(record.verify("  ADMIN  ", "пример-пароль"));
        assert!(!record.verify("admin", "incorrect"));
        assert!(!record.verify("someone-else", "пример-пароль"));
    }
    #[test]
    fn rejects_invalid_and_expensive_records() {
        for iterations in [0, MAX_ITERATIONS + 1] {
            let mut v = fixture();
            v["iterations"] = json!(iterations);
            assert!(PasswordRecord::from_legacy(&v).is_err());
        }
        for field in ["salt", "hash"] {
            let mut v = fixture();
            v[field] = json!("not-base64");
            assert!(PasswordRecord::from_legacy(&v).is_err());
        }
        let mut v = fixture();
        v["algorithm"] = json!("unknown");
        assert!(PasswordRecord::from_legacy(&v).is_err());
    }
    #[test]
    fn errors_do_not_contain_input() {
        let v = json!({"salt":"SECRET-DO-NOT-PRINT", "hash":"SECRET-DO-NOT-PRINT"});
        let err = PasswordRecord::from_legacy(&v).err().unwrap();
        assert!(!err.contains("SECRET"));
    }
    #[test]
    fn new_password_records_are_compatible_and_randomly_salted() {
        let first = create_password(" admin ", "correct-horse").unwrap();
        let second = create_password("admin", "correct-horse").unwrap();
        assert_ne!(first["salt"], second["salt"]);
        assert!(PasswordRecord::from_legacy(&first)
            .unwrap()
            .verify("admin", "correct-horse"));
        assert!(create_password("admin", "short").is_err());
        assert!(create_password("", "correct-horse").is_err());
    }
    fn fast_record() -> PasswordRecord {
        let salt = b"test salt".to_vec();
        let iterations = NonZeroU32::new(10).unwrap();
        let mut digest = vec![0u8; 32];
        pbkdf2::derive(
            PBKDF2_HMAC_SHA256,
            iterations,
            &salt,
            b"correct-pass",
            &mut digest,
        );
        PasswordRecord {
            username: "admin".into(),
            salt,
            iterations,
            digest,
        }
    }
    #[test]
    fn sessions_expire_slide_and_can_be_revoked() {
        let auth = AuthGate::new(fast_record());
        let t = Instant::now();
        let token = auth.login_at("admin", "correct-pass", t).unwrap();
        assert_eq!(token.len(), 43);
        assert!(auth.valid_at(&token, t + SESSION_TTL - Duration::from_secs(1)));
        assert!(auth.valid_at(&token, t + SESSION_TTL));
        assert!(!auth.valid_at(&token, t + SESSION_TTL * 2));
        let token = auth.login_at("admin", "correct-pass", t).unwrap();
        auth.logout(&token);
        assert!(!auth.valid_at(&token, t));
    }
    #[test]
    fn failed_attempts_lock_even_a_correct_password_temporarily() {
        let auth = AuthGate::new(fast_record());
        let t = Instant::now();
        for _ in 0..MAX_FAILED {
            assert_eq!(auth.login_at("admin", "wrong", t), Err(LoginError::Invalid));
        }
        assert_eq!(
            auth.login_at(
                "admin",
                "correct-pass",
                t + LOCKOUT - Duration::from_secs(1)
            ),
            Err(LoginError::Locked)
        );
        assert!(auth.login_at("admin", "correct-pass", t + LOCKOUT).is_ok());
    }
}
