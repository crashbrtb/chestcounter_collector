//! The Windows Task Scheduler entry that runs the collector on its own.
//!
//! One task, named as the installer names it, that starts `tbcollector.exe` (next to
//! this executable) every so many minutes or hours. It is created, changed and
//! removed with `schtasks.exe` for the current user, and read back as XML so the
//! answer does not depend on the language Windows is in.

use std::path::PathBuf;
use std::process::Command;

pub const TASK_NAME: &str = "TB Chest Collector";

/// The intervals offered, in minutes. Below MIN_MINUTES runs would overlap the run in progress.
pub const MIN_MINUTES: u32 = 5;

pub const INTERVALS: [u32; 13] = [5, 10, 15, 30, 45, 60, 120, 180, 240, 360, 480, 720, 1440];

/// What the task scheduler has for the collector.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct TaskInfo {
    /// Minutes between runs (0 when the trigger is not one this program creates).
    pub every_minutes: u32,
    /// `HH:MM` the repetition counts from.
    pub start: String,
    pub enabled: bool,
    pub command: String,
}

pub fn describe_interval(minutes: u32) -> String {
    match minutes {
        0 => "custom schedule".into(),
        1440 => "once a day".into(),
        m if m % 60 == 0 => {
            let h = m / 60;
            if h == 1 { "every hour".into() } else { format!("every {} hours", h) }
        }
        m => format!("every {} minutes", m),
    }
}

/// The collector the task should start: tbcollector.exe beside the running executable.
pub fn collector_exe() -> PathBuf {
    let dir = std::env::current_exe().ok().and_then(|p| p.parent().map(|d| d.to_path_buf())).unwrap_or_default();
    dir.join("tbcollector.exe")
}

fn schtasks(args: &[String]) -> Result<String, String> {
    let mut command = Command::new("schtasks.exe");
    command.args(args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let out = command.output().map_err(|e| format!("could not start schtasks.exe: {}", e))?;
    let text = decode(&out.stdout);
    if out.status.success() {
        Ok(text)
    } else {
        let err = decode(&out.stderr);
        Err(if err.trim().is_empty() { text.trim().to_string() } else { err.trim().to_string() })
    }
}

/// schtasks writes UTF-16 for /XML and the console code page otherwise.
fn decode(bytes: &[u8]) -> String {
    let utf16 = bytes.len() >= 2 && (bytes.starts_with(&[0xFF, 0xFE]) || (bytes[1] == 0 && bytes[0] != 0));
    if utf16 {
        let units: Vec<u16> = bytes.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
        return String::from_utf16_lossy(&units).trim_start_matches('\u{feff}').to_string();
    }
    String::from_utf8_lossy(bytes).to_string()
}

fn tag<'a>(xml: &'a str, name: &str) -> Option<&'a str> {
    let open = format!("<{}>", name);
    let start = xml.find(&open)? + open.len();
    let end = xml[start..].find(&format!("</{}>", name))? + start;
    Some(xml[start..end].trim())
}

/// `PT1H30M`, `P1D`: in minutes.
pub fn duration_minutes(text: &str) -> u32 {
    let mut total = 0u32;
    let mut number = String::new();
    let mut in_time = false;
    for c in text.chars() {
        match c {
            'P' => {}
            'T' => in_time = true,
            d if d.is_ascii_digit() => number.push(d),
            unit => {
                let n: u32 = number.parse().unwrap_or(0);
                number.clear();
                total += match (unit, in_time) {
                    ('D', _) => n * 1440,
                    ('H', true) => n * 60,
                    ('M', true) => n,
                    _ => 0,
                };
            }
        }
    }
    total
}

/// The task as Task Scheduler exports it.
pub fn parse_task_xml(xml: &str) -> TaskInfo {
    let triggers = tag(xml, "Triggers").unwrap_or("");
    let mut every = tag(triggers, "Interval").map(duration_minutes).unwrap_or(0);
    if every == 0 {
        if let Some(days) = tag(triggers, "DaysInterval").and_then(|d| d.parse::<u32>().ok()) {
            every = days * 1440;
        }
    }
    let start = tag(triggers, "StartBoundary")
        .and_then(|s| s.split('T').nth(1))
        .map(|t| t.chars().take(5).collect())
        .unwrap_or_default();
    let settings = tag(xml, "Settings").unwrap_or("");
    let enabled = tag(settings, "Enabled").map(|e| e != "false").unwrap_or(true);
    let command = tag(xml, "Command").unwrap_or("").trim_matches('"').to_string();
    TaskInfo { every_minutes: every, start, enabled, command }
}

/// The collector's task, if there is one.
pub fn query() -> Result<Option<TaskInfo>, String> {
    match schtasks(&["/Query".into(), "/TN".into(), TASK_NAME.into(), "/XML".into()]) {
        Ok(xml) => Ok(Some(parse_task_xml(&xml))),
        Err(e) => {
            // "does not exist" in whatever language: ask the list instead of reading the message.
            let listed = schtasks(&["/Query".into(), "/FO".into(), "CSV".into(), "/NH".into()])
                .map(|all| all.lines().any(|l| l.trim_start_matches('"').trim_start_matches('\\').starts_with(&format!("{}\"", TASK_NAME))))
                .unwrap_or(false);
            if listed { Err(e) } else { Ok(None) }
        }
    }
}

/// The schtasks arguments that (re)create the task.
pub fn create_args(exe: &str, every_minutes: u32, start: &str) -> Result<Vec<String>, String> {
    let (kind, modifier) = match every_minutes {
        m if (MIN_MINUTES..60).contains(&m) => ("MINUTE", m),
        1440 => ("DAILY", 1),
        m if m >= 60 && m < 1440 && m % 60 == 0 => ("HOURLY", m / 60),
        m if m > MIN_MINUTES && m < 1440 => ("MINUTE", m),
        m => return Err(format!("an interval of {} minutes cannot be scheduled ({} minutes to 24 hours)", m, MIN_MINUTES)),
    };
    let mut args: Vec<String> = vec!["/Create".into(), "/F".into(), "/TN".into(), TASK_NAME.into(), "/TR".into(),
                                     format!("\"{}\"", exe), "/SC".into(), kind.into(), "/MO".into(), modifier.to_string()];
    let start = start.trim();
    if !start.is_empty() {
        let valid = start.len() == 5
            && start[..2].parse::<u32>().map(|h| h < 24).unwrap_or(false)
            && &start[2..3] == ":"
            && start[3..].parse::<u32>().map(|m| m < 60).unwrap_or(false);
        if !valid {
            return Err(format!("'{}' is not a time of day (HH:MM, for instance 08:00)", start));
        }
        args.push("/ST".into());
        args.push(start.into());
    }
    Ok(args)
}

/// Creates the task, or replaces the one there is.
pub fn create(every_minutes: u32, start: &str) -> Result<(), String> {
    create_for(&collector_exe(), every_minutes, start)
}

/// Creates the task, or replaces the one there is, to start `exe`.
pub fn create_for(exe: &std::path::Path, every_minutes: u32, start: &str) -> Result<(), String> {
    if !exe.is_file() {
        return Err(format!("{} was not found next to this program", exe.display()));
    }
    schtasks(&create_args(&exe.to_string_lossy(), every_minutes, start)?).map(|_| ())
}

pub fn remove() -> Result<(), String> {
    schtasks(&["/Delete".into(), "/F".into(), "/TN".into(), TASK_NAME.into()]).map(|_| ())
}

/// Starts the task now, as the scheduler would.
pub fn run_now() -> Result<(), String> {
    schtasks(&["/Run".into(), "/TN".into(), TASK_NAME.into()]).map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_interval_becomes_the_schtasks_schedule() {
        let a = create_args("C:\\TB\\tbcollector.exe", 30, "").unwrap();
        assert_eq!(vec!["/Create", "/F", "/TN", TASK_NAME, "/TR", "\"C:\\TB\\tbcollector.exe\"", "/SC", "MINUTE", "/MO", "30"], a);
        assert_eq!(&["HOURLY", "/MO", "2"], &create_args("x", 120, "").unwrap()[7..10]);
        assert_eq!(&["DAILY", "/MO", "1", "/ST", "08:00"], &create_args("x", 1440, "08:00").unwrap()[7..12]);
        assert_eq!(&["MINUTE", "/MO", "90"], &create_args("x", 90, "").unwrap()[7..10]);
        assert!(create_args("x", 0, "").is_err());
        assert!(create_args("x", 4, "").is_err());
        assert_eq!(&["MINUTE", "/MO", "5"], &create_args("x", 5, "").unwrap()[7..10]);
        assert!(create_args("x", 60, "8h").is_err());
        assert!(create_args("x", 60, "24:00").is_err());
    }

    #[test]
    fn the_exported_task_is_read_back() {
        let xml = r#"<?xml version="1.0" encoding="UTF-16"?>
<Task><Triggers><TimeTrigger><Repetition><Interval>PT2H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
<StartBoundary>2026-11-07T08:15:00</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
<Settings><Enabled>false</Enabled></Settings>
<Actions><Exec><Command>"C:\TB Chest Collector\tbcollector.exe"</Command></Exec></Actions></Task>"#;
        let t = parse_task_xml(xml);
        assert_eq!(TaskInfo { every_minutes: 120, start: "08:15".into(), enabled: false,
                              command: "C:\\TB Chest Collector\\tbcollector.exe".into() }, t);
        let daily = "<Task><Triggers><CalendarTrigger><StartBoundary>2026-11-07T06:00:00</StartBoundary>\
                     <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers></Task>";
        assert_eq!(1440, parse_task_xml(daily).every_minutes);
        assert_eq!(90, duration_minutes("PT1H30M"));
        assert_eq!("every 2 hours", describe_interval(120));
        assert_eq!("every 45 minutes", describe_interval(45));
    }

    #[test]
    fn utf16_output_is_decoded() {
        let bytes: Vec<u8> = "\u{feff}<Task/>".encode_utf16().flat_map(|u| u.to_le_bytes()).collect();
        assert_eq!("<Task/>", decode(&bytes));
        assert_eq!("ok", decode(b"ok"));
    }
}
