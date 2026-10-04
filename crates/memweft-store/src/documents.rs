use crate::{Scope, SqliteStore, StoreError, StoreResult};
use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Document {
    pub namespace: Vec<String>,
    pub key: String,
    pub value: Value,
    pub revision: u64,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

#[derive(Debug, Default)]
pub struct DocumentWindow {
    pub documents: Vec<Document>,
    pub omitted_keys: Vec<String>,
    pub omissions_truncated: bool,
}

// Documents written by this store contain canonical UTC timestamps. Strip the
// trailing Z so whole seconds sort before fractional seconds, at full precision.
const CREATED_ORDER: &str = "rtrim(json_extract(document, '$.created_at'), 'Z')";

/// Canonical JSON arrays with this component prefix have either ',' or ']'
/// after the serialized last component. Both delimiters fit in [',', '^').
pub(crate) fn namespace_bounds(prefix: &[String]) -> StoreResult<(String, String)> {
    if prefix.is_empty() {
        return Ok(("[".into(), "\\".into()));
    }
    let mut stem = serde_json::to_string(prefix)?;
    stem.pop();
    Ok((format!("{stem},"), format!("{stem}^")))
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Mutation {
    pub namespace: Vec<String>,
    pub key: String,
    /// None deletes the document.
    pub value: Option<Value>,
    /// None is unconditional; 0 requires absence; >0 requires that revision.
    pub expected_revision: Option<u64>,
}

pub(crate) fn ensure_schema(conn: &Connection) -> StoreResult<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS memweft_documents (
        tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        namespace TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
        document TEXT NOT NULL,
        PRIMARY KEY (tenant_id, user_id, agent_id, namespace, key)
    );",
    )?;
    conn.execute_batch(&format!(
        "CREATE INDEX IF NOT EXISTS memweft_documents_recent ON memweft_documents
        (tenant_id, user_id, agent_id, namespace, {CREATED_ORDER}, key);
        CREATE INDEX IF NOT EXISTS memweft_documents_learning ON memweft_documents
        (tenant_id, user_id, namespace, agent_id, key)
        WHERE namespace >= '[\"learning\",' AND namespace < '[\"learning\"^';"
    ))?;
    Ok(())
}

pub(crate) fn list(
    store: &SqliteStore,
    scope: &Scope,
    prefix: &[String],
) -> StoreResult<Vec<Document>> {
    let (lower, upper) = namespace_bounds(prefix)?;
    store.with_connection(|conn| {
        let mut stmt = conn.prepare_cached(
            "SELECT document FROM memweft_documents
            WHERE tenant_id=? AND user_id=? AND agent_id=?
            AND namespace>=? AND namespace<? ORDER BY namespace, key",
        )?;
        let rows = stmt.query_map(
            params![scope.tenant_id, scope.user_id, scope.agent_id, lower, upper],
            |r| r.get::<_, String>(0),
        )?;
        rows.map(|row| Ok(serde_json::from_str(&row?)?)).collect()
    })
}

pub(crate) fn get(
    store: &SqliteStore,
    scope: &Scope,
    namespace: &[String],
    key: &str,
) -> StoreResult<Option<Document>> {
    store.with_connection(|conn| {
        let encoded: Option<String> = conn
            .prepare_cached(
                "SELECT document FROM memweft_documents
            WHERE tenant_id=? AND user_id=? AND agent_id=? AND namespace=? AND key=?",
            )?
            .query_row(
                params![
                    scope.tenant_id,
                    scope.user_id,
                    scope.agent_id,
                    serde_json::to_string(namespace)?,
                    key
                ],
                |r| r.get(0),
            )
            .optional()?;
        encoded
            .map(|s| serde_json::from_str(&s).map_err(StoreError::from))
            .transpose()
    })
}

pub(crate) fn recent(
    store: &SqliteStore,
    scope: &Scope,
    namespace: &[String],
    limit: usize,
    omission_limit: usize,
) -> StoreResult<DocumentWindow> {
    let limit = i64::try_from(limit)
        .map_err(|_| StoreError::InvalidInput("document limit is too large".into()))?;
    let sample_limit = i64::try_from(omission_limit)
        .ok()
        .and_then(|n| n.checked_add(1))
        .ok_or_else(|| StoreError::InvalidInput("omission limit is too large".into()))?;
    let namespace = serde_json::to_string(namespace)?;
    store.with_connection(|conn| {
        let tx = conn.transaction()?;
        let mut documents: Vec<Document> = if limit == 0 {
            vec![]
        } else {
            let mut stmt = tx.prepare_cached(&format!(
                "SELECT document FROM memweft_documents
                WHERE tenant_id=?1 AND user_id=?2 AND agent_id=?3 AND namespace=?4
                ORDER BY {CREATED_ORDER} DESC, key DESC LIMIT ?5"
            ))?;
            let rows = stmt.query_map(
                params![
                    scope.tenant_id,
                    scope.user_id,
                    scope.agent_id,
                    namespace,
                    limit
                ],
                |r| r.get::<_, String>(0),
            )?;
            rows.map(|row| Ok(serde_json::from_str(&row?)?))
                .collect::<StoreResult<_>>()?
        };
        documents.reverse();
        let mut omitted_keys = vec![];
        // Fewer than limit means the whole namespace was returned. Otherwise
        // read only the diagnostic sample, using the same chronological index.
        if documents.len() as i64 == limit {
            let boundary = documents.first().map(|d| {
                (
                    d.created_at
                        .to_rfc3339_opts(chrono::SecondsFormat::AutoSi, true)
                        .trim_end_matches('Z')
                        .to_owned(),
                    d.key.clone(),
                )
            });
            let predicate = if boundary.is_some() {
                format!("AND ({CREATED_ORDER}, key) < (?6, ?7)")
            } else {
                String::new()
            };
            let mut stmt = tx.prepare_cached(&format!(
                "SELECT key FROM memweft_documents
                WHERE tenant_id=?1 AND user_id=?2 AND agent_id=?3 AND namespace=?4 {predicate}
                ORDER BY {CREATED_ORDER}, key LIMIT ?5"
            ))?;
            let mut values = vec![
                scope.tenant_id.clone().into(),
                scope.user_id.clone().into(),
                scope.agent_id.clone().into(),
                namespace.into(),
                rusqlite::types::Value::Integer(sample_limit),
            ];
            if let Some((time, key)) = boundary {
                values.push(time.into());
                values.push(key.into());
            }
            let rows = stmt.query_map(rusqlite::params_from_iter(values), |r| {
                r.get::<_, String>(0)
            })?;
            omitted_keys = rows.collect::<Result<_, _>>()?;
        }
        let omissions_truncated = omitted_keys.len() > omission_limit;
        omitted_keys.truncate(omission_limit);
        tx.commit()?;
        Ok(DocumentWindow {
            documents,
            omitted_keys,
            omissions_truncated,
        })
    })
}

pub(crate) fn mutate(
    store: &SqliteStore,
    scope: &Scope,
    mutations: &[Mutation],
) -> StoreResult<()> {
    mutate_checked(store, scope, mutations, &[])
}

pub(crate) fn mutate_checked(
    store: &SqliteStore,
    scope: &Scope,
    mutations: &[Mutation],
    guards: &[crate::PoolRevision],
) -> StoreResult<()> {
    store.with_connection(|conn| {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        crate::pools::check_revisions(&tx, scope, guards)?;
        let mut seen = std::collections::HashSet::new();
        for change in mutations {
            if change.namespace.is_empty() || change.key.is_empty() || change.namespace.iter().any(String::is_empty) {
                return Err(StoreError::InvalidInput("namespace and key must be nonempty".into()));
            }
            let ns = serde_json::to_string(&change.namespace)?;
            if !seen.insert((ns.clone(), change.key.clone())) {
                return Err(StoreError::InvalidInput("duplicate document in transaction".into()));
            }
            let old: Option<String> = tx.query_row("SELECT document FROM memweft_documents
                WHERE tenant_id=? AND user_id=? AND agent_id=? AND namespace=? AND key=?",
                params![scope.tenant_id, scope.user_id, scope.agent_id, ns, change.key], |r| r.get(0)).optional()?;
            let old: Option<Document> = old.map(|v| serde_json::from_str(&v)).transpose()?;
            let revision = old.as_ref().map(|d| d.revision).unwrap_or(0);
            if change.expected_revision.is_some_and(|expected| expected != revision) {
                return Err(StoreError::Conflict(format!("document {} changed", change.key)));
            }
            if let Some(value) = &change.value {
                let now = Utc::now();
                let doc = Document {
                    namespace: change.namespace.clone(), key: change.key.clone(), value: value.clone(),
                    revision: revision.checked_add(1).ok_or_else(|| StoreError::Storage("revision overflow".into()))?,
                    created_at: old.map(|d| d.created_at).unwrap_or(now), updated_at: now,
                };
                tx.execute("INSERT INTO memweft_documents VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT (tenant_id,user_id,agent_id,namespace,key)
                    DO UPDATE SET revision=excluded.revision, document=excluded.document",
                    params![scope.tenant_id, scope.user_id, scope.agent_id, ns, change.key, doc.revision, serde_json::to_string(&doc)?])?;
            } else {
                tx.execute("DELETE FROM memweft_documents WHERE tenant_id=? AND user_id=? AND agent_id=? AND namespace=? AND key=?",
                    params![scope.tenant_id, scope.user_id, scope.agent_id, ns, change.key])?;
            }
        }
        tx.commit()?;
        Ok(())
    })
}

/// Caller holds an IMMEDIATE transaction changing or deleting the fact. A
/// generation guard prevents in-flight evaluations from restoring stale material.
pub(crate) fn invalidate_private_sources(
    tx: &rusqlite::Transaction<'_>,
    scope: &Scope,
    key: &str,
) -> StoreResult<()> {
    let epoch = crate::learning_sources::epoch(tx, scope)?;
    let docs = crate::learning_sources::dependents(tx, scope, None, key)?;
    for (_, mut doc) in docs {
        // The generation is maintained separately, as in the scan implementation.
        if doc.namespace == ["learning", "epoch"] && doc.key == "current" {
            continue;
        }
        let namespace = serde_json::to_string(&doc.namespace)?;
        if doc.namespace == ["learning", "active"] {
            doc.value = Value::Null;
            doc.revision = doc
                .revision
                .checked_add(1)
                .ok_or_else(|| StoreError::Storage("revision overflow".into()))?;
            doc.updated_at = Utc::now();
            tx.execute("UPDATE memweft_documents SET revision=?,document=? WHERE tenant_id=? AND user_id=? AND agent_id=? AND namespace=? AND key=?",
                params![doc.revision,serde_json::to_string(&doc)?,scope.tenant_id,scope.user_id,scope.agent_id,namespace,doc.key])?;
        } else {
            tx.execute("DELETE FROM memweft_documents WHERE tenant_id=? AND user_id=? AND agent_id=? AND namespace=? AND key=?",
                params![scope.tenant_id,scope.user_id,scope.agent_id,namespace,doc.key])?;
        }
    }
    let now = Utc::now();
    let doc = Document {
        namespace: vec!["learning".into(), "epoch".into()],
        key: "current".into(),
        value: serde_json::json!({"invalidated":true}),
        revision: epoch
            .as_ref()
            .map(|d| d.revision)
            .unwrap_or(0)
            .checked_add(1)
            .ok_or_else(|| StoreError::Storage("revision overflow".into()))?,
        created_at: epoch.map(|d| d.created_at).unwrap_or(now),
        updated_at: now,
    };
    tx.execute("INSERT INTO memweft_documents VALUES (?,?,?,?,?,?,?) ON CONFLICT (tenant_id,user_id,agent_id,namespace,key) DO UPDATE SET revision=excluded.revision,document=excluded.document",
        params![scope.tenant_id,scope.user_id,scope.agent_id,serde_json::to_string(&doc.namespace)?,doc.key,doc.revision,serde_json::to_string(&doc)?])?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Store;
    use serde_json::json;

    fn scope() -> Scope {
        Scope {
            tenant_id: "t".into(),
            user_id: "u".into(),
            agent_id: "a".into(),
            session_id: String::new(),
            run_id: String::new(),
        }
    }

    #[test]
    fn namespace_ranges_match_component_prefixes_with_escaped_and_unicode_names() {
        let store = SqliteStore::new_in_memory().unwrap();
        let scope = scope();
        let names: Vec<Vec<String>> = vec![
            vec!["a"],
            vec!["a", "child"],
            vec!["ab"],
            vec!["a,b"],
            vec!["a]"],
            vec!["a\""],
            vec!["a\"", "x"],
            vec!["a\\"],
            vec!["a\\", "x"],
            vec!["中文"],
            vec!["中文", "子目录"],
            vec!["中文x"],
            vec!["a\n"],
            vec!["%_"],
            vec!["%_", "child"],
            vec!["其他"],
        ]
        .into_iter()
        .map(|ns| ns.into_iter().map(str::to_owned).collect())
        .collect();
        store
            .mutate_documents(
                &scope,
                &names
                    .iter()
                    .map(|namespace| Mutation {
                        namespace: namespace.clone(),
                        key: "k".into(),
                        value: Some(json!({})),
                        expected_revision: None,
                    })
                    .collect::<Vec<_>>(),
            )
            .unwrap();
        for prefix in std::iter::once(vec![]).chain(names.iter().cloned()) {
            let mut expected: Vec<_> = names
                .iter()
                .filter(|ns| ns.starts_with(&prefix))
                .cloned()
                .collect();
            let mut actual: Vec<_> = store
                .documents(&scope, &prefix)
                .unwrap()
                .into_iter()
                .map(|d| d.namespace)
                .collect();
            expected.sort();
            actual.sort();
            assert_eq!(actual, expected, "prefix {prefix:?}");
        }
        let other = Scope {
            agent_id: "other".into(),
            ..scope.clone()
        };
        assert!(store.documents(&other, &[]).unwrap().is_empty());
        assert!(
            store
                .document(&other, &["a".into()], "k")
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn recent_queries_preserve_nanosecond_order_ties_and_skip_unselected_payloads() {
        let store = SqliteStore::new_in_memory().unwrap();
        let scope = scope();
        let namespace = vec!["messages".into(), "s".into()];
        store
            .with_connection(|conn| {
                // Exercise migration from a populated pre-index document table.
                conn.execute_batch(
                    "DROP INDEX memweft_documents_recent; DROP INDEX memweft_documents_learning;",
                )?;
                for (key, ts) in [
                    ("z", "2026-09-27T00:00:00Z"),
                    ("b", "2026-09-27T00:00:00.100000001Z"),
                    ("a", "2026-09-27T00:00:00.100000001Z"),
                    ("c", "2026-09-27T00:00:00.100Z"),
                    ("d", "2026-09-27T00:00:01Z"),
                ] {
                    let created_at = ts.parse().unwrap();
                    let doc = Document {
                        namespace: namespace.clone(),
                        key: key.into(),
                        value: json!({}),
                        revision: 1,
                        created_at,
                        updated_at: created_at,
                    };
                    conn.execute(
                        "INSERT INTO memweft_documents VALUES (?,?,?,?,?,?,?)",
                        params![
                            scope.tenant_id,
                            scope.user_id,
                            scope.agent_id,
                            serde_json::to_string(&namespace)?,
                            key,
                            1,
                            serde_json::to_string(&doc)?
                        ],
                    )?;
                }
                // Valid JSON, intentionally not a Document. It must never be decoded
                // by a point lookup, another namespace, or a window excluding it.
                for ns in [&namespace, &vec!["unrelated".into()]] {
                    conn.execute(
                        "INSERT INTO memweft_documents VALUES (?,?,?,?,?,?,?)",
                        params![
                            scope.tenant_id,
                            scope.user_id,
                            scope.agent_id,
                            serde_json::to_string(ns)?,
                            "old-bad-payload",
                            1,
                            r#"{"created_at":"2000-01-01T00:00:00Z"}"#
                        ],
                    )?;
                }
                ensure_schema(conn)?;
                ensure_schema(conn)?;
                Ok(())
            })
            .unwrap();
        assert!(store.document(&scope, &namespace, "a").unwrap().is_some());
        assert!(
            store
                .document(&scope, &namespace, "missing")
                .unwrap()
                .is_none()
        );
        let recent = store.recent_documents(&scope, &namespace, 5, 1).unwrap();
        assert_eq!(
            recent
                .documents
                .iter()
                .map(|d| d.key.as_str())
                .collect::<Vec<_>>(),
            ["z", "c", "a", "b", "d"]
        );
        assert_eq!(recent.omitted_keys, ["old-bad-payload"]);
        assert!(!recent.omissions_truncated);
        let recent = store.recent_documents(&scope, &namespace, 2, 2).unwrap();
        assert_eq!(
            recent
                .documents
                .iter()
                .map(|d| d.key.as_str())
                .collect::<Vec<_>>(),
            ["b", "d"]
        );
        assert_eq!(recent.omitted_keys, ["old-bad-payload", "z"]);
        assert!(recent.omissions_truncated);
        let recent = store.recent_documents(&scope, &namespace, 0, 0).unwrap();
        assert!(recent.documents.is_empty());
        assert!(recent.omitted_keys.is_empty());
        assert!(recent.omissions_truncated);
        let empty = store
            .recent_documents(&scope, &["none".into()], 0, 0)
            .unwrap();
        assert!(!empty.omissions_truncated);
        assert!(store.documents(&scope, &[]).is_err());
    }
}
