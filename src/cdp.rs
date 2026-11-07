//! The DevTools socket and the debugging endpoints of one Chrome.

use std::io::ErrorKind;
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use serde_json::Value as Json;
use tungstenite::protocol::WebSocketConfig;
use tungstenite::{Message, WebSocket};

pub type Ws = WebSocket<TcpStream>;

/// Opens a DevTools socket. No Origin header goes out, which is what Chrome
/// wants from a client that is not a page.
pub fn connect(url: &str, timeout: Duration) -> Result<Ws, String> {
    let parsed = url::Url::parse(url).map_err(|e| e.to_string())?;
    let host = parsed.host_str().ok_or("no host in the socket address")?.to_string();
    let port = parsed.port_or_known_default().unwrap_or(80);
    let addr = (host.as_str(), port)
        .to_socket_addrs()
        .map_err(|e| e.to_string())?
        .next()
        .ok_or("the socket address does not resolve")?;
    let stream = TcpStream::connect_timeout(&addr, timeout).map_err(|e| e.to_string())?;
    let _ = stream.set_nodelay(true);
    stream.set_read_timeout(Some(timeout)).map_err(|e| e.to_string())?;
    stream.set_write_timeout(Some(timeout)).map_err(|e| e.to_string())?;
    let mut config = WebSocketConfig::default();
    config.max_message_size = Some(1 << 30);
    config.max_frame_size = Some(1 << 30);
    let (ws, _) = tungstenite::client::client_with_config(url, stream, Some(config)).map_err(|e| e.to_string())?;
    Ok(ws)
}

/// One message within `wait`; Ok(None) when none came. Err when the socket is gone.
pub fn recv(ws: &mut Ws, wait: Duration) -> Result<Option<String>, String> {
    let _ = ws.get_mut().set_read_timeout(Some(wait.max(Duration::from_millis(1))));
    match ws.read() {
        Ok(Message::Text(t)) => Ok(Some(t)),
        Ok(Message::Binary(b)) => Ok(Some(String::from_utf8_lossy(&b).into_owned())),
        Ok(Message::Close(_)) => Err("the socket was closed".into()),
        Ok(_) => Ok(None),
        Err(tungstenite::Error::Io(e)) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

pub fn send(ws: &mut Ws, text: String) -> Result<(), String> {
    ws.send(Message::Text(text)).map_err(|e| e.to_string())
}

/// GET on a debugging endpoint.
pub fn http_get(host: &str, port: u16, path: &str, timeout: Duration) -> Result<(u16, String), String> {
    let url = format!("http://{}:{}{}", host, port, path);
    match crate::http::agent(timeout).get(&url).call() {
        Ok(r) => {
            let status = r.status();
            Ok((status, r.into_string().unwrap_or_default()))
        }
        Err(ureq::Error::Status(code, r)) => Ok((code, r.into_string().unwrap_or_default())),
        Err(e) => Err(e.to_string()),
    }
}

pub fn http_put(host: &str, port: u16, path: &str, timeout: Duration) -> Result<(u16, String), String> {
    let url = format!("http://{}:{}{}", host, port, path);
    match crate::http::agent(timeout).put(&url).call() {
        Ok(r) => {
            let status = r.status();
            Ok((status, r.into_string().unwrap_or_default()))
        }
        Err(ureq::Error::Status(code, r)) => Ok((code, r.into_string().unwrap_or_default())),
        Err(e) => Err(e.to_string()),
    }
}

/// The open targets (tabs, workers...) of the Chrome on this port.
pub fn targets(host: &str, port: u16) -> Vec<Json> {
    match http_get(host, port, "/json/list", Duration::from_secs(3)) {
        Ok((200, body)) => serde_json::from_str::<Json>(&body).ok().and_then(|v| v.as_array().cloned()).unwrap_or_default(),
        _ => vec![],
    }
}

/// The game's tab: the one asked for by id, else the first whose address matches.
pub fn find_game_tab(host: &str, port: u16, tab_id: Option<&str>, url_filter: &str) -> Option<Json> {
    let pages: Vec<Json> = targets(host, port).into_iter().filter(|t| t["type"] == "page").collect();
    if let Some(id) = tab_id {
        if let Some(tab) = pages.iter().find(|t| t["id"].as_str() == Some(id)) {
            return Some(tab.clone());
        }
    }
    let filter = url_filter.to_lowercase();
    pages.into_iter().find(|t| t["url"].as_str().unwrap_or("").to_lowercase().contains(&filter))
}

/// `{"id": .., "method": .., "params": .., "sessionId": ..}` as text.
pub fn command(id: u64, method: &str, params: Json, session: Option<&str>) -> String {
    let mut msg = serde_json::json!({"id": id, "method": method, "params": if params.is_null() { serde_json::json!({}) } else { params }});
    if let Some(s) = session {
        msg["sessionId"] = Json::String(s.to_string());
    }
    msg.to_string()
}
