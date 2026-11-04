//! Verification of legacy PBKDF2 password records; no login endpoint is exposed yet.
use base64::{engine::general_purpose::STANDARD, Engine};
use ring::pbkdf2::{self, PBKDF2_HMAC_SHA256};
use serde_json::Value;
use std::num::NonZeroU32;

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
        let algorithm = auth
            .get("algorithm")
            .and_then(Value::as_str)
            .unwrap_or("pbkdf2_sha256");
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
        if username.trim().to_lowercase() != self.username.to_lowercase()
            || password.len() > MAX_PASSWORD_BYTES
        {
            return false;
        }
        pbkdf2::verify(
            PBKDF2_HMAC_SHA256,
            self.iterations,
            &self.salt,
            password.as_bytes(),
            &self.digest,
        )
        .is_ok()
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
}
