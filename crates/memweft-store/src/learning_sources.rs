//! Exact reverse index of explicitly declared learning-document dependencies.
//! Extraction preserves the previous recursive source_keys/source_pools rules.
use crate::{Document, Scope, StoreError, StoreResult};
use rusqlite::functions::FunctionFlags;
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};
use serde_json::Value;
use std::collections::BTreeSet;

fn collect(value: &Value, sources: &mut BTreeSet<(u8, String, String)>) {
    match value {
        Value::Object(map) => {
            for (name, value) in map {
                if let Some(items) = value.as_array() {
                    if name == "source_keys" {
                        for key in items.iter().filter_map(Value::as_str) {
                            sources.insert((0, String::new(), key.into()));
                        }
                    } else if name == "source_pools" || name == "pool_revisions" {
                        for item in items {
                            if let (Some(pool), Some(key)) = (
                                item.get("pool_id").and_then(Value::as_str),
                                item.get("key").and_then(Value::as_str),
                            ) {
                                sources.insert((1, pool.into(), key.into()));
                            }
                        }
                    }
                }
                collect(value, sources);
            }
        }
        Value::Array(items) => {
            for item in items {
                collect(item, sources);
            }
        }
        _ => {}
    }
}

pub(crate) fn register(conn: &Connection) -> rusqlite::Result<()> {
    conn.create_scalar_function(
        "memweft_learning_sources_v1",
        1,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| {
            let encoded: String = ctx.get(0)?;
            let document: Document = serde_json::from_str(&encoded)
                .map_err(|e| rusqlite::Error::UserFunctionError(Box::new(e)))?;
            let mut sources = BTreeSet::new();
            collect(&document.value, &mut sources);
            Ok(serde_json::to_string(&sources).expect("source tuples serialize to JSON"))
        },
    )
}

fn version(conn: &Connection) -> StoreResult<Option<i64>> {
    let exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='memweft_learning_source_version')",
        [],
        |r| r.get(0),
    )?;
    if !exists {
        return Ok(None);
    }
    let version = conn
        .query_row(
            "SELECT version FROM memweft_learning_source_version",
            [],
            |r| r.get(0),
        )
        .optional()?;
    if version != Some(1) {
        return Err(StoreError::Storage(
            "unsupported learning source index version".into(),
        ));
    }
    Ok(version)
}

pub(crate) fn ensure_schema(conn: &Connection) -> StoreResult<()> {
    if version(conn)? == Some(1) {
        return Ok(());
    }
    let tx = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
    if version(&tx)? == Some(1) {
        return Ok(());
    }
    tx.execute_batch(
        "CREATE TABLE memweft_learning_sources (
            tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
            kind INTEGER NOT NULL, pool_id TEXT NOT NULL, source_key TEXT NOT NULL,
            agent_id TEXT NOT NULL, namespace TEXT NOT NULL, document_key TEXT NOT NULL,
            PRIMARY KEY (tenant_id,user_id,kind,pool_id,source_key,agent_id,namespace,document_key),
            FOREIGN KEY (tenant_id,user_id,agent_id,namespace,document_key)
              REFERENCES memweft_documents(tenant_id,user_id,agent_id,namespace,key) ON DELETE CASCADE
        ) WITHOUT ROWID;
        CREATE INDEX memweft_learning_source_owner ON memweft_learning_sources
            (tenant_id,user_id,agent_id,namespace,document_key);
        CREATE TABLE memweft_learning_source_version (version INTEGER NOT NULL);
        INSERT INTO memweft_learning_source_version VALUES (1);"
    )?;
    let insert = "INSERT INTO memweft_learning_sources
        SELECT NEW.tenant_id,NEW.user_id,json_extract(s.value,'$[0]'),json_extract(s.value,'$[1]'),json_extract(s.value,'$[2]'),
          NEW.agent_id,NEW.namespace,NEW.key FROM json_each(memweft_learning_sources_v1(NEW.document)) AS s;";
    let learning = "NEW.namespace >= '[\"learning\",' AND NEW.namespace < '[\"learning\"^'";
    tx.execute_batch(&format!(
        "CREATE TRIGGER memweft_learning_source_insert AFTER INSERT ON memweft_documents WHEN {learning}
          BEGIN {insert} END;
        CREATE TRIGGER memweft_learning_source_before_update BEFORE UPDATE ON memweft_documents
          WHEN OLD.namespace >= '[\"learning\",' AND OLD.namespace < '[\"learning\"^'
          BEGIN DELETE FROM memweft_learning_sources WHERE tenant_id=OLD.tenant_id AND user_id=OLD.user_id
            AND agent_id=OLD.agent_id AND namespace=OLD.namespace AND document_key=OLD.key; END;
        CREATE TRIGGER memweft_learning_source_update AFTER UPDATE ON memweft_documents WHEN {learning}
          BEGIN {insert} END;"
    ))?;
    tx.execute_batch(
        "INSERT INTO memweft_learning_sources
        SELECT d.tenant_id,d.user_id,json_extract(s.value,'$[0]'),json_extract(s.value,'$[1]'),json_extract(s.value,'$[2]'),
          d.agent_id,d.namespace,d.key FROM memweft_documents AS d,
          json_each(memweft_learning_sources_v1(d.document)) AS s
        WHERE d.namespace >= '[\"learning\",' AND d.namespace < '[\"learning\"^';"
    )?;
    tx.commit()?;
    Ok(())
}

/// Read only owners of this declared dependency, in the caller's write transaction.
/// Shared sources reach all actors for this tenant/user; private sources one actor.
pub(crate) fn dependents(
    tx: &Transaction<'_>,
    scope: &Scope,
    pool: Option<&str>,
    key: &str,
) -> StoreResult<Vec<(String, Document)>> {
    let private = if pool.is_none() {
        "AND s.agent_id=?6"
    } else {
        ""
    };
    let mut stmt = tx.prepare_cached(&format!(
        "SELECT d.agent_id,d.document FROM memweft_learning_sources AS s
        JOIN memweft_documents AS d ON d.tenant_id=s.tenant_id AND d.user_id=s.user_id
          AND d.agent_id=s.agent_id AND d.namespace=s.namespace AND d.key=s.document_key
        WHERE s.tenant_id=?1 AND s.user_id=?2 AND s.kind=?3 AND s.pool_id=?4 AND s.source_key=?5 {private}"
    ))?;
    let mut values = vec![
        scope.tenant_id.clone().into(),
        scope.user_id.clone().into(),
        rusqlite::types::Value::Integer(i64::from(pool.is_some())),
        pool.unwrap_or("").to_string().into(),
        key.to_string().into(),
    ];
    if pool.is_none() {
        values.push(scope.agent_id.clone().into());
    }
    let rows = stmt.query_map(rusqlite::params_from_iter(values), |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
    })?;
    rows.map(|row| {
        let (agent, encoded) = row?;
        Ok((agent, serde_json::from_str(&encoded)?))
    })
    .collect()
}

pub(crate) fn epoch(tx: &Transaction<'_>, scope: &Scope) -> StoreResult<Option<Document>> {
    let encoded: Option<String> = tx.prepare_cached("SELECT document FROM memweft_documents
      WHERE tenant_id=? AND user_id=? AND agent_id=? AND namespace='[\"learning\",\"epoch\"]' AND key='current'")?
        .query_row(params![scope.tenant_id,scope.user_id,scope.agent_id], |r| r.get(0)).optional()?;
    encoded
        .map(|s| serde_json::from_str(&s).map_err(StoreError::from))
        .transpose()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Mutation, SqliteStore, Store};
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
    fn change(key: &str, value: Option<Value>) -> Mutation {
        Mutation {
            namespace: vec!["learning".into(), "versions".into()],
            key: key.into(),
            value,
            expected_revision: None,
        }
    }
    fn owners(
        store: &SqliteStore,
        scope: &Scope,
        pool: Option<&str>,
        key: &str,
    ) -> BTreeSet<(String, String)> {
        store
            .with_connection(|conn| {
                let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
                Ok(dependents(&tx, scope, pool, key)?
                    .into_iter()
                    .map(|(a, d)| (a, d.key))
                    .collect())
            })
            .unwrap()
    }
    // Kept independently from extraction: the previous scan's membership rule.
    fn scan(value: &Value, pool: Option<&str>, key: &str) -> bool {
        match value {
            Value::Object(map) => map.iter().any(|(name, value)| {
                let direct = match pool {
                    None => {
                        name == "source_keys"
                            && value.as_array().is_some_and(|a| a.iter().any(|v| v == key))
                    }
                    Some(pool) => {
                        (name == "source_pools" || name == "pool_revisions")
                            && value.as_array().is_some_and(|a| {
                                a.iter().any(|v| v["pool_id"] == pool && v["key"] == key)
                            })
                    }
                };
                direct || scan(value, pool, key)
            }),
            Value::Array(a) => a.iter().any(|v| scan(v, pool, key)),
            _ => false,
        }
    }

    #[test]
    fn indexed_dependencies_match_recursive_scan_across_shapes_and_scopes() {
        let store = SqliteStore::new_in_memory().unwrap();
        let s = scope();
        let mut values = vec![
            Value::Null,
            json!({}),
            json!({"source_keys":["port","port",null,42,"","中文\"\\"]}),
            json!({"source_pools":[{"pool_id":"team","key":"port"},{"pool_id":"team","key":"port"},null,42,{}]}),
            json!({"baseline":{"proposal":{"source_keys":["port"]},"pool_revisions":[{"pool_id":"team","key":"port","revision":7}]}}),
            json!({"source_keys":"port","source_pools":{"pool_id":"team","key":"port"}}),
            json!({"source_pools":[{"pool_id":"private","key":"port"}]}),
            json!({"pool_revisions":[{"pool_id":"","key":""}]}),
        ];
        for i in 0..32 {
            values.push(json!({"nested":[{"source_keys":[format!("key-{}",i%3)]},{"proposal":{"source_pools":[{"pool_id":format!("pool-{}",i%2),"key":format!("key-{}",i%3)}]}}]}));
        }
        let scopes = [
            s.clone(),
            Scope {
                agent_id: "b".into(),
                ..s.clone()
            },
            Scope {
                user_id: "other".into(),
                ..s.clone()
            },
            Scope {
                tenant_id: "other".into(),
                ..s.clone()
            },
        ];
        for scope in &scopes {
            for (i, value) in values.iter().enumerate() {
                store
                    .mutate_documents(scope, &[change(&format!("v{i}"), Some(value.clone()))])
                    .unwrap();
            }
            let mut unrelated = change("not-learning", Some(json!({"source_keys":["port"]})));
            unrelated.namespace = vec!["learning-other".into()];
            store.mutate_documents(scope, &[unrelated]).unwrap();
        }
        for pool in [
            None,
            Some("team"),
            Some("private"),
            Some(""),
            Some("pool-0"),
            Some("pool-1"),
        ] {
            for key in ["port", "", "中文\"\\", "key-0", "key-1", "key-2", "absent"] {
                let expected: BTreeSet<_> = scopes
                    .iter()
                    .filter(|t| {
                        t.tenant_id == s.tenant_id
                            && t.user_id == s.user_id
                            && (pool.is_some() || t.agent_id == s.agent_id)
                    })
                    .flat_map(|t| {
                        values
                            .iter()
                            .enumerate()
                            .filter(move |(_, v)| scan(v, pool, key))
                            .map(move |(i, _)| (t.agent_id.clone(), format!("v{i}")))
                    })
                    .collect();
                assert_eq!(owners(&store, &s, pool, key), expected, "{pool:?}/{key}");
            }
        }
    }

    #[test]
    fn replacement_delete_cas_failure_and_missing_function_keep_index_atomic() {
        let store = SqliteStore::new_in_memory().unwrap();
        let s = scope();
        store
            .mutate_documents(&s, &[change("v", Some(json!({"source_keys":["old"]})))])
            .unwrap();
        let mut fail = change("missing", Some(json!({})));
        fail.expected_revision = Some(7);
        assert!(
            store
                .mutate_documents(&s, &[
                    change("v", Some(json!({"source_keys":["new"]}))),
                    fail
                ])
                .is_err()
        );
        assert_eq!(owners(&store, &s, None, "old").len(), 1);
        assert!(owners(&store, &s, None, "new").is_empty());
        store
            .with_connection(|conn| {
                conn.remove_function("memweft_learning_sources_v1", 1)?;
                Ok(())
            })
            .unwrap();
        assert!(
            store
                .mutate_documents(&s, &[change("v", Some(json!({"source_keys":["new"]})))])
                .is_err()
        );
        assert_eq!(owners(&store, &s, None, "old").len(), 1);
        store
            .with_connection(|conn| {
                register(conn)?;
                Ok(())
            })
            .unwrap();
        store
            .mutate_documents(&s, &[change("v", Some(json!({"source_keys":["new"]})))])
            .unwrap();
        assert!(owners(&store, &s, None, "old").is_empty());
        assert_eq!(owners(&store, &s, None, "new").len(), 1);
        store.mutate_documents(&s, &[change("v", None)]).unwrap();
        assert!(owners(&store, &s, None, "new").is_empty());
        store
            .with_connection(|conn| {
                assert!(!conn.prepare("PRAGMA foreign_key_check")?.exists([])?);
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn migration_backfills_once_and_rolls_back_every_index_object_on_bad_document() {
        let store = SqliteStore::new_in_memory().unwrap();
        let s = scope();
        store.mutate_documents(&s,&[change("v",Some(json!({"baseline":{"source_keys":["port"]},"pool_revisions":[{"pool_id":"team","key":"port"}]})))]).unwrap();
        store.with_connection(|conn| {
            conn.execute_batch("DROP TRIGGER memweft_learning_source_insert; DROP TRIGGER memweft_learning_source_before_update;
                DROP TRIGGER memweft_learning_source_update; DROP TABLE memweft_learning_sources; DROP TABLE memweft_learning_source_version;
                INSERT INTO memweft_documents VALUES ('t','u','a','[\"learning\",\"versions\"]','bad',1,'{}');")?;
            assert!(ensure_schema(conn).is_err());
            let count:i64=conn.query_row("SELECT count(*) FROM sqlite_master WHERE name LIKE 'memweft_learning_source%'",[],|r|r.get(0))?;
            assert_eq!(count,0);
            conn.execute("DELETE FROM memweft_documents WHERE key='bad'",[])?;
            ensure_schema(conn)?; ensure_schema(conn)?;
            let count:i64=conn.query_row("SELECT count(*) FROM memweft_learning_sources",[],|r|r.get(0))?;
            assert_eq!(count,2);
            conn.execute("UPDATE memweft_learning_source_version SET version=99",[])?;
            assert!(ensure_schema(conn).is_err());
            conn.execute("UPDATE memweft_learning_source_version SET version=1",[])?;
            Ok(())
        }).unwrap();
        assert_eq!(owners(&store, &s, None, "port").len(), 1);
        assert_eq!(owners(&store, &s, Some("team"), "port").len(), 1);
    }

    #[test]
    fn dependency_lookup_skips_unrelated_payloads_and_uses_source_key_index() {
        let store = SqliteStore::new_in_memory().unwrap();
        let s = scope();
        store
            .mutate_documents(&s, &[change("good", Some(json!({"source_keys":["port"]})))])
            .unwrap();
        store.with_connection(|conn| {
            // Corruption is only a read-work probe, not a supported input format.
            conn.execute_batch("DROP TRIGGER memweft_learning_source_insert;
                INSERT INTO memweft_documents VALUES ('t','u','a','[\"learning\",\"versions\"]','bad',1,'{}');")?;
            let plan:String=conn.query_row("EXPLAIN QUERY PLAN SELECT namespace,document_key FROM memweft_learning_sources
                WHERE tenant_id='t' AND user_id='u' AND kind=0 AND pool_id='' AND source_key='port' AND agent_id='a'",[],|r|r.get(3))?;
            assert!(plan.contains("SEARCH") && plan.contains("source_key=?"),"{plan}");
            Ok(())
        }).unwrap();
        assert_eq!(owners(&store, &s, None, "port").len(), 1);
        assert!(owners(&store, &s, None, "absent").is_empty());
        assert!(store.documents(&s, &["learning".into()]).is_err());
    }
}
