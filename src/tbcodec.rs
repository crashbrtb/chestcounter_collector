//! The Total Battle wire protocol.
//!
//! The game does NOT use standard MessagePack: sizes and multi-byte integers are
//! little-endian. A standard msgpack library scrambles every integer - and a
//! tournament score is precisely a large integer.
//!
//! Framing of each answer:
//!
//! ```text
//! [u32 LE total_len][u32 LE inner_len][msgpack LE ...][tail of events]
//! ```
//!
//! The first object is always `[route_id, seq]`. Route 2 is a batch: after the
//! header comes the number of sub-frames and the sub-frames concatenated. After
//! `inner_len` there may be a tail of events `[u32 len][msgpack]`.
//!
//! Map keys are kept the way the original reader kept them: as the text of the
//! key (`str(key)` in Python), so `{1: x}` is found under "1" and a key that is a
//! list, `[[1024, 1], 0]`, under "[[1024, 1], 0]".

use std::io::Read;

use indexmap::IndexMap;

pub const ROUTE_BATCH: i128 = 2;
const MAX_DEPTH: usize = 120;
pub const MAX_BLOB_DEPTH: usize = 6;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Nil,
    Bool(bool),
    Int(i128),
    Float(f64),
    Str(String),
    Bin(Vec<u8>),
    Arr(Vec<Value>),
    /// Keys as written on the wire when packing; as text when decoded.
    Map(Vec<(Value, Value)>),
}

impl Value {
    pub fn str(s: impl Into<String>) -> Value {
        Value::Str(s.into())
    }

    pub fn int(i: impl Into<i128>) -> Value {
        Value::Int(i.into())
    }

    pub fn arr(items: Vec<Value>) -> Value {
        Value::Arr(items)
    }

    pub fn map(pairs: Vec<(Value, Value)>) -> Value {
        Value::Map(pairs)
    }

    pub fn as_arr(&self) -> Option<&Vec<Value>> {
        match self {
            Value::Arr(a) => Some(a),
            _ => None,
        }
    }

    pub fn as_map(&self) -> Option<&Vec<(Value, Value)>> {
        match self {
            Value::Map(m) => Some(m),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_bin(&self) -> Option<&[u8]> {
        match self {
            Value::Bin(b) => Some(b),
            _ => None,
        }
    }

    /// An integer and not a boolean (`_is_int` in the original).
    pub fn strict_int(&self) -> Option<i128> {
        match self {
            Value::Int(i) => Some(*i),
            _ => None,
        }
    }

    /// `isinstance(x, int)`: booleans count, as 0 and 1.
    pub fn py_int(&self) -> Option<i128> {
        match self {
            Value::Int(i) => Some(*i),
            Value::Bool(b) => Some(*b as i128),
            _ => None,
        }
    }

    pub fn is_bool(&self) -> bool {
        matches!(self, Value::Bool(_))
    }

    pub fn is_list(&self) -> bool {
        matches!(self, Value::Arr(_))
    }

    pub fn is_map(&self) -> bool {
        matches!(self, Value::Map(_))
    }

    /// Item `i` of a list.
    pub fn at(&self, i: usize) -> Option<&Value> {
        self.as_arr().and_then(|a| a.get(i))
    }

    pub fn len(&self) -> usize {
        match self {
            Value::Arr(a) => a.len(),
            Value::Map(m) => m.len(),
            Value::Str(s) => s.len(),
            Value::Bin(b) => b.len(),
            _ => 0,
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// A map's value under the text of its key.
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.as_map()?.iter().rev().find(|(k, _)| key_text(k) == key).map(|(_, v)| v)
    }

    /// Python truthiness.
    pub fn truthy(&self) -> bool {
        match self {
            Value::Nil => false,
            Value::Bool(b) => *b,
            Value::Int(i) => *i != 0,
            Value::Float(f) => *f != 0.0,
            Value::Str(s) => !s.is_empty(),
            Value::Bin(b) => !b.is_empty(),
            Value::Arr(a) => !a.is_empty(),
            Value::Map(m) => !m.is_empty(),
        }
    }
}

impl From<i64> for Value {
    fn from(v: i64) -> Self {
        Value::Int(v as i128)
    }
}

impl From<&str> for Value {
    fn from(v: &str) -> Self {
        Value::Str(v.to_string())
    }
}

/// What `str(key)` gives in Python, used for map keys.
pub fn key_text(v: &Value) -> String {
    match v {
        Value::Str(s) => s.clone(),
        other => py_repr(other),
    }
}

/// Python's `repr`, for the shapes the game sends.
pub fn py_repr(v: &Value) -> String {
    match v {
        Value::Nil => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Int(i) => i.to_string(),
        Value::Float(f) => py_float(*f),
        Value::Str(s) => {
            let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
            let mut out = String::new();
            out.push(quote);
            for c in s.chars() {
                match c {
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    c if c == quote => {
                        out.push('\\');
                        out.push(c)
                    }
                    c => out.push(c),
                }
            }
            out.push(quote);
            out
        }
        Value::Bin(b) => {
            let mut out = String::from("b'");
            for &byte in b {
                match byte {
                    b'\\' => out.push_str("\\\\"),
                    b'\'' => out.push_str("\\'"),
                    b'\n' => out.push_str("\\n"),
                    b'\r' => out.push_str("\\r"),
                    b'\t' => out.push_str("\\t"),
                    0x20..=0x7e => out.push(byte as char),
                    _ => out.push_str(&format!("\\x{:02x}", byte)),
                }
            }
            out.push('\'');
            out
        }
        Value::Arr(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Map(m) => format!(
            "{{{}}}",
            m.iter().map(|(k, v)| format!("{}: {}", py_repr(k), py_repr(v))).collect::<Vec<_>>().join(", ")
        ),
    }
}

pub fn py_float(f: f64) -> String {
    if f.is_finite() && f == f.trunc() && f.abs() < 1e16 {
        format!("{:.1}", f)
    } else {
        format!("{}", f)
    }
}

/// `json.dumps(value, default=str)` with Python's separators. `ensure_ascii`
/// escapes everything outside ASCII as `\uXXXX`, as Python does by default.
pub fn py_json(v: &Value, ensure_ascii: bool) -> String {
    let mut out = String::new();
    write_py_json(&mut out, v, ensure_ascii);
    out
}

fn write_py_json(out: &mut String, v: &Value, ascii: bool) {
    match v {
        Value::Nil => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Int(i) => out.push_str(&i.to_string()),
        Value::Float(f) => {
            if f.is_nan() {
                out.push_str("NaN")
            } else if f.is_infinite() {
                out.push_str(if *f > 0.0 { "Infinity" } else { "-Infinity" })
            } else {
                out.push_str(&py_float(*f))
            }
        }
        Value::Str(s) => json_string(out, s, ascii),
        Value::Bin(_) => json_string(out, &py_repr(v), ascii),
        Value::Arr(a) => {
            out.push('[');
            for (i, item) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_py_json(out, item, ascii);
            }
            out.push(']');
        }
        Value::Map(m) => {
            out.push('{');
            for (i, (k, item)) in m.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                let key = match k {
                    Value::Str(s) => s.clone(),
                    Value::Bool(b) => (if *b { "true" } else { "false" }).to_string(),
                    Value::Nil => "null".to_string(),
                    other => key_text(other),
                };
                json_string(out, &key, ascii);
                out.push_str(": ");
                write_py_json(out, item, ascii);
            }
            out.push('}');
        }
    }
}

pub fn json_string(out: &mut String, s: &str, ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if ascii && (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

// ------------------------------------------------------------------- reading

#[derive(Debug, Clone, PartialEq)]
pub struct DecodeError(pub String);

impl std::fmt::Display for DecodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// MessagePack with multi-byte fields in little-endian.
pub struct LEUnpacker<'a> {
    pub b: &'a [u8],
    pub p: usize,
    depth: usize,
}

impl<'a> LEUnpacker<'a> {
    pub fn new(buf: &'a [u8], pos: usize) -> Self {
        LEUnpacker { b: buf, p: pos, depth: 0 }
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], DecodeError> {
        if self.p + n > self.b.len() {
            return Err(DecodeError(format!("read out of buffer at {}", self.p)));
        }
        let s = &self.b[self.p..self.p + n];
        self.p += n;
        Ok(s)
    }

    pub fn u8(&mut self) -> Result<u8, DecodeError> {
        Ok(self.take(1)?[0])
    }
    pub fn u16(&mut self) -> Result<u16, DecodeError> {
        Ok(u16::from_le_bytes(self.take(2)?.try_into().unwrap()))
    }
    pub fn u32(&mut self) -> Result<u32, DecodeError> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn u64(&mut self) -> Result<u64, DecodeError> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }

    pub fn read(&mut self) -> Result<Value, DecodeError> {
        let b = *self
            .b
            .get(self.p)
            .ok_or_else(|| DecodeError(format!("unexpected end of buffer at {}", self.p)))?;
        self.p += 1;
        match b {
            0x00..=0x7f => Ok(Value::Int(b as i128)),
            0xe0..=0xff => Ok(Value::Int(b as i128 - 256)),
            0x80..=0x8f => self.map((b & 0x0f) as usize),
            0x90..=0x9f => self.array((b & 0x0f) as usize),
            0xa0..=0xbf => self.string((b & 0x1f) as usize),
            0xc0 => Ok(Value::Nil),
            0xc2 => Ok(Value::Bool(false)),
            0xc3 => Ok(Value::Bool(true)),
            0xc4 => {
                let n = self.u8()? as usize;
                self.bin(n)
            }
            0xc5 => {
                let n = self.u16()? as usize;
                self.bin(n)
            }
            0xc6 => {
                let n = self.u32()? as usize;
                self.bin(n)
            }
            0xca => Ok(Value::Float(f32::from_le_bytes(self.take(4)?.try_into().unwrap()) as f64)),
            0xcb => Ok(Value::Float(f64::from_le_bytes(self.take(8)?.try_into().unwrap()))),
            0xcc => Ok(Value::Int(self.u8()? as i128)),
            0xcd => Ok(Value::Int(self.u16()? as i128)),
            0xce => Ok(Value::Int(self.u32()? as i128)),
            0xcf => Ok(Value::Int(self.u64()? as i128)),
            0xd0 => Ok(Value::Int(self.u8()? as i8 as i128)),
            0xd1 => Ok(Value::Int(self.u16()? as i16 as i128)),
            0xd2 => Ok(Value::Int(self.u32()? as i32 as i128)),
            0xd3 => Ok(Value::Int(self.u64()? as i64 as i128)),
            0xd9 => {
                let n = self.u8()? as usize;
                self.string(n)
            }
            0xda => {
                let n = self.u16()? as usize;
                self.string(n)
            }
            0xdb => {
                let n = self.u32()? as usize;
                self.string(n)
            }
            0xdc => {
                let n = self.u16()? as usize;
                self.array(n)
            }
            0xdd => {
                let n = self.u32()? as usize;
                self.array(n)
            }
            0xde => {
                let n = self.u16()? as usize;
                self.map(n)
            }
            0xdf => {
                let n = self.u32()? as usize;
                self.map(n)
            }
            0xd4..=0xd8 => self.ext(1usize << (b - 0xd4)),
            0xc7 => {
                let n = self.u8()? as usize;
                self.ext(n)
            }
            0xc8 => {
                let n = self.u16()? as usize;
                self.ext(n)
            }
            0xc9 => {
                let n = self.u32()? as usize;
                self.ext(n)
            }
            _ => Err(DecodeError(format!("unknown format byte 0x{:02x} at {}", b, self.p - 1))),
        }
    }

    fn enter(&mut self) -> Result<(), DecodeError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(DecodeError(format!("nesting above {} at {}", MAX_DEPTH, self.p)));
        }
        Ok(())
    }

    // Each element takes at least one byte: a size larger than what is left can
    // only be garbage, and refusing early avoids allocating huge lists.
    fn array(&mut self, n: usize) -> Result<Value, DecodeError> {
        if n > self.b.len() - self.p {
            return Err(DecodeError(format!("impossible array ({}) at {}", n, self.p)));
        }
        self.enter()?;
        let mut out = Vec::with_capacity(n);
        let result = (|| {
            for _ in 0..n {
                out.push(self.read()?);
            }
            Ok(())
        })();
        self.depth -= 1;
        result.map(|_| Value::Arr(out))
    }

    fn map(&mut self, n: usize) -> Result<Value, DecodeError> {
        if 2 * n > self.b.len() - self.p {
            return Err(DecodeError(format!("impossible map ({}) at {}", n, self.p)));
        }
        self.enter()?;
        let mut out: IndexMap<String, Value> = IndexMap::with_capacity(n);
        let result = (|| {
            for _ in 0..n {
                let k = key_text(&self.read()?);
                let v = self.read()?;
                out.insert(k, v);
            }
            Ok(())
        })();
        self.depth -= 1;
        result.map(|_| Value::Map(out.into_iter().map(|(k, v)| (Value::Str(k), v)).collect()))
    }

    fn string(&mut self, n: usize) -> Result<Value, DecodeError> {
        if self.p + n > self.b.len() {
            return Err(DecodeError(format!("truncated string at {}", self.p)));
        }
        let s = String::from_utf8_lossy(&self.b[self.p..self.p + n]).into_owned();
        self.p += n;
        Ok(Value::Str(s))
    }

    fn bin(&mut self, n: usize) -> Result<Value, DecodeError> {
        if self.p + n > self.b.len() {
            return Err(DecodeError(format!("truncated bin at {}", self.p)));
        }
        let s = self.b[self.p..self.p + n].to_vec();
        self.p += n;
        Ok(Value::Bin(s))
    }

    fn ext(&mut self, n: usize) -> Result<Value, DecodeError> {
        let code = self.b.get(self.p).copied().unwrap_or(0);
        let start = (self.p + 1).min(self.b.len());
        let end = (self.p + 1 + n).min(self.b.len());
        let payload = self.b[start..end].to_vec();
        self.p += 1 + n;
        Ok(Value::Map(vec![
            (Value::str("__ext__"), Value::Int(code as i128)),
            (Value::str("data"), Value::Bin(payload)),
        ]))
    }
}

pub fn unpack(buf: &[u8]) -> Result<Value, DecodeError> {
    LEUnpacker::new(buf, 0).read()
}

// ------------------------------------------------------------------- writing

/// Encodes in the game's dialect.
pub fn pack(obj: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    pack_into(&mut out, obj);
    out
}

fn pack_into(out: &mut Vec<u8>, obj: &Value) {
    match obj {
        Value::Nil => out.push(0xc0),
        Value::Bool(b) => out.push(if *b { 0xc3 } else { 0xc2 }),
        Value::Int(i) => {
            let i = *i;
            if (0..=0x7f).contains(&i) {
                out.push(i as u8)
            } else if (-32..0).contains(&i) {
                out.push((i & 0xff) as u8)
            } else if (0..=0xff).contains(&i) {
                out.push(0xcc);
                out.push(i as u8)
            } else if (0..=0xffff).contains(&i) {
                out.push(0xcd);
                out.extend((i as u16).to_le_bytes())
            } else if (0..=0xffff_ffff).contains(&i) {
                out.push(0xce);
                out.extend((i as u32).to_le_bytes())
            } else if i >= 0 {
                out.push(0xcf);
                out.extend((i as u64).to_le_bytes())
            } else if i >= -0x80 {
                out.push(0xd0);
                out.extend((i as i8).to_le_bytes())
            } else if i >= -0x8000 {
                out.push(0xd1);
                out.extend((i as i16).to_le_bytes())
            } else if i >= -0x8000_0000 {
                out.push(0xd2);
                out.extend((i as i32).to_le_bytes())
            } else {
                out.push(0xd3);
                out.extend((i as i64).to_le_bytes())
            }
        }
        Value::Float(f) => {
            out.push(0xcb);
            out.extend(f.to_le_bytes())
        }
        Value::Str(s) => {
            let raw = s.as_bytes();
            let n = raw.len();
            if n < 32 {
                out.push(0xa0 | n as u8)
            } else if n <= 0xff {
                out.push(0xd9);
                out.push(n as u8)
            } else if n <= 0xffff {
                out.push(0xda);
                out.extend((n as u16).to_le_bytes())
            } else {
                out.push(0xdb);
                out.extend((n as u32).to_le_bytes())
            }
            out.extend_from_slice(raw)
        }
        Value::Bin(raw) => {
            let n = raw.len();
            if n <= 0xff {
                out.push(0xc4);
                out.push(n as u8)
            } else {
                out.push(0xc6);
                out.extend((n as u32).to_le_bytes())
            }
            out.extend_from_slice(raw)
        }
        Value::Arr(items) => {
            let n = items.len();
            if n < 16 {
                out.push(0x90 | n as u8)
            } else if n <= 0xffff {
                out.push(0xdc);
                out.extend((n as u16).to_le_bytes())
            } else {
                out.push(0xdd);
                out.extend((n as u32).to_le_bytes())
            }
            for item in items {
                pack_into(out, item)
            }
        }
        Value::Map(pairs) => {
            let n = pairs.len();
            if n < 16 {
                out.push(0x80 | n as u8)
            } else if n <= 0xffff {
                out.push(0xde);
                out.extend((n as u16).to_le_bytes())
            } else {
                out.push(0xdf);
                out.extend((n as u32).to_le_bytes())
            }
            for (k, v) in pairs {
                pack_into(out, k);
                pack_into(out, v)
            }
        }
    }
}

/// A whole frame. Only for tests and stand-ins. An event is `(header, payload...)`.
pub fn build_frame(route: i128, seq: i128, objects: &[Value], tail_events: &[Vec<Value>]) -> Vec<u8> {
    let mut body = pack(&Value::Arr(vec![Value::Int(route), Value::Int(seq)]));
    for o in objects {
        body.extend(pack(o));
    }
    let mut tail = Vec::new();
    for event in tail_events {
        let packed: Vec<u8> = event.iter().flat_map(pack).collect();
        tail.extend(((packed.len() + 4) as u32).to_le_bytes());
        tail.extend(packed);
    }
    let inner = 8 + body.len();
    let mut out = Vec::new();
    out.extend(((inner + tail.len()) as u32).to_le_bytes());
    out.extend((inner as u32).to_le_bytes());
    out.extend(body);
    out.extend(tail);
    out
}

#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub route: Option<i128>,
    pub seq: Option<i128>,
    pub objects: Vec<Value>,
    pub offset: usize,
    pub length: usize,
    pub error: Option<String>,
    pub tail: Vec<u8>,
}

impl Frame {
    /// A frame holding only these objects, for stand-ins of the game.
    pub fn of(objects: Vec<Value>) -> Frame {
        Frame { route: Some(0), seq: Some(0), objects, offset: 0, length: 0, error: None, tail: Vec::new() }
    }
}

fn u32_at(buf: &[u8], pos: usize) -> u32 {
    u32::from_le_bytes(buf[pos..pos + 4].try_into().unwrap())
}

/// Walks the frames of a payload, unwrapping batches (route 2).
pub fn iter_frames(buf: &[u8]) -> Vec<Frame> {
    let mut out = Vec::new();
    frames_between(buf, 0, buf.len(), 0, &mut out);
    out
}

fn frames_between(buf: &[u8], mut pos: usize, end: usize, depth: usize, out: &mut Vec<Frame>) {
    while pos + 8 <= end {
        let mut total = u32_at(buf, pos) as usize;
        let inner = u32_at(buf, pos + 4) as usize;
        if total < 8 || pos + total > end {
            total = end - pos;
        }
        let frame_end = pos + total;
        let content_end = if 8 <= inner && inner <= total { pos + inner } else { frame_end };
        let mut u = LEUnpacker::new(&buf[..frame_end], pos + 8);
        let mut error = None;
        let header = match u.read() {
            Ok(h) => Some(h),
            Err(e) => {
                error = Some(e.0);
                None
            }
        };
        let (mut route, mut seq) = (None, None);
        if let Some(Value::Arr(h)) = &header {
            if h.len() >= 2 {
                if let Some(r) = h[0].py_int() {
                    route = Some(r);
                    seq = h[1].py_int();
                }
            }
        }
        if route == Some(ROUTE_BATCH) && error.is_none() && depth < 4 {
            match u.read() {
                Err(e) => out.push(Frame {
                    route,
                    seq,
                    objects: vec![],
                    offset: pos,
                    length: total,
                    error: Some(e.0),
                    tail: vec![],
                }),
                Ok(_) => frames_between(buf, u.p, frame_end, depth + 1, out),
            }
        } else {
            let mut objects = Vec::new();
            if error.is_none() {
                let mut c = LEUnpacker::new(&buf[..content_end.max(u.p)], u.p);
                while c.p < content_end {
                    match c.read() {
                        Ok(o) => objects.push(o),
                        Err(e) => {
                            error = Some(e.0);
                            break;
                        }
                    }
                }
            }
            out.push(Frame {
                route,
                seq,
                objects,
                offset: pos,
                length: total,
                error,
                tail: buf[content_end..frame_end].to_vec(),
            });
        }
        pos = frame_end;
    }
}

/// The header of each event attached after the body.
pub fn decode_tail(tail: &[u8]) -> Vec<Value> {
    tail_events(tail).into_iter().map(|mut e| e.remove(0)).collect()
}

/// The tail's events whole: `[header, payload...]` of each one. `len` counts
/// its own 4 bytes. A block that does not decode to the end is skipped.
pub fn tail_events(tail: &[u8]) -> Vec<Vec<Value>> {
    let mut events = Vec::new();
    let mut pos = 0usize;
    while pos + 4 <= tail.len() {
        let n = u32_at(tail, pos) as usize;
        if n <= 4 || pos + n > tail.len() {
            break;
        }
        let chunk = &tail[pos + 4..pos + n];
        pos += n;
        let mut u = LEUnpacker::new(chunk, 0);
        let mut objects = Vec::new();
        let mut broken = false;
        while u.p < chunk.len() {
            match u.read() {
                Ok(o) => objects.push(o),
                Err(_) => {
                    broken = true;
                    break;
                }
            }
        }
        if !broken && !objects.is_empty() {
            events.push(objects);
        }
    }
    events
}

/// gzip, zstd or zlib, by their magic bytes; anything else as it is.
pub fn decompress(data: &[u8]) -> Vec<u8> {
    if data.starts_with(&[0x1f, 0x8b]) {
        let mut out = Vec::new();
        if flate2::read::MultiGzDecoder::new(data).read_to_end(&mut out).is_ok() {
            return out;
        }
        return data.to_vec();
    }
    if data.starts_with(&[0x28, 0xb5, 0x2f, 0xfd]) {
        return zstd::stream::decode_all(data).unwrap_or_else(|_| data.to_vec());
    }
    if data.len() > 2 && data[0] == 0x78 && matches!(data[1], 0x01 | 0x5e | 0x9c | 0xda) {
        let mut out = Vec::new();
        if flate2::read::ZlibDecoder::new(data).read_to_end(&mut out).is_ok() {
            return out;
        }
        return data.to_vec();
    }
    data.to_vec()
}

/// Concatenated LE msgpack objects, without framing, filling the whole buffer.
/// The format of the notifications WebSocket (the Journal comes that way).
pub fn unpack_stream(data: &[u8]) -> Option<Vec<Value>> {
    if data.is_empty() {
        return None;
    }
    let mut u = LEUnpacker::new(data, 0);
    let mut objects = Vec::new();
    while u.p < data.len() {
        objects.push(u.read().ok()?);
    }
    match objects.first() {
        Some(Value::Arr(_)) | Some(Value::Map(_)) => Some(objects),
        _ => None,
    }
}

/// Replaces binary blobs that are msgpack with `{"blob": [objects]}`.
pub fn expand_blobs(node: &Value) -> Value {
    expand(node, 0)
}

fn expand(node: &Value, depth: usize) -> Value {
    match node {
        Value::Bin(b) => {
            if depth < MAX_BLOB_DEPTH && b.len() >= 4 {
                if let Some(inner) = unpack_stream(b) {
                    return Value::Map(vec![(
                        Value::str("blob"),
                        Value::Arr(inner.iter().map(|x| expand(x, depth + 1)).collect()),
                    )]);
                }
            }
            node.clone()
        }
        Value::Arr(a) => Value::Arr(a.iter().map(|x| expand(x, depth)).collect()),
        Value::Map(m) => Value::Map(m.iter().map(|(k, v)| (k.clone(), expand(v, depth))).collect()),
        other => other.clone(),
    }
}

/// Shorthand for building packets: `arr![a, b]`.
#[macro_export]
macro_rules! arr {
    ($($x:expr),* $(,)?) => { $crate::tbcodec::Value::Arr(vec![$($crate::tbcodec::IntoValue::into_value($x)),*]) };
}

pub trait IntoValue {
    fn into_value(self) -> Value;
}
impl IntoValue for Value {
    fn into_value(self) -> Value {
        self
    }
}
impl IntoValue for i64 {
    fn into_value(self) -> Value {
        Value::Int(self as i128)
    }
}
impl IntoValue for i32 {
    fn into_value(self) -> Value {
        Value::Int(self as i128)
    }
}
impl IntoValue for i128 {
    fn into_value(self) -> Value {
        Value::Int(self)
    }
}
impl IntoValue for u64 {
    fn into_value(self) -> Value {
        Value::Int(self as i128)
    }
}
impl IntoValue for &str {
    fn into_value(self) -> Value {
        Value::Str(self.to_string())
    }
}
impl IntoValue for String {
    fn into_value(self) -> Value {
        Value::Str(self)
    }
}
impl IntoValue for bool {
    fn into_value(self) -> Value {
        Value::Bool(self)
    }
}
impl IntoValue for Vec<u8> {
    fn into_value(self) -> Value {
        Value::Bin(self)
    }
}
impl IntoValue for &[u8] {
    fn into_value(self) -> Value {
        Value::Bin(self.to_vec())
    }
}
impl IntoValue for Vec<Value> {
    fn into_value(self) -> Value {
        Value::Arr(self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integers_are_little_endian_and_round_trip() {
        for i in [0i128, 1, 127, 128, 255, 256, 65535, 65536, 4294967295, 4294967296, -1, -32, -33, -129, -40000,
                  -3_000_000_000, 485331363759] {
            let packed = pack(&Value::Int(i));
            assert_eq!(Value::Int(i), unpack(&packed).unwrap(), "{}", i);
        }
        assert_eq!(vec![0xcd, 0x01, 0x02], pack(&Value::Int(0x0201)));
    }

    #[test]
    fn maps_keep_their_keys_as_text() {
        let packed = pack(&Value::Map(vec![(Value::Int(123596), Value::Int(1))]));
        let back = unpack(&packed).unwrap();
        assert_eq!(Some(&Value::Int(1)), back.get("123596"));
        let listed = pack(&Value::Map(vec![(arr![arr![1024, 1], 0], Value::Int(1))]));
        assert!(unpack(&listed).unwrap().get("[[1024, 1], 0]").is_some());
    }

    #[test]
    fn garbage_is_refused_rather_than_allocated() {
        assert!(unpack(&[0xdd, 0xff, 0xff, 0xff, 0x7f]).is_err());
        assert!(unpack(&[0xc1]).is_err());
        let deep: Vec<u8> = std::iter::repeat(0x91).take(500).collect();
        assert!(unpack(&deep).is_err());
    }

    #[test]
    fn batches_are_unwrapped() {
        let a = build_frame(402, 1, &[arr![1]], &[]);
        let b = build_frame(15000, 2, &[arr![2]], &[]);
        let mut body = pack(&arr![2, 9]);
        body.extend(pack(&Value::Int(2)));
        body.extend(&a);
        body.extend(&b);
        let mut batch = Vec::new();
        batch.extend(((8 + body.len()) as u32).to_le_bytes());
        batch.extend(((8 + body.len()) as u32).to_le_bytes());
        batch.extend(body);
        let frames = iter_frames(&batch);
        assert_eq!(vec![Some(402), Some(15000)], frames.iter().map(|f| f.route).collect::<Vec<_>>());
    }

    #[test]
    fn blobs_that_are_msgpack_are_opened() {
        let inner = pack(&arr!["global_tournament_user_result", Value::Nil, arr![1024, 1], 1]);
        let tree = expand_blobs(&arr![Value::Bin(inner), Value::Bin(vec![1, 2])]);
        let blob = tree.at(0).unwrap().get("blob").unwrap();
        assert_eq!(Some("global_tournament_user_result"), blob.at(0).unwrap().at(0).unwrap().as_str());
        assert_eq!(&Value::Bin(vec![1, 2]), tree.at(1).unwrap());
    }

    #[test]
    fn compressed_bodies_are_opened() {
        let data = b"hello hello hello hello".to_vec();
        let z = zstd::stream::encode_all(&data[..], 3).unwrap();
        assert_eq!(data, decompress(&z));
        let mut gz = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        std::io::Write::write_all(&mut gz, &data).unwrap();
        assert_eq!(data, decompress(&gz.finish().unwrap()));
        assert_eq!(b"plain".to_vec(), decompress(b"plain"));
    }

    #[test]
    fn python_text_forms() {
        assert_eq!("[1050, 2]", py_json(&arr![1050, 2], false));
        assert_eq!("[\"a\\u00e9\", 1]", py_json(&arr!["aé", 1], true));
        assert_eq!("[[1024, 1], 0]", key_text(&arr![arr![1024, 1], 0]));
        assert_eq!("\"\\ud83d\\ude00\"", py_json(&Value::str("😀"), true));
    }
}
