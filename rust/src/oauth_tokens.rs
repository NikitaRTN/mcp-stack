//! Experimental Python-compatible `mh1` token codec for a future OAuth server.
//! This module is not connected to HTTP routes or an authorization flow.
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use ring::{
    digest, hmac,
    rand::{SecureRandom, SystemRandom},
};
use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_TOKEN: usize = 16_384;
const ACCESS_TTL: u64 = 3_600;
const REFRESH_TTL: u64 = 30 * 24 * 3_600;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TokenKind {
    Access,
    Refresh,
}
impl TokenKind {
    fn name(self) -> &'static str {
        match self {
            Self::Access => "access",
            Self::Refresh => "refresh",
        }
    }
    fn ttl(self) -> u64 {
        match self {
            Self::Access => ACCESS_TTL,
            Self::Refresh => REFRESH_TTL,
        }
    }
}

/// The Python Hub's signed claims. Tokens and signing material must not be logged.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Claims {
    pub client_id: String,
    pub exp: u64,
    pub iat: u64,
    pub jti: String,
    pub resource: String,
    pub scope: Vec<String>,
    pub typ: String,
}
impl Claims {
    fn valid(&self) -> bool {
        !self.client_id.is_empty()
            && self.client_id.len() <= 256
            && !self.resource.is_empty()
            && self.resource.len() <= 2048
            && !self.jti.is_empty()
            && self.jti.len() <= 256
            && self.iat <= self.exp
            && self.scope.len() <= 128
            && self.scope.iter().all(|s| !s.is_empty() && s.len() <= 256)
    }
}

/// Holds only the SHA-256 derived HMAC key, as in Python's `_key()`.
/// Key provisioning, access control, rotation and persistence are the caller's responsibility.
pub struct TokenCodec {
    key: hmac::Key,
}
impl TokenCodec {
    pub fn new(signing_key: &str) -> Result<Self, &'static str> {
        if signing_key.is_empty() || signing_key.len() > 4096 {
            return Err("invalid signing key");
        }
        let key = digest::digest(&digest::SHA256, signing_key.as_bytes());
        Ok(Self {
            key: hmac::Key::new(hmac::HMAC_SHA256, key.as_ref()),
        })
    }
    pub fn issue(
        &self,
        kind: TokenKind,
        client_id: &str,
        resource: &str,
        scope: &[String],
    ) -> Result<String, &'static str> {
        self.issue_at(kind, client_id, resource, scope, now()?)
    }
    fn issue_at(
        &self,
        kind: TokenKind,
        client_id: &str,
        resource: &str,
        scope: &[String],
        time: u64,
    ) -> Result<String, &'static str> {
        let mut random = [0u8; 12];
        SystemRandom::new()
            .fill(&mut random)
            .map_err(|_| "random generator unavailable")?;
        let claims = Claims {
            client_id: client_id.into(),
            resource: resource.into(),
            scope: scope.to_vec(),
            typ: kind.name().into(),
            iat: time,
            exp: time.checked_add(kind.ttl()).ok_or("invalid time")?,
            jti: URL_SAFE_NO_PAD.encode(random),
        };
        if !claims.valid() {
            return Err("invalid token claims");
        }
        self.sign(&claims)
    }
    fn sign(&self, claims: &Claims) -> Result<String, &'static str> {
        let body =
            URL_SAFE_NO_PAD.encode(serde_json::to_vec(claims).map_err(|_| "invalid token claims")?);
        let message = format!("mh1.{body}");
        let signature = URL_SAFE_NO_PAD.encode(hmac::sign(&self.key, message.as_bytes()).as_ref());
        let token = format!("{message}.{signature}");
        if token.len() > MAX_TOKEN {
            return Err("invalid token claims");
        }
        Ok(token)
    }
    /// Signature/type/expiry only: callers must also enforce resource, scopes, client
    /// and revocation policy before granting access. This is not an auth endpoint.
    pub fn verify(&self, token: &str, kind: TokenKind) -> Result<Claims, &'static str> {
        self.verify_at(token, kind, now()?)
    }
    fn verify_at(&self, token: &str, kind: TokenKind, time: u64) -> Result<Claims, &'static str> {
        let invalid = "invalid token";
        if token.len() > MAX_TOKEN {
            return Err(invalid);
        }
        let mut parts = token.split('.');
        let (Some("mh1"), Some(body), Some(signature), None) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        else {
            return Err(invalid);
        };
        let sig = URL_SAFE_NO_PAD.decode(signature).map_err(|_| invalid)?;
        let message = format!("mh1.{body}");
        hmac::verify(&self.key, message.as_bytes(), &sig).map_err(|_| invalid)?;
        let raw = URL_SAFE_NO_PAD.decode(body).map_err(|_| invalid)?;
        let claims: Claims = serde_json::from_slice(&raw).map_err(|_| invalid)?;
        if !claims.valid() || claims.typ != kind.name() || claims.exp <= time {
            return Err(invalid);
        }
        Ok(claims)
    }
}
fn now() -> Result<u64, &'static str> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "system clock unavailable")?
        .as_secs())
}

#[cfg(test)]
mod tests {
    use super::*;
    // Produced by Python json.dumps(sort_keys=True, separators=(",", ":")) and hmac.
    const PYTHON_TOKEN: &str = "mh1.eyJjbGllbnRfaWQiOiJtY3BfdGVzdCIsImV4cCI6MjAwMDAwMDAwMCwiaWF0IjoxNzAwMDAwMDAwLCJqdGkiOiJ0ZXN0LWp0aSIsInJlc291cmNlIjoiaHR0cHM6Ly9leGFtcGxlLmNvbS9tY3AiLCJzY29wZSI6WyJtY3A6cmVhZCIsIm1jcDp3cml0ZSJdLCJ0eXAiOiJhY2Nlc3MifQ.avMUhLtYsbwarLjp804FPb_-jpRUfYkp5Klbyxm859g";
    fn codec() -> TokenCodec {
        TokenCodec::new("unit-signing-key").unwrap()
    }
    #[test]
    fn python_fixture_verifies_and_rust_signature_matches() {
        let c = codec();
        let claims = c
            .verify_at(PYTHON_TOKEN, TokenKind::Access, 1_800_000_000)
            .unwrap();
        assert_eq!(claims.resource, "https://example.com/mcp");
        assert_eq!(claims.scope, ["mcp:read", "mcp:write"]);
        assert_eq!(c.sign(&claims).unwrap(), PYTHON_TOKEN);
    }
    #[test]
    fn accepts_python_ascii_escaped_unicode_claims() {
        let token = "mh1.eyJjbGllbnRfaWQiOiJjbGllbnQiLCJleHAiOjIwMCwiaWF0IjoxMDAsImp0aSI6InRlc3QiLCJyZXNvdXJjZSI6Imh0dHBzOi8vZXhhbXBsZS5jb20vXHUwNDNmXHUwNDQzXHUwNDQyXHUwNDRjIiwic2NvcGUiOlsiXHUwNDM0XHUwNDMwXHUwNDNkXHUwNDNkXHUwNDRiXHUwNDM1Il0sInR5cCI6ImFjY2VzcyJ9.jUHOKrqMZCfhYVb7NhR2x4-jB27UQpNkrUSSpPExdrY";
        let claims = codec().verify_at(token, TokenKind::Access, 100).unwrap();
        assert_eq!(
            claims.resource,
            "https://example.com/\u{43f}\u{443}\u{442}\u{44c}"
        );
        assert_eq!(claims.scope, ["\u{434}\u{430}\u{43d}\u{43d}\u{44b}\u{435}"]);
    }
    #[test]
    fn issue_has_random_jti_expiry_and_distinct_types() {
        let c = codec();
        let scopes = vec!["mcp:tools".to_owned()];
        let a = c
            .issue_at(
                TokenKind::Access,
                "client",
                "https://example.com/mcp",
                &scopes,
                100,
            )
            .unwrap();
        let b = c
            .issue_at(
                TokenKind::Access,
                "client",
                "https://example.com/mcp",
                &scopes,
                100,
            )
            .unwrap();
        assert_ne!(a, b);
        let claims = c.verify_at(&a, TokenKind::Access, 100).unwrap();
        assert_eq!(claims.exp, 100 + ACCESS_TTL);
        assert!(c.verify_at(&a, TokenKind::Access, claims.exp).is_err());
        assert!(c.verify_at(&a, TokenKind::Refresh, 100).is_err());
        let r = c
            .issue_at(
                TokenKind::Refresh,
                "client",
                "https://example.com/mcp",
                &scopes,
                100,
            )
            .unwrap();
        assert_eq!(
            c.verify_at(&r, TokenKind::Refresh, 100).unwrap().exp,
            100 + REFRESH_TTL
        );
    }
    #[test]
    fn rejects_tampering_wrong_key_and_malformed_tokens() {
        let c = codec();
        assert!(TokenCodec::new("").is_err());
        assert!(TokenCodec::new("other")
            .unwrap()
            .verify_at(PYTHON_TOKEN, TokenKind::Access, 100)
            .is_err());
        for bad in [
            "",
            "mh1.a.b",
            "mh2.a.b",
            "mh1.a.b.c",
            "mh1...",
            &"x".repeat(MAX_TOKEN + 1),
        ] {
            assert!(c.verify_at(bad, TokenKind::Access, 100).is_err());
        }
        let mut tampered = PYTHON_TOKEN.to_owned();
        tampered.replace_range(10..11, "A");
        assert!(c.verify_at(&tampered, TokenKind::Access, 100).is_err());
        assert!(c
            .issue_at(TokenKind::Access, "", "resource", &[], 100)
            .is_err());
        assert!(c
            .issue_at(TokenKind::Access, "client", "resource", &[], u64::MAX)
            .is_err());
    }
}
