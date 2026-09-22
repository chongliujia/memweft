//! Shared facts have their own tenant/user/pool keyspace, separate from actors.
use crate::{Scope, SqliteStore, StoreError, StoreResult};
use memweft_types::Fact;
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PoolFact {
    #[serde(flatten)]
    pub fact: Fact,
    pub pool_id: String,
    /// Private legacy facts have no revision; shared facts have monotonic revisions.
    pub revision: Option<u64>,
    pub writer_agent_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[serde(deny_unknown_fields)]
pub struct PoolRef {
    pub pool_id: String,
    pub key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PoolRevision {
    pub pool_id: String,
    pub key: String,
    pub revision: u64,
}

pub(crate) fn ensure_schema(conn: &Connection) -> StoreResult<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS memweft_pool_facts (
        tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, pool_id TEXT NOT NULL,
        fact_key TEXT NOT NULL, revision INTEGER NOT NULL, record TEXT,
        PRIMARY KEY (tenant_id, user_id, pool_id, fact_key)
    );",
    )?;
    Ok(())
}

pub(crate) fn list(store: &SqliteStore, scope: &Scope, pool: &str) -> StoreResult<Vec<PoolFact>> {
    store.with_connection(|conn| {
        let mut stmt = conn.prepare(
            "SELECT record FROM memweft_pool_facts
            WHERE tenant_id=? AND user_id=? AND pool_id=? AND record IS NOT NULL ORDER BY fact_key",
        )?;
        let rows = stmt.query_map(params![scope.tenant_id, scope.user_id, pool], |r| {
            r.get::<_, String>(0)
        })?;
        rows.map(|row| Ok(serde_json::from_str(&row?)?)).collect()
    })
}

pub(crate) fn check_revisions(
    tx: &Transaction<'_>,
    scope: &Scope,
    guards: &[PoolRevision],
) -> StoreResult<()> {
    for guard in guards {
        let current: Option<u64> = tx
            .query_row(
                "SELECT revision FROM memweft_pool_facts
            WHERE tenant_id=? AND user_id=? AND pool_id=? AND fact_key=? AND record IS NOT NULL",
                params![scope.tenant_id, scope.user_id, guard.pool_id, guard.key],
                |r| r.get(0),
            )
            .optional()?;
        if current != Some(guard.revision) {
            return Err(StoreError::Conflict(format!(
                "source pool {} key {} changed",
                guard.pool_id, guard.key
            )));
        }
    }
    Ok(())
}

fn references(value: &Value, pool: &str, key: &str) -> bool {
    match value {
        Value::Object(map) => map.iter().any(|(name, value)| {
            ((name == "source_pools" || name == "pool_revisions")
                && value.as_array().is_some_and(|refs| {
                    refs.iter().any(|r| r["pool_id"] == pool && r["key"] == key)
                }))
                || references(value, pool, key)
        }),
        Value::Array(values) => values.iter().any(|v| references(v, pool, key)),
        _ => false,
    }
}

/// Remove declared derivatives across actors, within this tenant and user only.
/// Active pointers retain a revision tombstone, so in-flight updates conflict.
fn invalidate(tx: &Transaction<'_>, scope: &Scope, pool: &str, key: &str) -> StoreResult<()> {
    let mut stmt = tx.prepare(
        "SELECT agent_id, document FROM memweft_documents
        WHERE tenant_id=? AND user_id=?",
    )?;
    let rows = stmt.query_map(params![scope.tenant_id, scope.user_id], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
    })?;
    let mut documents = Vec::new();
    for row in rows {
        let (agent, value) = row?;
        documents.push((agent, serde_json::from_str::<crate::Document>(&value)?));
    }
    drop(stmt);
    for (agent, mut doc) in documents {
        if doc.namespace.first().map(String::as_str) != Some("learning")
            || !references(&doc.value, pool, key)
        {
            continue;
        }
        let namespace = serde_json::to_string(&doc.namespace)?;
        if doc.namespace == ["learning", "active"] {
            doc.value = Value::Null;
            doc.revision = doc
                .revision
                .checked_add(1)
                .ok_or_else(|| StoreError::Storage("revision overflow".into()))?;
            doc.updated_at = chrono::Utc::now();
            tx.execute(
                "UPDATE memweft_documents SET revision=?, document=?
                WHERE tenant_id=? AND user_id=? AND agent_id=? AND namespace=? AND key=?",
                params![
                    doc.revision,
                    serde_json::to_string(&doc)?,
                    scope.tenant_id,
                    scope.user_id,
                    agent,
                    namespace,
                    doc.key
                ],
            )?;
        } else {
            tx.execute(
                "DELETE FROM memweft_documents
                WHERE tenant_id=? AND user_id=? AND agent_id=? AND namespace=? AND key=?",
                params![scope.tenant_id, scope.user_id, agent, namespace, doc.key],
            )?;
        }
    }
    Ok(())
}

pub(crate) fn write(
    store: &SqliteStore,
    scope: &Scope,
    pool: &str,
    key: &str,
    fact: Option<Fact>,
    expected: Option<u64>,
) -> StoreResult<Option<PoolFact>> {
    if pool.trim().is_empty() || pool == "private" || key.trim().is_empty() {
        return Err(StoreError::InvalidInput(
            "shared pool ID and key must be nonempty; private is reserved".into(),
        ));
    }
    store.with_connection(|conn| {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let old: Option<(u64, Option<String>)> = tx
            .query_row(
                "SELECT revision, record FROM memweft_pool_facts
            WHERE tenant_id=? AND user_id=? AND pool_id=? AND fact_key=?",
                params![scope.tenant_id, scope.user_id, pool, key],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        let present = old.as_ref().is_some_and(|(_, record)| record.is_some());
        let revision = old.as_ref().map(|(rev, _)| *rev).unwrap_or(0);
        // 0 means create-if-absent, including after deletion; positive revisions
        // require a live record. The retained counter prevents delete/recreate ABA.
        if expected.is_some_and(|rev| {
            if rev == 0 {
                present
            } else {
                !present || rev != revision
            }
        }) {
            return Err(StoreError::Conflict(format!(
                "pool {pool} key {key} changed"
            )));
        }
        if fact.is_none() && !present {
            return Ok(None);
        }
        let next = revision
            .checked_add(1)
            .ok_or_else(|| StoreError::Storage("revision overflow".into()))?;
        let record = fact.map(|fact| PoolFact {
            fact,
            pool_id: pool.into(),
            revision: Some(next),
            writer_agent_id: scope.agent_id.clone(),
        });
        let encoded = record.as_ref().map(serde_json::to_string).transpose()?;
        tx.prepare_cached(
            "INSERT INTO memweft_pool_facts (tenant_id,user_id,pool_id,fact_key,revision,record)
            VALUES (?,?,?,?,?,?) ON CONFLICT (tenant_id,user_id,pool_id,fact_key)
            DO UPDATE SET revision=excluded.revision, record=excluded.record",
        )?.execute(params![scope.tenant_id, scope.user_id, pool, key, next, encoded])?;
        if present {
            invalidate(&tx, scope, pool, key)?;
        }
        tx.commit()?;
        // A deletion returns the removed record so the wrapper can return a bool.
        match record {
            Some(record) => Ok(Some(record)),
            None => old
                .and_then(|(_, v)| v)
                .map(|v| serde_json::from_str(&v).map_err(StoreError::from))
                .transpose(),
        }
    })
}
