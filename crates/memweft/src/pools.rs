//! Configurable fact visibility. Pool bindings are application policy, not authentication.
use super::*;
pub use memweft_store::{PoolFact, PoolRef, PoolRevision};
use std::collections::{BTreeMap, HashSet};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum PoolAccess {
    #[default]
    Read,
    ReadWrite,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PoolBinding {
    pub pool_id: String,
    #[serde(default)]
    pub access: PoolAccess,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ConflictPolicy {
    #[default]
    PrivateFirst,
    ReadOrder,
    Error,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryConfig {
    pub read_pools: Vec<PoolBinding>,
    /// None makes explicit pool selection mandatory for writes.
    pub default_write_pool: Option<String>,
    #[serde(default)]
    pub conflict_policy: ConflictPolicy,
}
impl Default for MemoryConfig {
    fn default() -> Self {
        Self {
            read_pools: vec![PoolBinding {
                pool_id: "private".into(),
                access: PoolAccess::ReadWrite,
            }],
            default_write_pool: Some("private".into()),
            conflict_policy: ConflictPolicy::PrivateFirst,
        }
    }
}
impl MemoryConfig {
    pub(crate) fn validate(&self) -> StoreResult<()> {
        if self.read_pools.len() > 32 {
            return Err(invalid("at most 32 read pools are supported"));
        }
        let mut ids = HashSet::new();
        for binding in &self.read_pools {
            validate_id("pool_id", &binding.pool_id)?;
            if !ids.insert(&binding.pool_id) {
                return Err(invalid("duplicate pool binding"));
            }
        }
        if let Some(pool) = &self.default_write_pool {
            self.write_pool(Some(pool))?;
        }
        Ok(())
    }
    fn read_pool(&self, pool: &str) -> StoreResult<()> {
        if self.read_pools.iter().any(|b| b.pool_id == pool) {
            Ok(())
        } else {
            Err(invalid("pool is not bound for reading"))
        }
    }
    fn write_pool<'a>(&'a self, explicit: Option<&'a str>) -> StoreResult<&'a str> {
        let pool = explicit
            .or(self.default_write_pool.as_deref())
            .ok_or_else(|| invalid("no default write pool; select a writable pool"))?;
        if self
            .read_pools
            .iter()
            .any(|b| b.pool_id == pool && b.access == PoolAccess::ReadWrite)
        {
            Ok(pool)
        } else {
            Err(invalid("pool is not bound for writing"))
        }
    }
}
impl UserMemory {
    pub fn remember_in(
        &self,
        pool: Option<&str>,
        key: &str,
        value: Value,
        expected_revision: Option<u64>,
    ) -> StoreResult<PoolFact> {
        validate_id("key", key)?;
        let pool = self.memory_config.write_pool(pool)?;
        let fact = Fact {
            fact_id: format!("memweft:key:{key}"),
            fact_key: key.into(),
            value,
            status: FactStatus::Active,
            validity: Validity::default(),
            confidence: 1.0,
            sources: vec![],
            scope_level: ScopeLevel::User,
            notes: String::new(),
        };
        if pool == "private" {
            if expected_revision.is_some() {
                return Err(invalid(
                    "private legacy facts do not support expected_revision",
                ));
            }
            self.store.upsert_fact(&self.scope, fact.clone())?;
            Ok(PoolFact {
                fact,
                pool_id: pool.into(),
                revision: None,
                writer_agent_id: self.scope.agent_id.clone(),
            })
        } else {
            self.store
                .put_pool_fact(&self.scope, pool, fact, expected_revision)
        }
    }
    pub fn forget_in(
        &self,
        pool: Option<&str>,
        key: &str,
        expected_revision: Option<u64>,
    ) -> StoreResult<bool> {
        validate_id("key", key)?;
        let pool = self.memory_config.write_pool(pool)?;
        if pool == "private" {
            if expected_revision.is_some() {
                return Err(invalid(
                    "private legacy facts do not support expected_revision",
                ));
            }
            self.store.forget_fact(&self.scope, key)
        } else {
            self.store
                .forget_pool_fact(&self.scope, pool, key, expected_revision)
        }
    }
    fn read_records(&self, pool: &str) -> StoreResult<Vec<PoolFact>> {
        self.memory_config.read_pool(pool)?;
        if pool == "private" {
            Ok(self
                .store
                .list_facts(
                    &self.scope,
                    FactFilter {
                        status: Some(vec![FactStatus::Active]),
                        valid_at: Some(Utc::now()),
                        limit: None,
                    },
                )?
                .into_iter()
                .map(|fact| PoolFact {
                    fact,
                    pool_id: pool.into(),
                    revision: None,
                    writer_agent_id: self.scope.agent_id.clone(),
                })
                .collect())
        } else {
            self.store.pool_facts(&self.scope, pool)
        }
    }
    /// An explicit pool bypasses key merging, but never bypasses its read binding.
    pub fn memory_records(&self, pool: Option<&str>) -> StoreResult<Vec<PoolFact>> {
        match pool {
            Some(pool) => self.read_records(pool),
            None => Ok(self.resolve_pools()?.0),
        }
    }
    pub(crate) fn resolve_pools(&self) -> StoreResult<(Vec<PoolFact>, Vec<Value>)> {
        let mut bindings: Vec<_> = self.memory_config.read_pools.iter().collect();
        if matches!(
            self.memory_config.conflict_policy,
            ConflictPolicy::PrivateFirst
        ) {
            bindings.sort_by_key(|b| b.pool_id != "private");
        }
        let mut selected = BTreeMap::<String, PoolFact>::new();
        let mut shadowed = vec![];
        for binding in bindings {
            for record in self.read_records(&binding.pool_id)? {
                let key = record.fact.fact_key.clone();
                if let Some(winner) = selected.get(&key) {
                    let differs = winner.fact.value != record.fact.value;
                    if differs
                        && matches!(self.memory_config.conflict_policy, ConflictPolicy::Error)
                    {
                        return Err(StoreError::Conflict(format!(
                            "memory key {key} differs between pools {} and {}",
                            winner.pool_id, record.pool_id
                        )));
                    }
                    shadowed.push(json!({"key":key,"selected_pool":winner.pool_id,"shadowed_pool":record.pool_id,"different_value":differs}));
                } else {
                    selected.insert(key, record);
                }
            }
        }
        Ok((selected.into_values().collect(), shadowed))
    }

    pub(crate) fn context_candidates(&self, query: Option<&str>, limit: usize)
        -> StoreResult<(memweft_store::RecallCandidates, &'static str)> {
        // Error mode promises to reject conflicts anywhere in the visible pools,
        // including keys outside Top-K. Keep that global check on the fallback.
        if !matches!(self.memory_config.conflict_policy, ConflictPolicy::Error) {
            let mut pools: Vec<_> = self.memory_config.read_pools.iter().map(|b|b.pool_id.clone()).collect();
            if matches!(self.memory_config.conflict_policy, ConflictPolicy::PrivateFirst) {
                pools.sort_by_key(|p|p!="private");
            }
            if let Some(candidates) = self.store.recall_candidates(&self.scope, &pools, query, limit)? {
                return Ok((candidates, "sqlite_inverted_v1"));
            }
        }
        let (records, mut shadowed) = self.resolve_pools()?;
        let shadowed_truncated = shadowed.len()>64;
        shadowed.truncate(64);
        Ok((memweft_store::RecallCandidates { records, shadowed, has_more:false, shadowed_truncated, ranking_plan:"full_scan" }, "full_scan"))
    }
}
