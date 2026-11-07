//! Client for the chestcounter site's uploader API (/api/v1).
//!
//! The token goes in both `Authorization: Bearer` and `X-Api-Token`: some hosts
//! running PHP as FastCGI drop the first one before the site sees it.

use std::time::Duration;

use serde_json::{json, Map, Value as Json};

/// How the site knows this client; kept from the standalone uploader.
pub const CLIENT_NAME: &str = "EventUploader";
const TIMEOUT: Duration = Duration::from_secs(25);

#[derive(Debug, Clone, PartialEq)]
pub struct ApiError {
    pub status: u16,
    pub message: String,
    pub errors: Vec<(String, String)>,
}

impl ApiError {
    pub fn new(status: u16, message: &str) -> Self {
        ApiError { status, message: message.to_string(), errors: vec![] }
    }

    pub fn describe(&self) -> String {
        let mut text = if self.status != 0 { format!("{} (HTTP {})", self.message, self.status) } else { self.message.clone() };
        if !self.errors.is_empty() {
            text.push('\n');
            text.push_str(&self.errors.iter().take(10).map(|(_, v)| format!("- {}", v)).collect::<Vec<_>>().join("\n"));
        }
        text
    }
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

pub fn normalize_site_url(url: &str) -> String {
    let mut url = url.trim().trim_end_matches('/').to_string();
    if !url.is_empty() && !url.contains("://") {
        url = format!("https://{}", url);
    }
    for suffix in ["/api/v1", "/api"] {
        if let Some(stripped) = url.strip_suffix(suffix) {
            url = stripped.to_string();
        }
    }
    url
}

/// The site as the search uses it. The real one is `SiteClient`.
pub trait Site {
    fn search_state(&self) -> Result<Json, ApiError>;
    fn lookup(&self, result_uids: &[String]) -> Result<Map<String, Json>, ApiError>;
    fn send_catalog(&self, entries: &[Json]) -> Result<Vec<Json>, ApiError>;
    fn send_tournament(&self, payload: &Json) -> Result<Json, ApiError>;
    fn report_search(&self, report: &Json) -> Result<Json, ApiError>;
    /// How the clan did in some tournaments (POST /clan-results). A site from before it answers 404.
    fn send_clan_results(&self, _results: &[Json]) -> Result<Json, ApiError> {
        Err(ApiError::new(404, "Not Found"))
    }
    /// The game's Tournaments window (POST /live-tournaments).
    fn send_live(&self, _payload: &Json) -> Result<Json, ApiError> {
        Err(ApiError::new(404, "Not Found"))
    }
    /// The images the site has (name -> CDN file id) and the items it needs (GET /game-images).
    fn known_images(&self) -> Result<Json, ApiError> {
        Err(ApiError::new(404, "Not Found"))
    }
    /// Images: `[{name, file_id, png}]` (POST /game-images).
    fn send_images(&self, _images: &[Json]) -> Result<Json, ApiError> {
        Err(ApiError::new(404, "Not Found"))
    }
}

pub struct SiteClient {
    pub base: String,
    token: String,
    client_version: String,
    agent: ureq::Agent,
}

impl SiteClient {
    pub fn new(site_url: &str, token: &str, client_version: &str) -> Self {
        SiteClient { base: format!("{}/api/v1", normalize_site_url(site_url)), token: token.trim().to_string(),
                     client_version: client_version.to_string(), agent: crate::http::agent(TIMEOUT) }
    }

    fn request(&self, method: &str, path: &str, body: Option<&Json>) -> Result<Json, ApiError> {
        if self.token.is_empty() {
            return Err(ApiError::new(0, "The API token is missing."));
        }
        let request = self
            .agent
            .request(method, &format!("{}{}", self.base, path))
            .set("Authorization", &format!("Bearer {}", self.token))
            .set("X-Api-Token", &self.token)
            .set("Accept", "application/json")
            .set("User-Agent", &format!("{}/{}", CLIENT_NAME, self.client_version));
        let outcome = match body {
            Some(b) => request.send_json(b.clone()),
            None => request.call(),
        };
        let (status, text) = match outcome {
            Ok(r) => (r.status(), r.into_string().unwrap_or_default()),
            Err(ureq::Error::Status(code, r)) => (code, r.into_string().unwrap_or_default()),
            Err(ureq::Error::Transport(t)) => {
                let message = t.to_string();
                let lower = message.to_lowercase();
                if lower.contains("certificate") || lower.contains("tls") || lower.contains("ssl") {
                    return Err(ApiError::new(0, &format!("HTTPS certificate failure: {}", message)));
                }
                return Err(ApiError::new(0, &format!("Could not reach the site: {}", message)));
            }
        };
        let data: Option<Json> = serde_json::from_str(&text).ok();
        if status >= 400 {
            if let Some(Json::Object(d)) = &data {
                let message = d
                    .get("error")
                    .filter(|v| !v.is_null() && v.as_str() != Some(""))
                    .or_else(|| d.get("message").filter(|v| !v.is_null() && v.as_str() != Some("")))
                    .map(|v| v.as_str().map(String::from).unwrap_or_else(|| v.to_string()))
                    .unwrap_or_else(|| "Error".into());
                let errors = d
                    .get("errors")
                    .and_then(|e| e.as_object())
                    .map(|e| e.iter().map(|(k, v)| (k.clone(), v.as_str().map(String::from).unwrap_or_else(|| v.to_string()))).collect())
                    .unwrap_or_default();
                return Err(ApiError { status, message, errors });
            }
            return Err(ApiError::new(status, "The site answered with an error. Check the site address."));
        }
        match data {
            Some(d @ Json::Object(_)) => Ok(d),
            _ => Err(ApiError::new(status, "Unexpected answer: does the address really point to the chestcounter site?")),
        }
    }

    pub fn me(&self) -> Result<Json, ApiError> {
        self.request("GET", "/me", None)
    }

    pub fn known_tournaments(&self) -> Result<Vec<Json>, ApiError> {
        Ok(self.request("GET", "/tournaments/known", None)?.get("tournaments").and_then(|t| t.as_array().cloned()).unwrap_or_default())
    }

    /// The clan's Members list: who is in it becomes active on the site, everyone else inactive.
    pub fn send_members(&self, payload: &Json) -> Result<Json, ApiError> {
        self.request("POST", "/members", Some(payload))
    }
}

impl Site for SiteClient {
    fn search_state(&self) -> Result<Json, ApiError> {
        self.request("GET", "/searches/last", None)
    }

    fn lookup(&self, result_uids: &[String]) -> Result<Map<String, Json>, ApiError> {
        let mut found = Map::new();
        for chunk in result_uids.chunks(500) {
            let answer = self.request("POST", "/tournaments/lookup", Some(&json!({"result_uids": chunk})))?;
            if let Some(t) = answer.get("tournaments").and_then(|t| t.as_object()) {
                found.extend(t.clone());
            }
        }
        Ok(found)
    }

    fn send_catalog(&self, entries: &[Json]) -> Result<Vec<Json>, ApiError> {
        Ok(self
            .request("POST", "/tournament-catalog", Some(&json!({"entries": entries})))?
            .get("results")
            .and_then(|r| r.as_array().cloned())
            .unwrap_or_default())
    }

    fn send_tournament(&self, payload: &Json) -> Result<Json, ApiError> {
        self.request("POST", "/tournaments", Some(payload))
    }

    fn report_search(&self, report: &Json) -> Result<Json, ApiError> {
        self.request("POST", "/searches", Some(report))
    }

    fn send_clan_results(&self, results: &[Json]) -> Result<Json, ApiError> {
        self.request("POST", "/clan-results", Some(&json!({"results": results})))
    }

    fn send_live(&self, payload: &Json) -> Result<Json, ApiError> {
        self.request("POST", "/live-tournaments", Some(payload))
    }

    fn known_images(&self) -> Result<Json, ApiError> {
        self.request("GET", "/game-images", None)
    }

    fn send_images(&self, images: &[Json]) -> Result<Json, ApiError> {
        self.request("POST", "/game-images", Some(&json!({"images": images})))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    /// A one-request-at-a-time HTTP server answering from a script.
    fn serve(answers: Vec<(u16, &'static str)>) -> (String, Arc<Mutex<Vec<(String, Vec<(String, String)>, String)>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let log = seen.clone();
        std::thread::spawn(move || {
            for (status, body) in answers {
                let Ok((stream, _)) = listener.accept() else { return };
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let mut headers = Vec::new();
                let mut length = 0;
                loop {
                    let mut h = String::new();
                    reader.read_line(&mut h).unwrap();
                    let h = h.trim_end().to_string();
                    if h.is_empty() {
                        break;
                    }
                    if let Some((k, v)) = h.split_once(':') {
                        if k.eq_ignore_ascii_case("content-length") {
                            length = v.trim().parse().unwrap_or(0);
                        }
                        headers.push((k.to_lowercase(), v.trim().to_string()));
                    }
                }
                let mut body_in = vec![0; length];
                reader.read_exact(&mut body_in).unwrap();
                log.lock().unwrap().push((line.trim().to_string(), headers, String::from_utf8_lossy(&body_in).into_owned()));
                let mut s = stream;
                write!(s, "HTTP/1.1 {} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                       status, body.len(), body).unwrap();
            }
        });
        (format!("127.0.0.1:{}", port), seen)
    }

    #[test]
    fn sends_the_token_in_both_headers() {
        let (addr, seen) = serve(vec![(200, r#"{"user": {"name": "Admin"}}"#)]);
        let client = SiteClient::new(&format!("http://{}/api/v1/", addr), "cct_x", "1.1.0");
        assert_eq!("Admin", client.me().unwrap()["user"]["name"]);
        let (line, headers, _) = seen.lock().unwrap()[0].clone();
        assert_eq!("GET /api/v1/me HTTP/1.1", line);
        assert!(headers.contains(&("authorization".into(), "Bearer cct_x".into())));
        assert!(headers.contains(&("x-api-token".into(), "cct_x".into())));
        assert!(headers.contains(&("user-agent".into(), "EventUploader/1.1.0".into())));
    }

    #[test]
    fn errors_carry_status_and_reasons() {
        let (addr, _) = serve(vec![(422, r#"{"error": "Invalid ranking", "errors": {"rows": "rows are required"}}"#),
                                   (500, "<html>oops</html>")]);
        let client = SiteClient::new(&format!("http://{}", addr), "t", "x");
        let e = client.send_tournament(&json!({})).unwrap_err();
        assert_eq!((422, "Invalid ranking".to_string()), (e.status, e.message.clone()));
        assert!(e.describe().contains("- rows are required"));
        let e = client.me().unwrap_err();
        assert_eq!(500, e.status);
        assert!(e.message.contains("Check the site address"));
    }

    #[test]
    fn unreachable_site_and_missing_token() {
        assert_eq!(0, SiteClient::new("http://127.0.0.1:1", "t", "x").me().unwrap_err().status);
        assert_eq!("The API token is missing.", SiteClient::new("http://x", " ", "x").me().unwrap_err().message);
    }

    #[test]
    fn site_url_is_normalised() {
        assert_eq!("https://clan.example.com", normalize_site_url(" clan.example.com/api/v1/ "));
        assert_eq!("http://x.org", normalize_site_url("http://x.org/api"));
        assert_eq!("", normalize_site_url(""));
    }
}
