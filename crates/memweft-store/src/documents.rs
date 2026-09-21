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
    Ok(())
}

pub(crate) fn list(
    store: &SqliteStore,
    scope: &Scope,
    prefix: &[String],
) -> StoreResult<Vec<Document>> {
    store.with_connection(|conn| {
        let mut stmt = conn.prepare(
            "SELECT document FROM memweft_documents
            WHERE tenant_id=? AND user_id=? AND agent_id=? ORDER BY namespace, key",
        )?;
        let rows = stmt.query_map(
            params![scope.tenant_id, scope.user_id, scope.agent_id],
            |r| r.get::<_, String>(0),
        )?;
        let mut docs = Vec::new();
        for row in rows {
            let doc: Document = serde_json::from_str(&row?)?;
            if doc.namespace.starts_with(prefix) {
                docs.push(doc);
            }
        }
        Ok(docs)
    })
}

pub(crate) fn mutate(
    store: &SqliteStore,
    scope: &Scope,
    mutations: &[Mutation],
) -> StoreResult<()> {
    store.with_connection(|conn| {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
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

fn references(value: &Value, key: &str) -> bool {
    match value {
        Value::Object(map) => map.iter().any(|(name, value)| {
            (name == "source_keys"
                && value
                    .as_array()
                    .is_some_and(|keys| keys.iter().any(|v| v == key)))
                || references(value, key)
        }),
        Value::Array(items) => items.iter().any(|v| references(v, key)),
        _ => false,
    }
}

/// Caller holds an IMMEDIATE transaction also deleting the fact. A generation
/// guard prevents in-flight evaluations from restoring forgotten material.
pub(crate) fn forget_derived(
    tx: &rusqlite::Transaction<'_>,
    scope: &Scope,
    key: &str,
) -> StoreResult<()> {
    let mut stmt = tx.prepare(
        "SELECT document FROM memweft_documents WHERE tenant_id=? AND user_id=? AND agent_id=?",
    )?;
    let rows = stmt.query_map(
        params![scope.tenant_id, scope.user_id, scope.agent_id],
        |r| r.get::<_, String>(0),
    )?;
    let mut docs = Vec::new();
    for row in rows {
        docs.push(serde_json::from_str::<Document>(&row?)?);
    }
    drop(stmt);
    let mut epoch = None;
    for mut doc in docs {
        if doc.namespace.first().map(String::as_str) != Some("learning") {
            continue;
        }
        if doc.namespace == ["learning", "epoch"] && doc.key == "current" {
            epoch = Some(doc);
            continue;
        }
        if references(&doc.value, key) {
            let namespace = serde_json::to_string(&doc.namespace)?;
            if doc.namespace == ["learning", "active"] {
                doc.value = Value::Null;
                doc.revision += 1;
                doc.updated_at = Utc::now();
                tx.execute("UPDATE memweft_documents SET revision=?,document=? WHERE tenant_id=? AND user_id=? AND agent_id=? AND namespace=? AND key=?",
                    params![doc.revision,serde_json::to_string(&doc)?,scope.tenant_id,scope.user_id,scope.agent_id,namespace,doc.key])?;
            } else {
                tx.execute("DELETE FROM memweft_documents WHERE tenant_id=? AND user_id=? AND agent_id=? AND namespace=? AND key=?",
                    params![scope.tenant_id,scope.user_id,scope.agent_id,namespace,doc.key])?;
            }
        }
    }
    let now = Utc::now();
    let doc = Document {
        namespace: vec!["learning".into(), "epoch".into()],
        key: "current".into(),
        value: serde_json::json!({"invalidated":true}),
        revision: epoch.as_ref().map(|d| d.revision + 1).unwrap_or(1),
        created_at: epoch.map(|d| d.created_at).unwrap_or(now),
        updated_at: now,
    };
    tx.execute("INSERT INTO memweft_documents VALUES (?,?,?,?,?,?,?) ON CONFLICT (tenant_id,user_id,agent_id,namespace,key) DO UPDATE SET revision=excluded.revision,document=excluded.document",
        params![scope.tenant_id,scope.user_id,scope.agent_id,serde_json::to_string(&doc.namespace)?,doc.key,doc.revision,serde_json::to_string(&doc)?])?;
    Ok(())
}
