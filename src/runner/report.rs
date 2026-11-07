//! The summary written at the end of every run's log: whether it went well, how many
//! chests each profile got, what each module did, and every warning and error of the
//! run gathered in one place - so nobody has to read the whole log to know.

use std::collections::BTreeMap;

use crate::logger::{Level, Tally};
use crate::modules::{title_of, ModuleResult};

/// How the run ended, as far as the summary is concerned.
#[derive(Debug, Clone, PartialEq)]
pub enum Ending {
    /// It went through; the results say what was and was not done.
    Finished,
    /// Held ESC (or the interface's Stop).
    Cancelled(String),
    /// The configuration did not let it start.
    NotStarted(String),
    /// Something stopped it half-way.
    Stopped(String),
}

/// A module that was not due on this run, and why.
#[derive(Debug, Clone, PartialEq)]
pub struct NotDue {
    pub module: String,
    pub profile: String,
    pub why: String,
}

fn describe_time(epoch: i64) -> String {
    chrono::DateTime::from_timestamp(epoch, 0)
        .map(|t| t.with_timezone(&chrono::Local).format("%d/%m %H:%M").to_string())
        .unwrap_or_default()
}

fn int(r: &ModuleResult, key: &str) -> Option<i64> {
    r.extra.get(key).and_then(|v| v.as_i64())
}

fn flag(r: &ModuleResult, key: &str) -> Option<bool> {
    r.extra.get(key).and_then(|v| v.as_bool())
}

/// What the module did on the profile, in a few words. `chests` adds up every attempt.
pub fn detail(r: &ModuleResult, chests: i64, incomplete: i64) -> String {
    let mut parts: Vec<String> = Vec::new();
    match r.module.as_str() {
        "chests" => {
            parts.push(format!("{} chest(s) collected", chests));
            if incomplete > 0 {
                parts.push(format!("{} incomplete", incomplete));
            }
        }
        "shield" if r.success => {
            let until = int(r, "shield_ends").map(describe_time).filter(|t| !t.is_empty());
            let what = if flag(r, "renewed") == Some(true) { "new shield activated" } else { "shield already up" };
            parts.push(match until {
                Some(t) => format!("{} (until {})", what, t),
                None => what.to_string(),
            });
        }
        "members" if r.success => {
            if let Some(n) = int(r, "members") {
                parts.push(format!("{} members read", n));
            }
            parts.push(if flag(r, "sent") == Some(true) { "sent to the site".into() } else { "saved to the files".into() });
        }
        "events" if r.success || r.extra.contains_key("tournaments") => {
            if let Some(n) = int(r, "tournaments") {
                parts.push(format!("{} tournament(s) read", n));
            }
            if let Some(n) = int(r, "sent") {
                parts.push(format!("{} sent", n));
            }
            if let Some(n) = int(r, "published").filter(|n| *n > 0) {
                parts.push(format!("{} published", n));
            }
            if let Some(n) = int(r, "for_review").filter(|n| *n > 0) {
                parts.push(format!("{} for review", n));
            }
        }
        _ => {}
    }
    if !r.success {
        parts.push(if r.reason.is_empty() { "failed".into() } else { r.reason.clone() });
    }
    parts.join(", ")
}

/// The summary's lines, without the log's time and level.
///
/// `results` is every result of the run, attempts included (the chests of each
/// attempt add up); the last attempt of each module on each profile is its status.
pub fn lines(ending: &Ending, duration: &str, results: &[ModuleResult], not_due: &[NotDue], tally: &Tally) -> Vec<String> {
    let last = super::final_results(results);
    let failed = last.iter().filter(|r| !r.success).count();
    let done = last.iter().filter(|r| r.success && !r.module.is_empty()).count();
    let collected: i64 = results.iter().map(|r| r.collected).sum();
    let incomplete: i64 = results.iter().map(|r| r.incomplete).sum();

    let mut out = vec!["=".repeat(60)];
    out.push(if duration.is_empty() { "RUN SUMMARY".to_string() } else { format!("RUN SUMMARY (took {})", duration) });
    let status = match ending {
        Ending::Finished if last.is_empty() => "NOTHING TO DO - no module was due on any profile".to_string(),
        Ending::Finished if failed == 0 => format!("SUCCESS - {} task(s) done, none failed", done),
        Ending::Finished => format!("FINISHED WITH FAILURES - {} task(s) done, {} not done", done, failed),
        Ending::Cancelled(why) => format!("CANCELLED ({}) - {} task(s) done before it stopped", why, done),
        Ending::NotStarted(why) => format!("NOT STARTED - {}", why),
        Ending::Stopped(why) => format!("STOPPED BY AN ERROR - {}", why),
    };
    out.push(format!("Status: {}", status));
    out.push(if incomplete > 0 {
        format!("Chests collected: {} ({} incomplete)", collected, incomplete)
    } else {
        format!("Chests collected: {}", collected)
    });

    // Per profile, in the order they were done.
    let mut order: Vec<(String, String)> = Vec::new();
    for r in &last {
        let key = (r.account.clone(), r.profile.clone());
        if !r.profile.is_empty() && !r.module.is_empty() && !order.contains(&key) {
            order.push(key);
        }
    }
    for (account, profile) in &order {
        let mine: Vec<&ModuleResult> = last.iter().filter(|r| &r.account == account && &r.profile == profile && !r.module.is_empty()).collect();
        let chests: i64 = results.iter().filter(|r| &r.account == account && &r.profile == profile).map(|r| r.collected).sum();
        let label = if account.is_empty() { profile.clone() } else { format!("{} / {}", account, profile) };
        out.push(format!("  {} - {} chest(s)", label, chests));
        for r in mine {
            let (c, i) = results
                .iter()
                .filter(|x| x.account == r.account && x.profile == r.profile && x.module == r.module)
                .fold((0, 0), |(c, i), x| (c + x.collected, i + x.incomplete));
            out.push(format!("    {:<8} {}: {}", if r.success { "[OK]" } else { "[FAILED]" }, title_of(&r.module), detail(r, c, i)));
        }
    }
    // What failed above any one module: a whole account, a profile, a module left out.
    for r in last.iter().filter(|r| !r.success && (r.profile.is_empty() || r.module.is_empty())) {
        let mut parts: Vec<String> = Vec::new();
        if !r.account.is_empty() {
            parts.push(format!("account '{}'", r.account));
        }
        if !r.profile.is_empty() {
            parts.push(format!("profile '{}'", r.profile));
        }
        if !r.module.is_empty() {
            parts.push(format!("module '{}'", title_of(&r.module)));
        }
        let what = if parts.is_empty() { "run".to_string() } else { parts.join(", ") };
        out.push(format!("  [FAILED] {}: {}", what, r.reason));
    }

    if !not_due.is_empty() {
        let mut by_module: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for n in not_due {
            by_module.entry(title_of(&n.module)).or_default().push(format!("{} ({})", n.profile, n.why));
        }
        out.push(format!("Not due on this run ({}):", not_due.len()));
        for (title, profiles) in by_module {
            out.push(format!("  {}: {}", title, profiles.join("; ")));
        }
    }

    if tally.warnings + tally.errors == 0 {
        out.push("Warnings: 0 | Errors: 0".into());
    } else {
        out.push(format!("Warnings: {} | Errors: {} - what they said:", tally.warnings, tally.errors));
        for (time, level, message) in &tally.lines {
            out.push(format!("  {} {:<7} {}", time, if *level == Level::Error { "ERROR" } else { "WARNING" }, message));
        }
        let more = tally.warnings + tally.errors - tally.lines.len();
        if more > 0 {
            out.push(format!("  ... and {} more (see above in the log)", more));
        }
    }
    out.push("=".repeat(60));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn result(module: &str, profile: &str, success: bool, collected: i64) -> ModuleResult {
        ModuleResult { success, collected, account: "Main".into(), profile: profile.into(), module: module.into(),
                       reason: if success { String::new() } else { "no answer".into() }, ..Default::default() }
    }

    #[test]
    fn a_good_run_says_success_and_each_profiles_chests() {
        let mut members = result("members", "Alpha", true, 0);
        members.extra.insert("members".into(), json!(98));
        members.extra.insert("sent".into(), json!(true));
        let results = vec![result("chests", "Alpha", false, 5), result("chests", "Alpha", true, 7), members,
                           result("chests", "Beta", true, 3)];
        let text = lines(&Ending::Finished, "0:01:00", &results, &[], &Tally::default()).join("\n");
        assert!(text.contains("Status: SUCCESS - 3 task(s) done, none failed"), "{}", text);
        assert!(text.contains("Chests collected: 15"), "{}", text);
        assert!(text.contains("Main / Alpha - 12 chest(s)"), "attempts add up: {}", text);
        assert!(text.contains("[OK]     Clan chests: 12 chest(s) collected"), "{}", text);
        assert!(text.contains("98 members read, sent to the site"), "{}", text);
        assert!(text.contains("Main / Beta - 3 chest(s)"), "{}", text);
        assert!(text.contains("Warnings: 0 | Errors: 0"), "{}", text);
    }

    #[test]
    fn failures_and_the_errors_logged_are_repeated() {
        let results = vec![result("chests", "Alpha", false, 2),
                           ModuleResult { account: "Other".into(), reason: "Could not log in".into(), ..Default::default() }];
        let tally = Tally { warnings: 1, errors: 2,
                            lines: vec![("10:00:00".into(), Level::Error, "database down".into()),
                                        ("10:00:01".into(), Level::Warning, "slow".into())] };
        let not_due = vec![NotDue { module: "events".into(), profile: "Main / Alpha".into(), why: "already done today".into() }];
        let text = lines(&Ending::Finished, "0:00:05", &results, &not_due, &tally).join("\n");
        assert!(text.contains("FINISHED WITH FAILURES - 0 task(s) done, 2 not done"), "{}", text);
        assert!(text.contains("[FAILED] Clan chests: 2 chest(s) collected, no answer"), "{}", text);
        assert!(text.contains("[FAILED] account 'Other': Could not log in"), "{}", text);
        assert!(text.contains("Tournament rankings: Main / Alpha (already done today)"), "{}", text);
        assert!(text.contains("Warnings: 1 | Errors: 2"), "{}", text);
        assert!(text.contains("10:00:00 ERROR   database down"), "{}", text);
        assert!(text.contains("... and 1 more"), "{}", text);
    }

    #[test]
    fn a_run_that_did_not_start_or_was_cancelled_says_so() {
        let text = lines(&Ending::NotStarted("no account".into()), "", &[], &[], &Tally::default()).join("\n");
        assert!(text.contains("Status: NOT STARTED - no account"), "{}", text);
        let text = lines(&Ending::Cancelled("ESC".into()), "0:00:01", &[result("chests", "A", true, 1)], &[], &Tally::default())
            .join("\n");
        assert!(text.contains("CANCELLED (ESC) - 1 task(s) done"), "{}", text);
        let text = lines(&Ending::Finished, "0:00:01", &[], &[], &Tally::default()).join("\n");
        assert!(text.contains("NOTHING TO DO"), "{}", text);
    }
}
