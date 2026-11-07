//! Plain HTTP: the debugging endpoints of Chrome, the game's CDN, the clan site.

use std::io::Read;
use std::time::Duration;

pub const USER_AGENT: &str = "Mozilla/5.0";

/// One TLS provider for the whole process: two crates bring rustls with different
/// providers, and rustls refuses to pick one by itself.
pub fn ensure_crypto() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

pub fn agent(timeout: Duration) -> ureq::Agent {
    ensure_crypto();
    ureq::AgentBuilder::new()
        .timeout_connect(timeout.min(Duration::from_secs(10)))
        .timeout(timeout)
        .redirects(5)
        .build()
}

/// The body of a GET, opened when it comes compressed. Any failure is a text.
pub fn fetch_bytes(url: &str, timeout: Duration) -> Result<Vec<u8>, String> {
    let response = agent(timeout)
        .get(url)
        .set("User-Agent", USER_AGENT)
        .set("Accept-Encoding", "gzip")
        .call()
        .map_err(|e| e.to_string())?;
    let mut raw = Vec::new();
    response.into_reader().take(512 * 1024 * 1024).read_to_end(&mut raw).map_err(|e| e.to_string())?;
    Ok(crate::tbcodec::decompress(&raw))
}
