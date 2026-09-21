//! Framework-neutral document operations. Both LangGraph adapters only map types.
use crate::*;
use std::collections::BTreeMap;

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Operation {
    Get {
        namespace: Vec<String>,
        key: String,
    },
    Put {
        namespace: Vec<String>,
        key: String,
        value: Option<Value>,
    },
    Search {
        prefix: Vec<String>,
        #[serde(default)]
        filter: Option<Value>,
        limit: usize,
        offset: usize,
    },
    List {
        #[serde(default)]
        conditions: Vec<Condition>,
        max_depth: Option<usize>,
        limit: usize,
        offset: usize,
    },
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Condition {
    pub match_type: MatchType,
    pub path: Vec<String>,
}
#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum MatchType {
    Prefix,
    Suffix,
}

pub(crate) fn batch(
    memory: &Memory,
    scope: UserScope,
    operations: Vec<Operation>,
) -> StoreResult<Value> {
    let scope = scope.scope()?;
    let mut docs = memory.store.documents(&scope, &["external".into()])?;
    for doc in &mut docs {
        doc.namespace.remove(0);
    }
    docs.sort_by(|a, b| {
        b.updated_at
            .cmp(&a.updated_at)
            .then(a.namespace.cmp(&b.namespace))
            .then(a.key.cmp(&b.key))
    });
    let mut results = Vec::new();
    let mut writes = BTreeMap::new();
    for op in operations {
        results.push(match op {
            Operation::Get { namespace, key } => serde_json::to_value(
                docs.iter()
                    .find(|d| d.namespace == namespace && d.key == key),
            )?,
            Operation::Put {
                namespace,
                key,
                value,
            } => {
                if namespace.is_empty() || namespace.iter().any(|s| s.trim().is_empty()) {
                    return Err(invalid("namespace must be nonempty"));
                }
                if value.as_ref().is_some_and(|v| !v.is_object()) {
                    return Err(invalid("store value must be an object or null"));
                }
                let mut full = vec!["external".into()];
                full.extend(namespace);
                writes.insert(
                    (full.clone(), key.clone()),
                    Mutation {
                        namespace: full,
                        key,
                        value,
                        expected_revision: None,
                    },
                );
                Value::Null
            }
            Operation::Search {
                prefix,
                filter,
                limit,
                offset,
            } => {
                if let Some(ref f) = filter {
                    if !f.is_object() {
                        return Err(invalid("filter must be an object"));
                    }
                    validate_filter(f)?;
                }
                let matches: Vec<_> = docs
                    .iter()
                    .filter(|d| {
                        d.namespace.starts_with(&prefix)
                            && filter
                                .as_ref()
                                .map(|f| compare(&d.value, f))
                                .unwrap_or(true)
                    })
                    .skip(offset)
                    .take(limit)
                    .collect();
                serde_json::to_value(matches)?
            }
            Operation::List {
                conditions,
                max_depth,
                limit,
                offset,
            } => {
                if max_depth == Some(0) {
                    return Err(invalid("max_depth must be positive"));
                }
                let mut namespaces: Vec<Vec<String>> = docs
                    .iter()
                    .filter(|d| {
                        conditions.iter().all(|c| {
                            if c.path.len() > d.namespace.len() {
                                return false;
                            }
                            let slice = match c.match_type {
                                MatchType::Prefix => &d.namespace[..c.path.len()],
                                MatchType::Suffix => {
                                    &d.namespace[d.namespace.len() - c.path.len()..]
                                }
                            };
                            slice
                                .iter()
                                .zip(&c.path)
                                .all(|(actual, expected)| expected == "*" || actual == expected)
                        })
                    })
                    .map(|d| {
                        d.namespace[..max_depth
                            .unwrap_or(d.namespace.len())
                            .min(d.namespace.len())]
                            .to_vec()
                    })
                    .collect();
                namespaces.sort();
                namespaces.dedup();
                serde_json::to_value(
                    namespaces
                        .into_iter()
                        .skip(offset)
                        .take(limit)
                        .collect::<Vec<_>>(),
                )?
            }
        });
    }
    // Reads observe the state before this batch's writes, matching LangGraph's
    // reference store. Last write to a key wins; all writes commit together.
    memory
        .store
        .mutate_documents(&scope, &writes.into_values().collect::<Vec<_>>())?;
    Ok(Value::Array(results))
}
fn validate_filter(value: &Value) -> StoreResult<()> {
    match value {
        Value::Object(map) => {
            for (key, v) in map {
                if key.starts_with('$')
                    && !matches!(
                        key.as_str(),
                        "$eq" | "$ne" | "$gt" | "$gte" | "$lt" | "$lte"
                    )
                {
                    return Err(invalid("unsupported filter operator"));
                }
                if !key.starts_with('$') {
                    validate_filter(v)?;
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                validate_filter(item)?;
            }
        }
        _ => {}
    }
    Ok(())
}
fn equal(a: &Value, b: &Value) -> bool {
    match (a.as_f64(), b.as_f64()) {
        (Some(a), Some(b)) => a == b,
        _ => a == b,
    }
}
fn compare(actual: &Value, expected: &Value) -> bool {
    match expected {
        Value::Object(map) if map.keys().any(|k| k.starts_with('$')) => {
            map.iter().all(|(op, value)| {
                let order = match (actual, value) {
                    (Value::Number(a), Value::Number(b)) => a.as_f64().partial_cmp(&b.as_f64()),
                    (Value::String(a), Value::String(b)) => Some(a.cmp(b)),
                    _ => None,
                };
                match op.as_str() {
                    "$eq" => equal(actual, value),
                    "$ne" => !equal(actual, value),
                    "$gt" => order.is_some_and(|o| o.is_gt()),
                    "$gte" => order.is_some_and(|o| o.is_ge()),
                    "$lt" => order.is_some_and(|o| o.is_lt()),
                    "$lte" => order.is_some_and(|o| o.is_le()),
                    _ => false,
                }
            })
        }
        Value::Object(map) => actual.is_object() && map.iter().all(|(k, v)| compare(&actual[k], v)),
        Value::Array(items) => actual.as_array().is_some_and(|a| {
            a.len() == items.len() && a.iter().zip(items).all(|(a, b)| compare(a, b))
        }),
        _ => equal(actual, expected),
    }
}
