//! Comparing names, and the forms text is written to the database in.

use unicode_normalization::char::is_combining_mark;
use unicode_normalization::UnicodeNormalization;

/// Comparison form: no accents, no case, single spaces (dashes all the same).
pub fn normalize_name(name: &str) -> String {
    if name.is_empty() {
        return String::new();
    }
    let without: String = name
        .nfd()
        .filter(|c| !is_combining_mark(*c))
        .map(|c| if "—–‒−―".contains(c) { '-' } else { c })
        .collect();
    collapse_spaces(&without).to_lowercase()
}

/// Collapses runs of whitespace into one space and trims.
pub fn collapse_spaces(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Game punctuation and diacritics to the ASCII forms used in `standard_chests`,
/// cut to the width of the column.
pub fn normalize_text(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let cleaned = text
        .replace('\u{2013}', "-")
        .replace('\u{2014}', "-")
        .replace('\u{2018}', "'")
        .replace('\u{2019}', "'")
        .replace('\u{00a0}', " ");
    let folded: String = cleaned.nfkd().filter(|c| !is_combining_mark(*c)).collect();
    folded.trim().chars().take(50).collect()
}

/// Accent- and case-blind key, single-spaced (`_fold_key` of the fix tool).
pub fn fold_key(text: &str) -> String {
    let folded: String = text.nfkd().filter(|c| !is_combining_mark(*c)).collect();
    collapse_spaces(&folded.to_lowercase())
}

/// A file name from free text: letters, digits, '-' and '_', the rest '_'.
pub fn folder_name(text: &str) -> String {
    let name: String = text.trim().chars().map(|c| if c.is_alphanumeric() || c == '-' || c == '_' { c } else { '_' }).collect();
    if name.is_empty() {
        "account".into()
    } else {
        name
    }
}

/// The first `n` characters of `s` (not bytes).
pub fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_compare_without_accents_case_or_extra_spaces() {
        assert_eq!("crash br", normalize_name("  Crásh   BR "));
        assert_eq!("a-b", normalize_name("A—B"));
        assert_ne!(normalize_name("Crash BR"), normalize_name("Cash BR"));
    }

    #[test]
    fn game_text_becomes_the_columns_form() {
        assert_eq!("Fenrir's Chest", normalize_text("Fenrir\u{2019}s Chest"));
        assert_eq!("Level 35 epic Crypt - x", normalize_text("Level 35 epic Crypt \u{2013} x"));
        assert_eq!(50, normalize_text(&"x".repeat(80)).len());
    }
}
