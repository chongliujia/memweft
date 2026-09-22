//! Transactionally maintained, scoped inverted index. Query scoring is exactly
//! lexical_overlap_v1; this is not BM25, WAND, or a semantic retrieval backend.
use crate::{PoolFact, Scope, SqliteStore, StoreError, StoreResult, lexical};
use rusqlite::functions::FunctionFlags;
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet, HashSet};

pub const DIAGNOSTIC_LIMIT: usize = 64;

pub struct RecallCandidates {
    pub records: Vec<PoolFact>,
    pub shadowed: Vec<Value>,
    pub has_more: bool,
    pub shadowed_truncated: bool,
    /// Execution plan, not the scoring method or a physical index-visit count.
    pub ranking_plan: &'static str,
}

pub(crate) fn register(conn: &Connection) -> rusqlite::Result<()> {
    conn.create_scalar_function(
        "memweft_recall_terms_v1",
        2,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| {
            let key: String = ctx.get(0)?;
            let value: String = ctx.get(1)?;
            let value: Value = serde_json::from_str(&value)
                .map_err(|e| rusqlite::Error::UserFunctionError(Box::new(e)))?;
            let mut weights = BTreeMap::<String, u8>::new();
            for term in lexical::terms(&key) {
                *weights.entry(term).or_default() += 2;
            }
            for term in lexical::terms(&value.to_string()) {
                *weights.entry(term).or_default() += 1;
            }
            Ok(serde_json::to_string(&weights).expect("string/integer map is JSON"))
        },
    )
}

fn version(conn: &Connection) -> StoreResult<Option<i64>> {
    let exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='memweft_recall_version')",
        [],
        |r| r.get(0),
    )?;
    if !exists {
        return Ok(None);
    }
    let version: Option<i64> = conn
        .query_row("SELECT version FROM memweft_recall_version", [], |r| {
            r.get(0)
        })
        .optional()?;
    if !matches!(version, Some(1 | 2)) {
        return Err(StoreError::Storage(
            "unsupported recall index version".into(),
        ));
    }
    Ok(version)
}

// The triggers are deliberately in the same SQLite transaction as every source
// mutation, including low-level Store writes. A client missing the versioned
// tokenizer fails its write rather than silently leaving stale index entries.
pub(crate) fn ensure_schema(conn: &Connection) -> StoreResult<()> {
    if version(conn)? == Some(2) {
        return Ok(());
    }
    let tx = Transaction::new_unchecked(conn, TransactionBehavior::Immediate)?;
    let previous = version(&tx)?;
    if previous == Some(2) {
        return Ok(());
    }
    if previous == Some(1) {
        // Same postings and weights, reordered to expose the maximum weight of
        // a scoped term in a single B-tree seek. No additional full-size index.
        for table in ["facts", "memweft_pool_facts"] {
            for op in ["insert", "update", "delete"] {
                tx.execute_batch(&format!("DROP TRIGGER memweft_recall_{table}_{op}"))?;
            }
        }
        tx.execute_batch(
            "CREATE TABLE memweft_recall_terms_v2(
              tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,agent_id TEXT NOT NULL,
              pool_id TEXT NOT NULL,term TEXT NOT NULL,item_id INTEGER NOT NULL,
              weight INTEGER NOT NULL,PRIMARY KEY(tenant_id,user_id,agent_id,pool_id,term,weight DESC,item_id),
              FOREIGN KEY(item_id) REFERENCES memweft_recall_items(id) ON DELETE CASCADE
            ) WITHOUT ROWID;
            INSERT INTO memweft_recall_terms_v2 SELECT * FROM memweft_recall_terms;
            DROP TABLE memweft_recall_terms;
            ALTER TABLE memweft_recall_terms_v2 RENAME TO memweft_recall_terms;
            CREATE INDEX memweft_recall_term_item ON memweft_recall_terms(item_id);
            UPDATE memweft_recall_version SET version=2;",
        )?;
        install_triggers(&tx, false)?;
        tx.commit()?;
        return Ok(());
    }
    tx.execute_batch(
        "CREATE TABLE memweft_recall_version(version INTEGER PRIMARY KEY);
        CREATE TABLE memweft_recall_items(
          id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,
          agent_id TEXT NOT NULL,pool_id TEXT NOT NULL,identity TEXT NOT NULL,
          fact_id TEXT NOT NULL,fact_key TEXT NOT NULL,active INTEGER NOT NULL,
          valid_from INTEGER,valid_to INTEGER,
          UNIQUE(tenant_id,user_id,agent_id,pool_id,identity));
        CREATE INDEX memweft_recall_order ON memweft_recall_items(
          tenant_id,user_id,agent_id,pool_id,fact_key,fact_id);
        CREATE TABLE memweft_recall_terms(
          tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,agent_id TEXT NOT NULL,
          pool_id TEXT NOT NULL,term TEXT NOT NULL,item_id INTEGER NOT NULL,
          weight INTEGER NOT NULL,PRIMARY KEY(tenant_id,user_id,agent_id,pool_id,term,weight DESC,item_id),
          FOREIGN KEY(item_id) REFERENCES memweft_recall_items(id) ON DELETE CASCADE
        ) WITHOUT ROWID;
        CREATE INDEX memweft_recall_term_item ON memweft_recall_terms(item_id);",
    )?;
    install_triggers(&tx, true)?;
    tx.execute("INSERT INTO memweft_recall_version VALUES(2)", [])?;
    tx.commit()?;
    Ok(())
}

fn install_triggers(tx: &Transaction<'_>, backfill: bool) -> StoreResult<()> {
    for (table, shared) in [("facts", false), ("memweft_pool_facts", true)] {
        let delete = if shared {
            "DELETE FROM memweft_recall_items WHERE tenant_id=old.tenant_id AND user_id=old.user_id
             AND agent_id='' AND pool_id=old.pool_id AND identity=old.fact_key;"
        } else {
            "DELETE FROM memweft_recall_items WHERE tenant_id=old.tenant_id AND user_id=old.user_id
             AND agent_id=old.agent_id AND pool_id='private' AND identity=old.fact_id;"
        };
        let insert = insert_sql("new", "", shared);
        tx.execute_batch(&format!("
            CREATE TRIGGER memweft_recall_{table}_insert AFTER INSERT ON {table} BEGIN {insert} END;
            CREATE TRIGGER memweft_recall_{table}_update AFTER UPDATE ON {table} BEGIN {delete} {insert} END;
            CREATE TRIGGER memweft_recall_{table}_delete AFTER DELETE ON {table} BEGIN {delete} END;
        "))?;
        if backfill {
            tx.execute_batch(&insert_sql("f", &format!("FROM {table} f"), shared))?;
        }
    }
    Ok(())
}

fn insert_sql(alias: &str, from: &str, shared: bool) -> String {
    let a = alias;
    let (agent, pool, identity, fact_id, key, value, active, valid_from, valid_to, present) =
        if shared {
            (
                "''".into(),
                format!("{a}.pool_id"),
                format!("{a}.fact_key"),
                format!("json_extract({a}.record,'$.fact_id')"),
                format!("{a}.fact_key"),
                format!("{a}.record -> '$.value'"),
                "1".into(),
                "NULL".into(),
                "NULL".into(),
                format!("{a}.record IS NOT NULL"),
            )
        } else {
            (
                format!("{a}.agent_id"),
                "'private'".into(),
                format!("{a}.fact_id"),
                format!("{a}.fact_id"),
                format!("{a}.fact_key"),
                format!("{a}.value_json"),
                format!("{a}.status='active'"),
                format!("{a}.valid_from"),
                format!("{a}.valid_to"),
                "1".into(),
            )
        };
    // Shared legacy listing exposes live records regardless of embedded validity;
    // retain that behavior. Private facts preserve status and query-time validity.
    let token_from = if from.is_empty() {
        String::new()
    } else {
        format!("{from} CROSS JOIN ")
    };
    let token_from = if from.is_empty() {
        "FROM ".to_string()
    } else {
        token_from
    };
    format!("INSERT INTO memweft_recall_items(tenant_id,user_id,agent_id,pool_id,identity,fact_id,fact_key,active,valid_from,valid_to)
      SELECT {a}.tenant_id,{a}.user_id,{agent},{pool},{identity},{fact_id},{key},{active},{valid_from},{valid_to} {from} WHERE {present};
      INSERT INTO memweft_recall_terms(tenant_id,user_id,agent_id,pool_id,term,item_id,weight)
      SELECT {a}.tenant_id,{a}.user_id,{agent},{pool},t.key,
        (SELECT id FROM memweft_recall_items WHERE tenant_id={a}.tenant_id AND user_id={a}.user_id
         AND agent_id={agent} AND pool_id={pool} AND identity={identity}),t.value
      {token_from}json_each(memweft_recall_terms_v1({key},{value})) t WHERE {present};")
}

fn read_record(tx: &Transaction<'_>, id: i64) -> StoreResult<PoolFact> {
    let pool: String = tx.prepare_cached(
        "SELECT pool_id FROM memweft_recall_items WHERE id=?",
    )?.query_row([id],
        |r| r.get(0),
    )?;
    if pool == "private" {
        let fact = tx.prepare_cached("SELECT f.fact_id,f.fact_key,f.value_json,f.status,f.valid_from,f.valid_to,
           f.confidence,f.sources,f.scope_level,f.notes FROM memweft_recall_items i
           JOIN facts f ON f.tenant_id=i.tenant_id AND f.user_id=i.user_id AND f.agent_id=i.agent_id AND f.fact_id=i.identity
           WHERE i.id=?")?.query_row([id], crate::sqlite::decode_fact_row)?;
        let agent = tx.prepare_cached(
            "SELECT agent_id FROM memweft_recall_items WHERE id=?",
        )?.query_row([id],
            |r| r.get(0),
        )?;
        Ok(PoolFact {
            fact,
            pool_id: pool,
            revision: None,
            writer_agent_id: agent,
        })
    } else {
        let value: String = tx.prepare_cached("SELECT f.record FROM memweft_recall_items i
           JOIN memweft_pool_facts f ON f.tenant_id=i.tenant_id AND f.user_id=i.user_id AND f.pool_id=i.pool_id AND f.fact_key=i.identity
           WHERE i.id=?")?.query_row([id], |r| r.get(0))?;
        Ok(serde_json::from_str(&value)?)
    }
}

const BINDINGS: &str = "WITH bindings AS MATERIALIZED(SELECT json_extract(value,'$.pool') pool,
    json_extract(value,'$.agent') agent,json_extract(value,'$.rank') rank FROM json_each(?3))";
const ELIGIBLE: &str = "i.active=1 AND (i.valid_from IS NULL OR i.valid_from<=?4) AND (i.valid_to IS NULL OR i.valid_to>=?4)
    AND NOT EXISTS(SELECT 1 FROM bindings h CROSS JOIN memweft_recall_items x
      WHERE x.tenant_id=?1 AND x.user_id=?2 AND x.agent_id=h.agent AND x.pool_id=h.pool
      AND x.fact_key=i.fact_key AND x.active=1
      AND (x.valid_from IS NULL OR x.valid_from<=?4) AND (x.valid_to IS NULL OR x.valid_to>=?4)
      AND (h.rank<b.rank OR (h.rank=b.rank AND x.fact_id<i.fact_id)))";

type OrderedItem = (String, String, i64);
type ScoredItem = (i64, String, String, i64);
struct Search<'a, 'conn> {
    tx: &'a Transaction<'conn>,
    scope: &'a Scope,
    pools: &'a [String],
    bindings: &'a str,
    terms: &'a str,
    now: i64,
}

impl Search<'_, '_> {
    /// Complete key prefix of qualified winners. Pool precedence applies before
    /// the limit; an actual tuple range skips wholly shadowed later pools.
    fn ordered(&self, needed: usize, exclude: &[i64]) -> StoreResult<Vec<OrderedItem>> {
        let exclude = serde_json::to_string(exclude)?;
        let mut items: Vec<OrderedItem> = Vec::new();
        for (rank, pool) in self.pools.iter().enumerate() {
            let boundary = items.get(needed - 1);
            let upper = if boundary.is_some() {
                "AND (i.fact_key,i.fact_id) <= (?10,?11)"
            } else {
                "AND ?10 IS NULL AND ?11 IS NULL"
            };
            let sql = format!("{BINDINGS} SELECT i.fact_key,i.fact_id,i.id
                FROM memweft_recall_items i INDEXED BY memweft_recall_order CROSS JOIN bindings b
                WHERE b.rank=?7 AND i.tenant_id=?1 AND i.user_id=?2 AND i.agent_id=?8 AND i.pool_id=?9
                  AND {ELIGIBLE} AND i.id NOT IN(SELECT value FROM json_each(?5)) {upper}
                ORDER BY i.fact_key,i.fact_id LIMIT ?6");
            let agent = if pool == "private" { self.scope.agent_id.as_str() } else { "" };
            let extra = self.tx.prepare_cached(&sql)?.query_map(params![
                self.scope.tenant_id,self.scope.user_id,self.bindings,self.now,exclude,
                needed as i64,rank as i64,agent,pool,
                boundary.map(|b|b.0.as_str()),boundary.map(|b|b.1.as_str())
            ], |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?.collect::<Result<Vec<_>,_>>()?;
            items.extend(extra);
            items.sort();
            items.truncate(needed);
        }
        Ok(items)
    }

    /// Return an exact positive-score Top-K only when its completeness is proven.
    /// Short lists are consumed entirely. Unseen items can then match only the
    /// remaining dense lists, whose per-pool maximum weights bound their score.
    /// A qualified key prefix proves which equal-score items win the tie break.
    /// This is a bounded speculative fast path, not an approximate candidate cap.
    fn bounded_top(&self, terms: &BTreeSet<String>, needed: usize) -> StoreResult<Option<(Vec<i64>, &'static str)>> {
        const SPARSE_LIMIT: usize = 64;
        if needed > 257 || terms.len().saturating_mul(self.pools.len()) > 128 {
            return Ok(None);
        }
        let mut candidate_ids = BTreeSet::new();
        let mut unseen_bound = 0_i64;
        let mut dense = false;
        let mut probe = self.tx.prepare_cached("SELECT weight,item_id FROM memweft_recall_terms
            WHERE tenant_id=?1 AND user_id=?2 AND agent_id=?3 AND pool_id=?4 AND term=?5
            ORDER BY weight DESC,item_id LIMIT ?6")?;
        for pool in self.pools {
            let agent = if pool == "private" { self.scope.agent_id.as_str() } else { "" };
            let mut pool_bound = 0;
            for term in terms {
                let rows = probe.query_map(params![self.scope.tenant_id,self.scope.user_id,agent,pool,term,
                    (SPARSE_LIMIT+1) as i64], |r|Ok((r.get::<_,i64>(0)?,r.get::<_,i64>(1)?)))?
                    .collect::<Result<Vec<_>,_>>()?;
                if rows.len() > SPARSE_LIMIT {
                    dense = true;
                    // Includes ineligible/shadowed items, so this can only
                    // overestimate the maximum. Never underestimate for speed.
                    pool_bound += rows[0].0;
                } else {
                    candidate_ids.extend(rows.iter().map(|r|r.1));
                    if candidate_ids.len() > 2048 { return Ok(None); }
                }
            }
            unseen_bound = unseen_bound.max(pool_bound);
        }
        let prefix_limit = (needed * 2).max(128);
        let prefix = if dense { self.ordered(prefix_limit, &[])? } else { Vec::new() };
        candidate_ids.extend(prefix.iter().map(|r|r.2));
        if candidate_ids.is_empty() && !dense { return Ok(Some((Vec::new(), "bounded_prefix"))); }
        let ranked = self.score_candidates(&candidate_ids, needed)?;
        let complete = !dense || prefix.len() < prefix_limit || ranked.get(needed-1).is_some_and(|last| {
            last.3 > unseen_bound || (last.3 == unseen_bound && prefix.last().is_some_and(|end|
                (&last.1,&last.2) <= (&end.0,&end.1)))
        });
        if complete {
            return Ok(Some((ranked.into_iter().map(|r|r.0).collect(), "bounded_prefix")));
        }
        // The prefix already proves the tie order at this floor. Only records
        // strictly above it can displace a winner. For two terms, enumerate all
        // stronger single lists and weight-pair intersections without GROUP BY.
        if terms.len() == 2 && self.pools.len() <= 4 {
            if let Some(last) = ranked.get(needed-1) {
                if prefix.last().is_some_and(|end| (&last.1,&last.2) <= (&end.0,&end.1))
                    && self.competitive_pairs(terms, last.3, &mut candidate_ids)?
                {
                    let ranked = self.score_candidates(&candidate_ids, needed)?;
                    return Ok(Some((ranked.into_iter().map(|r|r.0).collect(), "bounded_intersection")));
                }
            }
        }
        Ok(None)
    }

    fn score_candidates(&self, candidate_ids: &BTreeSet<i64>, needed: usize) -> StoreResult<Vec<ScoredItem>> {
        let candidates = serde_json::to_string(&candidate_ids)?;
        let sql = format!("{BINDINGS}, scored AS MATERIALIZED(
            SELECT i.id,i.fact_key,i.fact_id,COALESCE((SELECT SUM(p.weight)
              FROM memweft_recall_terms p INDEXED BY memweft_recall_term_item
              WHERE p.item_id=i.id AND p.term IN(SELECT value FROM json_each(?5))),0) score
            FROM json_each(?6) c CROSS JOIN memweft_recall_items i
            JOIN bindings b ON b.pool=i.pool_id AND b.agent=i.agent_id
            WHERE i.id=c.value AND i.tenant_id=?1 AND i.user_id=?2
              AND {ELIGIBLE})
            SELECT id,fact_key,fact_id,score FROM scored WHERE score>0
            ORDER BY score DESC,fact_key,fact_id LIMIT ?7");
        Ok(self.tx.prepare_cached(&sql)?.query_map(params![self.scope.tenant_id,self.scope.user_id,
            self.bindings,self.now,self.terms,candidates,needed as i64],
            |r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,i64>(3)?)))?
            .collect::<Result<Vec<_>,_>>()?)
    }

    fn competitive_pairs(&self, terms: &BTreeSet<String>, floor: i64, ids: &mut BTreeSet<i64>) -> StoreResult<bool> {
        const CAP: usize = 2048;
        let terms: Vec<_> = terms.iter().collect();
        let mut maxima = self.tx.prepare_cached("SELECT weight FROM memweft_recall_terms
            WHERE tenant_id=?1 AND user_id=?2 AND agent_id=?3 AND pool_id=?4 AND term=?5
            ORDER BY weight DESC,item_id LIMIT 1")?;
        let mut single = self.tx.prepare_cached("SELECT item_id FROM memweft_recall_terms
            WHERE tenant_id=?1 AND user_id=?2 AND agent_id=?3 AND pool_id=?4 AND term=?5
              AND weight>?6 LIMIT ?7")?;
        let mut pair = self.tx.prepare_cached("SELECT a.item_id FROM memweft_recall_terms a
            CROSS JOIN memweft_recall_terms b
            WHERE a.tenant_id=?1 AND a.user_id=?2 AND a.agent_id=?3 AND a.pool_id=?4 AND a.term=?5 AND a.weight=?7
              AND b.tenant_id=?1 AND b.user_id=?2 AND b.agent_id=?3 AND b.pool_id=?4 AND b.term=?6 AND b.weight=?8
              AND b.item_id=a.item_id LIMIT ?9")?;
        for pool in self.pools {
            let agent = if pool == "private" { self.scope.agent_id.as_str() } else { "" };
            let mut weights = [0_i64; 2];
            for (index, term) in terms.iter().enumerate() {
                weights[index] = maxima.query_row(params![self.scope.tenant_id,self.scope.user_id,agent,pool,term],
                    |r|r.get(0)).optional()?.unwrap_or(0);
                // lexical_overlap_v1 assigns 2 for key and 1 for value. A future
                // scoring version outside this domain must use the general plan.
                if weights[index] > 3 { return Ok(false); }
                if weights[index] > floor {
                    let rows = single.query_map(params![self.scope.tenant_id,self.scope.user_id,agent,pool,term,floor,
                        (CAP+1) as i64], |r|r.get::<_,i64>(0))?.collect::<Result<Vec<_>,_>>()?;
                    if rows.len() > CAP { return Ok(false); }
                    ids.extend(rows);
                    if ids.len() > CAP { return Ok(false); }
                }
            }
            for a in 1..=weights[0].min(floor) {
                for b in 1..=weights[1].min(floor) {
                    if a+b <= floor { continue; }
                    let rows = pair.query_map(params![self.scope.tenant_id,self.scope.user_id,agent,pool,terms[0],terms[1],a,b,
                        (CAP+1) as i64], |r|r.get::<_,i64>(0))?.collect::<Result<Vec<_>,_>>()?;
                    if rows.len() > CAP { return Ok(false); }
                    ids.extend(rows);
                    if ids.len() > CAP { return Ok(false); }
                }
            }
        }
        Ok(true)
    }

    fn aggregate(&self, needed: usize) -> StoreResult<Vec<i64>> {
        let sql = format!("{BINDINGS}, scores AS MATERIALIZED(
            SELECT p.item_id,SUM(p.weight) score FROM bindings b CROSS JOIN memweft_recall_terms p
            WHERE p.tenant_id=?1 AND p.user_id=?2 AND p.agent_id=b.agent AND p.pool_id=b.pool
              AND p.term IN (SELECT value FROM json_each(?5)) GROUP BY p.item_id)
            SELECT i.id FROM scores s CROSS JOIN memweft_recall_items i
            JOIN bindings b ON b.pool=i.pool_id AND b.agent=i.agent_id
            WHERE i.id=s.item_id AND {ELIGIBLE}
            ORDER BY s.score DESC,i.fact_key,i.fact_id LIMIT ?6");
        Ok(self.tx.prepare_cached(&sql)?.query_map(params![self.scope.tenant_id,self.scope.user_id,
            self.bindings,self.now,self.terms,needed as i64], |r|r.get(0))?.collect::<Result<_,_>>()?)
    }
}

/// Pool precedence is a qualification predicate, BEFORE scoring/LIMIT. Otherwise
/// a matching but shadowed shared value can displace a less-relevant private one.
pub(crate) fn query(
    store: &SqliteStore,
    scope: &Scope,
    pools: &[String],
    query: Option<&str>,
    limit: usize,
) -> StoreResult<RecallCandidates> {
    if pools.len() > 32 || limit > 10_064 || query.is_some_and(|q| q.len() > 4096) {
        return Err(StoreError::InvalidInput(
            "recall request exceeds limits".into(),
        ));
    }
    if pools.iter().any(|p| p.trim().is_empty())
        || pools.iter().collect::<HashSet<_>>().len() != pools.len()
    {
        return Err(StoreError::InvalidInput(
            "recall pools must be nonempty and distinct".into(),
        ));
    }
    store.with_connection(|conn| {
        // Qualification, candidate selection and record fetches share a snapshot.
        let tx = conn.transaction()?;
        let bindings: Vec<_> = pools.iter().enumerate().map(|(rank,pool)| json!({
            "pool":pool,"agent":if pool=="private" {scope.agent_id.as_str()} else {""},"rank":rank})).collect();
        let bindings = serde_json::to_string(&bindings)?;
        let query_terms = lexical::terms(query.unwrap_or_default());
        let terms = serde_json::to_string(&query_terms)?;
        let now = chrono::Utc::now().timestamp_millis();
        let search = Search { tx: &tx, scope, pools, bindings: &bindings, terms: &terms, now };
        let mut ids = Vec::new();
        let mut ranking_plan = "key_order";
        if !query_terms.is_empty() && !pools.is_empty() {
            if let Some((top, plan)) = search.bounded_top(&query_terms, limit + 1)? {
                ids = top;
                ranking_plan = plan;
            } else {
                ids = search.aggregate(limit + 1)?;
                ranking_plan = "postings_aggregate";
            }
        }
        if ids.len() <= limit {
            let fill = search.ordered(limit + 1 - ids.len(), &ids)?;
            ids.extend(fill.into_iter().map(|r|r.2));
        }
        let has_more = ids.len()>limit;
        ids.truncate(limit);
        let records = ids.iter().map(|id|read_record(&tx,*id)).collect::<StoreResult<Vec<_>>>()?;
        let mut shadowed = Vec::new();
        let mut shadowed_truncated = false;
        // Batch explanation lookups for retrieved keys. Ordering by selected
        // ordinal preserves the per-record diagnostic order without repeatedly
        // binding/executing the same CTE for every selected record.
        if !ids.is_empty() {
            let selected = serde_json::to_string(&ids)?;
            let sql = format!("{BINDINGS}, selected AS MATERIALIZED(
                SELECT CAST(c.key AS INTEGER) ordinal,i.id,i.fact_key
                FROM json_each(?5) c CROSS JOIN memweft_recall_items i WHERE i.id=c.value)
                SELECT s.ordinal,i.id FROM selected s CROSS JOIN bindings b CROSS JOIN memweft_recall_items i
                WHERE i.tenant_id=?1 AND i.user_id=?2 AND i.agent_id=b.agent AND i.pool_id=b.pool
                  AND i.fact_key=s.fact_key AND i.id<>s.id AND i.active=1
                  AND (i.valid_from IS NULL OR i.valid_from<=?4) AND (i.valid_to IS NULL OR i.valid_to>=?4)
                ORDER BY s.ordinal,b.rank,i.fact_id LIMIT ?6");
            let losers: Vec<(usize,i64)> = tx.prepare_cached(&sql)?.query_map(params![
                scope.tenant_id,scope.user_id,bindings,now,selected,(DIAGNOSTIC_LIMIT+1) as i64
            ],|r|Ok((r.get(0)?,r.get(1)?)))?.collect::<Result<_,_>>()?;
            for (ordinal,loser) in losers {
                if shadowed.len()==DIAGNOSTIC_LIMIT {shadowed_truncated=true;break;}
                let record = &records[ordinal];
                let loser=read_record(&tx,loser)?;
                shadowed.push(json!({"key":record.fact.fact_key,"selected_pool":record.pool_id,
                    "shadowed_pool":loser.pool_id,"different_value":record.fact.value!=loser.fact.value}));
            }
        }
        // Unretrieved keys may have additional shadows, even when this sample is empty.
        shadowed_truncated |= has_more;
        tx.commit()?;
        Ok(RecallCandidates { records,shadowed,has_more,shadowed_truncated,ranking_plan })
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Store;
    use memweft_types::{Fact, FactStatus, ScopeLevel, Validity};
    fn scope() -> Scope {
        Scope {
            tenant_id: "t".into(),
            user_id: "u".into(),
            agent_id: "a".into(),
            session_id: String::new(),
            run_id: String::new(),
        }
    }
    fn fact(key: &str, value: &str) -> Fact {
        Fact {
            fact_id: key.into(),
            fact_key: key.into(),
            value: json!(value),
            status: FactStatus::Active,
            validity: Validity::default(),
            confidence: 1.0,
            sources: vec![],
            scope_level: ScopeLevel::User,
            notes: String::new(),
        }
    }
    fn remove_index(conn: &Connection) {
        for table in ["facts", "memweft_pool_facts"] {
            for op in ["insert", "update", "delete"] {
                conn.execute_batch(&format!("DROP TRIGGER memweft_recall_{table}_{op}"))
                    .unwrap();
            }
        }
        conn.execute_batch("DROP TABLE memweft_recall_terms; DROP TABLE memweft_recall_items; DROP TABLE memweft_recall_version;").unwrap();
    }
    #[test]
    fn legacy_backfill_is_atomic_idempotent_and_supports_shared_tombstones() {
        let store = SqliteStore::new_in_memory().unwrap();
        store
            .upsert_fact(&scope(), fact("private", "部署端口 port"))
            .unwrap();
        store
            .put_pool_fact(&scope(), "team", fact("shared", "shared port"), None)
            .unwrap();
        store
            .put_pool_fact(&scope(), "team", fact("deleted", "old secret"), None)
            .unwrap();
        store
            .forget_pool_fact(&scope(), "team", "deleted", None)
            .unwrap();
        store
            .with_connection(|conn| {
                remove_index(conn);
                // A bad source document aborts the entire migration, including DDL.
                conn.execute("UPDATE facts SET value_json='invalid JSON'", [])?;
                assert!(ensure_schema(conn).is_err());
                assert_eq!(version(conn)?, None);
                conn.execute("UPDATE facts SET value_json=?", ["\"部署端口 port\""])?;
                ensure_schema(conn)?;
                ensure_schema(conn)?;
                let count: i64 =
                    conn.query_row("SELECT count(*) FROM memweft_recall_items", [], |r| {
                        r.get(0)
                    })?;
                assert_eq!(count, 2);
                assert_eq!(
                    conn.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))?,
                    "ok"
                );
                Ok(())
            })
            .unwrap();
        let result = query(
            &store,
            &scope(),
            &["private".into(), "team".into()],
            Some("port"),
            10,
        )
        .unwrap();
        assert_eq!(result.records.len(), 2);
        assert!(result.records.iter().all(|r| r.fact.fact_key != "deleted"));
    }
    #[test]
    fn rolled_back_source_mutation_does_not_leave_postings_or_delete_old_ones() {
        let store = SqliteStore::new_in_memory().unwrap();
        store
            .upsert_fact(&scope(), fact("key", "old port"))
            .unwrap();
        store
            .with_connection(|conn| {
                let tx = conn.transaction()?;
                tx.execute(
                    "UPDATE facts SET fact_key='changed',value_json='\"new host\"'",
                    [],
                )?;
                // drop rolls both source changes and trigger side effects back
                Ok(())
            })
            .unwrap();
        let r = query(&store, &scope(), &["private".into()], Some("old"), 1).unwrap();
        assert_eq!(r.records[0].fact.value, "old port");
        assert_eq!(r.records[0].fact.fact_key, "key");
    }
    #[test]
    fn zero_fill_plan_seeks_the_scope_order_index_without_sorting_all_items() {
        let store = SqliteStore::new_in_memory().unwrap();
        store.upsert_fact(&scope(), fact("a", "port")).unwrap();
        store.with_connection(|conn| {
            // Candidate fill uses an explicitly ordered outer loop. Verify the
            // index can satisfy this prefix + key order, independently of timing.
            let mut stmt=conn.prepare("EXPLAIN QUERY PLAN SELECT id FROM memweft_recall_items INDEXED BY memweft_recall_order
                WHERE tenant_id=? AND user_id=? AND agent_id=? AND pool_id=? ORDER BY fact_key,fact_id LIMIT 65")?;
            let rows=stmt.query_map(params!["t","u","a","private"],|r|r.get::<_,String>(3))?.collect::<Result<Vec<_>,_>>()?;
            assert!(rows.iter().any(|r|r.contains("memweft_recall_order")));
            assert!(rows.iter().all(|r|!r.contains("TEMP B-TREE")));
            Ok(())
        }).unwrap();
    }

    #[test]
    fn v1_migration_preserves_postings_and_cached_writes_then_reuses_v2() {
        let store = SqliteStore::new_in_memory().unwrap();
        store.upsert_fact(&scope(), fact("port", "port host")).unwrap();
        store.put_pool_fact(&scope(), "team", fact("shared", "port"), None).unwrap();
        store.with_connection(|conn| {
            // Build the exact v1 layout while retaining its items and triggers.
            for table in ["facts", "memweft_pool_facts"] {
                for op in ["insert", "update", "delete"] {
                    conn.execute_batch(&format!("DROP TRIGGER memweft_recall_{table}_{op}"))?;
                }
            }
            conn.execute_batch("CREATE TABLE old_terms(
                tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,agent_id TEXT NOT NULL,
                pool_id TEXT NOT NULL,term TEXT NOT NULL,item_id INTEGER NOT NULL,weight INTEGER NOT NULL,
                PRIMARY KEY(tenant_id,user_id,agent_id,pool_id,term,item_id),
                FOREIGN KEY(item_id) REFERENCES memweft_recall_items(id) ON DELETE CASCADE) WITHOUT ROWID;
                INSERT INTO old_terms SELECT * FROM memweft_recall_terms;
                DROP TABLE memweft_recall_terms;
                ALTER TABLE old_terms RENAME TO memweft_recall_terms;
                CREATE INDEX memweft_recall_term_item ON memweft_recall_terms(item_id);
                UPDATE memweft_recall_version SET version=1;")?;
            let tx = conn.transaction()?;
            install_triggers(&tx, false)?;
            tx.commit()?;
            // An incompatible source layout must leave the v1 migration intact.
            conn.execute_batch("ALTER TABLE memweft_recall_terms ADD COLUMN unexpected TEXT")?;
            assert!(ensure_schema(conn).is_err());
            assert_eq!(version(conn)?, Some(1));
            let triggers: i64 = conn.query_row("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'memweft_recall_%'", [], |r|r.get(0))?;
            assert_eq!(triggers, 6);
            conn.execute_batch("ALTER TABLE memweft_recall_terms DROP COLUMN unexpected")?;
            ensure_schema(conn)?;
            ensure_schema(conn)?;
            assert_eq!(version(conn)?, Some(2));
            assert_eq!(conn.query_row("SELECT sum(weight) FROM memweft_recall_terms", [], |r|r.get::<_,i64>(0))?, 7);
            // Maximum-weight probing uses the primary key, without temp sorting.
            let mut stmt = conn.prepare("EXPLAIN QUERY PLAN SELECT weight,item_id FROM memweft_recall_terms
                WHERE tenant_id=?1 AND user_id=?2 AND agent_id=?3 AND pool_id=?4 AND term=?5
                ORDER BY weight DESC,item_id LIMIT 65")?;
            let rows = stmt.query_map(params!["t","u","a","private","port"], |r|r.get::<_,String>(3))?
                .collect::<Result<Vec<_>,_>>()?;
            assert!(rows.iter().any(|r|r.contains("PRIMARY KEY")));
            assert!(rows.iter().all(|r|!r.contains("TEMP B-TREE")));
            Ok(())
        }).unwrap();
        store.upsert_fact(&scope(), fact("port", "replacement")).unwrap();
        let r = query(&store,&scope(),&["private".into(),"team".into()],Some("port"),10).unwrap();
        assert_eq!(r.records.len(), 2);
        assert_eq!(r.records[0].fact.fact_key, "port");
        store.forget_pool_fact(&scope(), "team", "shared", None).unwrap();
        store.with_connection(|conn| {
            assert_eq!(conn.query_row("PRAGMA integrity_check", [], |r|r.get::<_,String>(0))?, "ok");
            assert!(!conn.prepare("PRAGMA foreign_key_check")?.exists([])?);
            Ok(())
        }).unwrap();
    }
}
