//! Deterministic lexical ranking of already-scoped active facts. This is not
//! semantic search. SQLite supplies bounded candidates before this final ranking.
use memweft_store::lexical_terms as terms;
use memweft_types::Fact;
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Default, Serialize)]
pub(crate) struct Match {
    pub score: usize,
    pub key_terms: Vec<String>,
    pub value_terms: Vec<String>,
}

pub(crate) struct Ranking {
    pub query_terms: BTreeSet<String>,
    pub matches: BTreeMap<String, Match>,
}

impl Ranking {
    pub fn new(facts: &mut [Fact], query: Option<&str>) -> Self {
        let query_terms = terms(query.unwrap_or_default());
        let matches: BTreeMap<_, _> = facts
            .iter()
            .map(|fact| {
                let mut found = Match::default();
                if !query_terms.is_empty() {
                    found.key_terms = terms(&fact.fact_key)
                        .intersection(&query_terms)
                        .cloned()
                        .collect();
                    found.value_terms = terms(&fact.value.to_string())
                        .intersection(&query_terms)
                        .cloned()
                        .collect();
                    // Repeated words do not inflate relevance. A key match
                    // carries twice the weight of a value match.
                    found.score = 2 * found.key_terms.len() + found.value_terms.len();
                }
                (fact.fact_id.clone(), found)
            })
            .collect();
        facts.sort_by(|a, b| {
            matches[&b.fact_id]
                .score
                .cmp(&matches[&a.fact_id].score)
                .then(a.fact_key.cmp(&b.fact_key))
                .then(a.fact_id.cmp(&b.fact_id))
        });
        Self {
            query_terms,
            matches,
        }
    }

    pub fn method(&self) -> &'static str {
        if self.query_terms.is_empty() {
            "key_order"
        } else {
            "lexical_overlap_v1"
        }
    }
}
