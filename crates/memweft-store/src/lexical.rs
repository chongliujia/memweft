//! Shared, versioned tokenizer for indexed retrieval and legacy ranking.
use std::collections::BTreeSet;

fn is_ideograph(c: char) -> bool {
    matches!(c, '\u{3400}'..='\u{4dbf}' | '\u{4e00}'..='\u{9fff}' |
        '\u{f900}'..='\u{faff}' | '\u{20000}'..='\u{323af}')
}

/// Unicode words/numbers split at punctuation; adjacent ideograph bigrams for
/// Chinese text without spaces. A standalone ideograph is a single term.
pub fn terms(text: &str) -> BTreeSet<String> {
    let mut result = BTreeSet::new();
    let mut word = String::new();
    let mut ideographs = Vec::new();
    fn flush(word: &mut String, chars: &mut Vec<char>, output: &mut BTreeSet<String>) {
        if !word.is_empty() {
            output.insert(std::mem::take(word));
        }
        if chars.len() == 1 {
            output.insert(chars[0].to_string());
        } else {
            for pair in chars.windows(2) {
                output.insert(pair.iter().collect());
            }
        }
        chars.clear();
    }
    for c in text.to_lowercase().chars() {
        if is_ideograph(c) {
            if !word.is_empty() {
                flush(&mut word, &mut ideographs, &mut result);
            }
            ideographs.push(c);
        } else if c.is_alphanumeric() {
            if !ideographs.is_empty() {
                flush(&mut word, &mut ideographs, &mut result);
            }
            word.push(c);
        } else {
            flush(&mut word, &mut ideographs, &mut result);
        }
    }
    flush(&mut word, &mut ideographs, &mut result);
    result
}
