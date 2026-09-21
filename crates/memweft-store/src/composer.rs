use chrono::{DateTime, Duration, Utc};
use memweft_types::{
    Budget, BudgetReport, Citation, CitationType, ConversationTurn, Episode, Fact, FactStatus,
    Insight, InsightItem, JsonMap, KeyQuote, LongTerm, MemoryPacket, Meta, Purpose, Scope,
    ShortTerm, UsagePolicy,
};
use serde::Serialize;
use serde_json::{json, Value};
use std::cmp::Ordering;
use std::collections::HashMap;

use crate::{
    EpisodeFilter, Event, EventKind, FactFilter, InsightFilter, Store, StoreResult, StmState,
    TimeRangeFilter,
};
use tracing::{debug, info, instrument, warn};

#[derive(Debug, Clone, Default)]
pub struct RecallCues {
    pub tags: Vec<String>,
    pub entities: Vec<String>,
    pub keywords: Vec<String>,
    pub time_range: Option<TimeRangeFilter>,
}

#[derive(Debug, Clone)]
pub struct RecallPolicy {
    pub max_total_candidates: usize,
    pub max_facts: usize,
    pub max_procedures: usize,
    pub max_episodes: usize,
    pub max_insights: usize,
    pub max_key_quotes: usize,
    pub conversation_window: usize,
    pub episode_time_window_days: i64,
    pub last_tool_evidence_limit: usize,
    pub include_conversation_window: bool,
    pub include_insights_in_tool: bool,
    pub allow_insights_in_responder: bool,
}

impl Default for RecallPolicy {
    fn default() -> Self {
        Self {
            max_total_candidates: 100,
            max_facts: 30,
            max_procedures: 5,
            max_episodes: 20,
            max_insights: 10,
            max_key_quotes: 10,
            conversation_window: 5,
            episode_time_window_days: 30,
            last_tool_evidence_limit: 3,
            include_conversation_window: false,
            include_insights_in_tool: false,
            allow_insights_in_responder: false,
        }
    }
}

#[derive(Debug, Clone)]
pub struct BuildRequest {
    pub scope: Scope,
    pub purpose: Purpose,
    pub task_type: Option<String>,
    pub cues: RecallCues,
    pub budget: Budget,
    pub policy_id: String,
    pub policy: RecallPolicy,
    pub persist: bool,
}

impl BuildRequest {
    pub fn new(scope: Scope, purpose: Purpose) -> Self {
        Self {
            scope,
            purpose,
            task_type: None,
            cues: RecallCues::default(),
            budget: default_budget(),
            policy_id: "default".to_string(),
            policy: RecallPolicy::default(),
            persist: true,
        }
    }
}

#[instrument(skip(store), fields(scope = ?request.scope, purpose = ?request.purpose))]
pub fn build_memory_packet<S: Store + ?Sized>(
    store: &S,
    request: BuildRequest,
) -> StoreResult<MemoryPacket> {
    let now = Utc::now();
    let task_type = request
        .task_type
        .clone()
        .unwrap_or_else(|| "generic".to_string());
    
    debug!("Starting build_memory_packet");

    let working_state = store
        .get_working_state(&request.scope)?
        .unwrap_or_default();
    let stm_state = store.get_stm(&request.scope)?.unwrap_or_default();

    let short_term = build_short_term(working_state, stm_state, store, &request)?;

    let facts = load_facts(store, &request.scope, now, request.policy.max_facts)?;
    let procedures =
        load_procedures(store, &request.scope, &task_type, request.policy.max_procedures)?;
    let episodes = load_episodes(store, &request.scope, &request, now)?;
    let mut insight = load_insights(store, &request.scope, &request)?;

    let mut long_term = LongTerm {
        facts,
        preferences: Vec::new(),
        procedures,
        episodes,
    };

    enforce_total_candidate_limit(&request.policy, &mut long_term, &mut insight);

    let mut citations = collect_citations(&short_term, &long_term, &insight);
    citations.sort_by(|a, b| citation_sort_key(a).cmp(&citation_sort_key(b)));

    let meta = Meta {
        schema_version: "v1".to_string(),
        scope: request.scope.clone(),
        generated_at: now,
        purpose: request.purpose.clone(),
        task_type,
        cues: cues_to_json(&request.cues),
        budget: request.budget.clone(),
        policy_id: request.policy_id.clone(),
    };

    let mut packet = MemoryPacket {
        meta,
        short_term,
        long_term,
        insight,
        citations,
        budget_report: BudgetReport::default(),
        explain: JsonMap::new(),
    };

    apply_budget(&request, &mut packet);

    if request.persist {
        if let Err(e) = store.write_context_build(&request.scope, packet.clone()) {
            warn!("Failed to persist context build: {}", e);
        }
    }
    
    info!(
        candidates.facts = packet.long_term.facts.len(),
        candidates.episodes = packet.long_term.episodes.len(),
        "Memory packet built"
    );

    Ok(packet)
}

fn build_short_term<S: Store + ?Sized>(
    working_state: memweft_types::WorkingState,
    stm_state: StmState,
    store: &S,
    request: &BuildRequest,
) -> StoreResult<ShortTerm> {
    let mut short_term = ShortTerm::default();
    
    // Pass by value optimization: move fields instead of cloning
    short_term.last_tool_evidence = working_state.tool_evidence.clone();
    short_term.working_state = working_state;
    short_term.rolling_summary = stm_state.rolling_summary;

    short_term.key_quotes = stm_state.key_quotes;
    if short_term.key_quotes.len() > request.policy.max_key_quotes {
        short_term.key_quotes.truncate(request.policy.max_key_quotes);
    }

    if short_term.last_tool_evidence.len() > request.policy.last_tool_evidence_limit {
        short_term
            .last_tool_evidence
            .truncate(request.policy.last_tool_evidence_limit);
    }

    if request.policy.include_conversation_window {
        let events = store.list_events(&request.scope, TimeRangeFilter::default(), None)?;
        short_term.conversation_window =
            build_conversation_window(events, request.policy.conversation_window);
    }

    Ok(short_term)
}

fn load_facts<S: Store + ?Sized>(
    store: &S,
    scope: &Scope,
    now: DateTime<Utc>,
    max_facts: usize,
) -> StoreResult<Vec<Fact>> {
    let mut facts = store.list_facts(
        scope,
        FactFilter {
            status: Some(vec![FactStatus::Active]),
            valid_at: Some(now),
            limit: Some(max_facts),
        },
    )?;

    facts.sort_by(|a, b| {
        a.fact_key
            .cmp(&b.fact_key)
            .then_with(|| a.fact_id.cmp(&b.fact_id))
    });

    if facts.len() > max_facts {
        debug!(
            "Trimming facts from {} to limit {}",
            facts.len(),
            max_facts
        );
        facts.truncate(max_facts);
    }

    Ok(facts)
}

fn load_procedures<S: Store + ?Sized>(
    store: &S,
    scope: &Scope,
    task_type: &str,
    max_procedures: usize,
) -> StoreResult<Vec<memweft_types::Procedure>> {
    let mut procedures = store.list_procedures(scope, task_type, Some(max_procedures))?;
    procedures.sort_by(|a, b| {
        b.priority
            .cmp(&a.priority)
            .then_with(|| a.procedure_id.cmp(&b.procedure_id))
    });
    if procedures.len() > max_procedures {
        procedures.truncate(max_procedures);
    }
    Ok(procedures)
}

fn load_episodes<S: Store + ?Sized>(
    store: &S,
    scope: &Scope,
    request: &BuildRequest,
    now: DateTime<Utc>,
) -> StoreResult<Vec<Episode>> {
    let mut filter = EpisodeFilter::default();
    if let Some(range) = &request.cues.time_range {
        filter.time_range = Some(range.clone());
    } else {
        let start = now - Duration::days(request.policy.episode_time_window_days);
        filter.time_range = Some(TimeRangeFilter {
            start: Some(start),
            end: Some(now),
        });
    }
    filter.tags = request.cues.tags.clone();
    filter.entities = request.cues.entities.clone();

    let mut episodes = store.list_episodes(scope, filter)?;
    for episode in &mut episodes {
        episode.recency_score = Some(compute_recency_score(episode, now));
    }
    episodes.sort_by(|a, b| {
        b.recency_score
            .unwrap_or(0.0)
            .partial_cmp(&a.recency_score.unwrap_or(0.0))
            .unwrap_or(Ordering::Equal)
            .then_with(|| a.episode_id.cmp(&b.episode_id))
    });
    if episodes.len() > request.policy.max_episodes {
        debug!(
            "Trimming episodes from {} to limit {}",
            episodes.len(),
            request.policy.max_episodes
        );
        episodes.truncate(request.policy.max_episodes);
    }
    Ok(episodes)
}

fn load_insights<S: Store + ?Sized>(
    store: &S,
    scope: &Scope,
    request: &BuildRequest,
) -> StoreResult<Insight> {
    let include_insights = match request.purpose {
        Purpose::Planner => true,
        Purpose::Tool => request.policy.include_insights_in_tool,
        Purpose::Responder => request.policy.allow_insights_in_responder,
    };

    if !include_insights {
        return Ok(Insight {
            usage_policy: UsagePolicy {
                allow_in_responder: request.policy.allow_insights_in_responder,
            },
            hypotheses: Vec::new(),
            strategy_sketches: Vec::new(),
            patterns: Vec::new(),
        });
    }

    let mut items = store.list_insights(
        scope,
        InsightFilter {
            validation_state: Some(vec![
                memweft_types::ValidationState::Validated,
                memweft_types::ValidationState::Testing,
                memweft_types::ValidationState::Unvalidated,
            ]),
            limit: None,
        },
    )?;
    items.sort_by(|a, b| insight_sort_key(b).cmp(&insight_sort_key(a)));
    if items.len() > request.policy.max_insights {
        items.truncate(request.policy.max_insights);
    }

    Ok(bucket_insights(items, request.policy.allow_insights_in_responder))
}

fn bucket_insights(items: Vec<InsightItem>, allow_in_responder: bool) -> Insight {
    let mut hypotheses = Vec::new();
    let mut strategy_sketches = Vec::new();
    let mut patterns = Vec::new();

    for item in items {
        match item.kind {
            memweft_types::InsightType::Hypothesis => hypotheses.push(item),
            memweft_types::InsightType::Strategy => strategy_sketches.push(item),
            memweft_types::InsightType::Pattern => patterns.push(item),
        }
    }

    Insight {
        usage_policy: UsagePolicy {
            allow_in_responder,
        },
        hypotheses,
        strategy_sketches,
        patterns,
    }
}

fn insight_sort_key(item: &InsightItem) -> (u8, i32, String) {
    let state_rank = match item.validation_state {
        memweft_types::ValidationState::Validated => 3,
        memweft_types::ValidationState::Testing => 2,
        memweft_types::ValidationState::Unvalidated => 1,
        memweft_types::ValidationState::Rejected => 0,
    };
    let confidence_rank = (item.confidence * 1000.0) as i32;
    (state_rank, confidence_rank, item.id.clone())
}

fn compute_recency_score(episode: &Episode, now: DateTime<Utc>) -> f64 {
    let elapsed = now - episode.time_range.start;
    let days = elapsed.num_seconds().max(0) as f64 / 86_400.0;
    1.0 / (1.0 + days)
}

fn build_conversation_window(events: Vec<Event>, limit: usize) -> Vec<ConversationTurn> {
    let mut turns: Vec<ConversationTurn> = events
        .into_iter()
        .filter_map(|event| event_to_turn(&event))
        .collect();

    turns.sort_by(|a, b| a.ts.cmp(&b.ts));
    if turns.len() > limit {
        turns = turns.split_off(turns.len() - limit);
    }
    turns
}

fn event_to_turn(event: &Event) -> Option<ConversationTurn> {
    if !matches!(event.kind, EventKind::Message) {
        return None;
    }

    let (content, role) = parse_event_payload(&event.payload)?;
    Some(ConversationTurn {
        role,
        content,
        evidence_id: Some(event.event_id.clone()),
        ts: Some(event.ts),
    })
}

fn parse_event_payload(payload: &Value) -> Option<(String, memweft_types::Role)> {
    match payload {
        Value::String(text) => Some((text.clone(), memweft_types::Role::User)),
        Value::Object(map) => {
            let content = map
                .get("content")
                .and_then(Value::as_str)
                .or_else(|| map.get("text").and_then(Value::as_str))?;
            let role = map
                .get("role")
                .and_then(Value::as_str)
                .and_then(parse_role)
                .unwrap_or(memweft_types::Role::User);
            Some((content.to_string(), role))
        }
        _ => None,
    }
}

fn parse_role(value: &str) -> Option<memweft_types::Role> {
    match value {
        "user" => Some(memweft_types::Role::User),
        "assistant" => Some(memweft_types::Role::Assistant),
        "tool" => Some(memweft_types::Role::Tool),
        _ => None,
    }
}

fn cues_to_json(cues: &RecallCues) -> JsonMap {
    let mut map = JsonMap::new();
    if !cues.tags.is_empty() {
        map.insert("tags".to_string(), json!(cues.tags));
    }
    if !cues.entities.is_empty() {
        map.insert("entities".to_string(), json!(cues.entities));
    }
    if !cues.keywords.is_empty() {
        map.insert("keywords".to_string(), json!(cues.keywords));
    }
    if let Some(range) = &cues.time_range {
        let start = range.start.map(|s| s.to_rfc3339());
        let end = range.end.map(|e| e.to_rfc3339());
        map.insert("time_range".to_string(), json!({ "start": start, "end": end }));
    }
    map
}

fn collect_citations(
    short_term: &ShortTerm,
    long_term: &LongTerm,
    insight: &Insight,
) -> Vec<Citation> {
    let mut citations = HashMap::new();

    collect_citations_from_key_quotes(&short_term.key_quotes, &mut citations);
    collect_citations_from_evidence(&short_term.last_tool_evidence, &mut citations);
    collect_citations_from_facts(&long_term.facts, &mut citations);
    collect_citations_from_episodes(&long_term.episodes, &mut citations);
    collect_citations_from_procedures(&long_term.procedures, &mut citations);
    collect_citations_from_insights(insight, &mut citations);
    collect_citations_from_conversation_window(&short_term.conversation_window, &mut citations);

    citations.into_values().collect()
}

fn collect_citations_from_key_quotes(
    quotes: &[KeyQuote],
    map: &mut HashMap<String, Citation>,
) {
    for quote in quotes {
        let key = citation_key(&quote.evidence_id, &CitationType::Message);
        map.entry(key).or_insert_with(|| Citation {
            id: quote.evidence_id.clone(),
            kind: CitationType::Message,
            ts: quote.ts,
            summary: quote.quote.clone(),
        });
    }
}

fn collect_citations_from_evidence(
    evidence: &[memweft_types::EvidenceRef],
    map: &mut HashMap<String, Citation>,
) {
    for item in evidence {
        let kind = evidence_kind_to_citation(&item.kind);
        let key = citation_key(&item.evidence_id, &kind);
        map.entry(key).or_insert_with(|| Citation {
            id: item.evidence_id.clone(),
            kind,
            ts: None,
            summary: item.summary.clone(),
        });
    }
}

fn collect_citations_from_facts(facts: &[Fact], map: &mut HashMap<String, Citation>) {
    for fact in facts {
        for source in &fact.sources {
            let key = citation_key(source, &CitationType::Message);
            map.entry(key).or_insert_with(|| Citation {
                id: source.clone(),
                kind: CitationType::Message,
                ts: None,
                summary: String::new(),
            });
        }
    }
}

fn collect_citations_from_episodes(episodes: &[Episode], map: &mut HashMap<String, Citation>) {
    for episode in episodes {
        for source in &episode.sources {
            let key = citation_key(source, &CitationType::Message);
            map.entry(key).or_insert_with(|| Citation {
                id: source.clone(),
                kind: CitationType::Message,
                ts: None,
                summary: String::new(),
            });
        }
    }
}

fn collect_citations_from_procedures(
    procedures: &[memweft_types::Procedure],
    map: &mut HashMap<String, Citation>,
) {
    for procedure in procedures {
        for source in &procedure.sources {
            let key = citation_key(source, &CitationType::Message);
            map.entry(key).or_insert_with(|| Citation {
                id: source.clone(),
                kind: CitationType::Message,
                ts: None,
                summary: String::new(),
            });
        }
    }
}

fn collect_citations_from_insights(insight: &Insight, map: &mut HashMap<String, Citation>) {
    let items = insight
        .hypotheses
        .iter()
        .chain(insight.strategy_sketches.iter())
        .chain(insight.patterns.iter());
    for item in items {
        for source in &item.sources {
            let key = citation_key(source, &CitationType::Message);
            map.entry(key).or_insert_with(|| Citation {
                id: source.clone(),
                kind: CitationType::Message,
                ts: None,
                summary: String::new(),
            });
        }
    }
}

fn collect_citations_from_conversation_window(
    turns: &[ConversationTurn],
    map: &mut HashMap<String, Citation>,
) {
    for turn in turns {
        if let Some(evidence_id) = &turn.evidence_id {
            let key = citation_key(evidence_id, &CitationType::Message);
            map.entry(key).or_insert_with(|| Citation {
                id: evidence_id.clone(),
                kind: CitationType::Message,
                ts: turn.ts,
                summary: turn.content.clone(),
            });
        }
    }
}

fn citation_key(id: &str, kind: &CitationType) -> String {
    format!("{}|{}", id, citation_kind_label(kind))
}

fn citation_kind_label(kind: &CitationType) -> &'static str {
    match kind {
        CitationType::Message => "message",
        CitationType::ToolResult => "tool_result",
        CitationType::StatePatch => "state_patch",
    }
}

fn citation_sort_key(citation: &Citation) -> (String, String) {
    (citation.id.clone(), citation_kind_label(&citation.kind).to_string())
}

fn evidence_kind_to_citation(kind: &str) -> CitationType {
    match kind {
        "tool_result" => CitationType::ToolResult,
        "state_patch" => CitationType::StatePatch,
        _ => CitationType::Message,
    }
}

fn enforce_total_candidate_limit(policy: &RecallPolicy, long_term: &mut LongTerm, insight: &mut Insight) {
    let mut total = long_term.facts.len()
        + long_term.procedures.len()
        + long_term.episodes.len()
        + insight_total(insight);

    while total > policy.max_total_candidates {
        if !insight.hypotheses.is_empty() {
            insight.hypotheses.pop();
        } else if !insight.strategy_sketches.is_empty() {
            insight.strategy_sketches.pop();
        } else if !insight.patterns.is_empty() {
            insight.patterns.pop();
        } else if !long_term.episodes.is_empty() {
            long_term.episodes.pop();
        } else if !long_term.procedures.is_empty() {
            long_term.procedures.pop();
        } else if !long_term.facts.is_empty() {
            long_term.facts.pop();
        } else {
            break;
        }
        total = long_term.facts.len()
            + long_term.procedures.len()
            + long_term.episodes.len()
            + insight_total(insight);
    }
}

fn insight_total(insight: &Insight) -> usize {
    insight.hypotheses.len() + insight.strategy_sketches.len() + insight.patterns.len()
}

fn apply_budget(request: &BuildRequest, packet: &mut MemoryPacket) {
    let mut report = BudgetReport {
        max_tokens: request.budget.max_tokens,
        ..BudgetReport::default()
    };

    let mut omissions = Vec::new();
    trim_to_budget(request, packet, &mut omissions);
    report.omissions = omissions;

    let section_usage = compute_section_usage(packet);
    let used_tokens_est = section_usage.values().filter_map(|v| v.as_u64()).sum::<u64>() as u32;
    report.used_tokens_est = used_tokens_est;
    report.section_usage = section_usage;

    packet.budget_report = report;
    packet.explain = build_explain(request, packet);
}

fn trim_to_budget(request: &BuildRequest, packet: &mut MemoryPacket, omissions: &mut Vec<Value>) {
    apply_per_section_budgets(request, packet, omissions);

    if request.budget.max_tokens == 0 {
        return;
    }

    let mut total_tokens = estimate_packet_tokens(packet);
    if total_tokens > request.budget.max_tokens {
        debug!("Packet tokens {} exceeds budget {}, starting trim", total_tokens, request.budget.max_tokens);
    }

    while total_tokens > request.budget.max_tokens {
        let dropped = if drop_last_insight(&mut packet.insight, omissions) {
            true
        } else if drop_last_episode(&mut packet.long_term.episodes, omissions) {
            true
        } else if drop_oldest_turn(&mut packet.short_term.conversation_window, omissions) {
            true
        } else if drop_last_procedure(&mut packet.long_term.procedures, omissions) {
            true
        } else if drop_last_fact(&mut packet.long_term.facts, omissions) {
            true
        } else if drop_last_key_quote(&mut packet.short_term.key_quotes, omissions) {
            true
        } else {
            false
        };

        if !dropped {
            warn!("Unable to trim packet further, stopping at {} tokens", total_tokens);
            break;
        }
        total_tokens = estimate_packet_tokens(packet);
    }
}

fn apply_per_section_budgets(
    request: &BuildRequest,
    packet: &mut MemoryPacket,
    omissions: &mut Vec<Value>,
) {
    if let Some(limit) = per_section_limit(&request.budget, "facts") {
        trim_vec_to_budget(
            &mut packet.long_term.facts,
            limit,
            omissions,
            "facts",
            |item| item.fact_id.clone(),
        );
    }
    if let Some(limit) = per_section_limit(&request.budget, "procedures") {
        trim_vec_to_budget(
            &mut packet.long_term.procedures,
            limit,
            omissions,
            "procedures",
            |item| item.procedure_id.clone(),
        );
    }
    if let Some(limit) = per_section_limit(&request.budget, "episodes") {
        trim_vec_to_budget(
            &mut packet.long_term.episodes,
            limit,
            omissions,
            "episodes",
            |item| item.episode_id.clone(),
        );
    }
    if let Some(limit) = per_section_limit(&request.budget, "insight") {
        trim_insight_to_budget(&mut packet.insight, limit, omissions);
    }
    if let Some(limit) = per_section_limit(&request.budget, "key_quotes") {
        trim_vec_to_budget(
            &mut packet.short_term.key_quotes,
            limit,
            omissions,
            "key_quotes",
            |item| item.evidence_id.clone(),
        );
    }
    if let Some(limit) = per_section_limit(&request.budget, "conversation_window") {
        trim_turns_to_budget(&mut packet.short_term.conversation_window, limit, omissions);
    }
}

fn per_section_limit(budget: &Budget, key: &str) -> Option<u32> {
    budget
        .per_section
        .get(key)
        .and_then(|value| value.as_u64())
        .map(|value| value as u32)
}

fn trim_vec_to_budget<T: Serialize, F: Fn(&T) -> String>(
    items: &mut Vec<T>,
    max_tokens: u32,
    omissions: &mut Vec<Value>,
    section: &str,
    id_fn: F,
) {
    let mut total = estimate_tokens(items);
    while total > max_tokens && !items.is_empty() {
        if let Some(item) = items.pop() {
            omissions.push(json!({
                "section": section,
                "id": id_fn(&item),
                "reason": "section_budget"
            }));
            let item_tokens = estimate_tokens(&item);
            total = total.saturating_sub(item_tokens);
        }
    }
}

fn trim_turns_to_budget(
    turns: &mut Vec<ConversationTurn>,
    max_tokens: u32,
    omissions: &mut Vec<Value>,
) {
    let mut total = estimate_tokens(turns);
    while total > max_tokens && !turns.is_empty() {
        let dropped = turns.remove(0);
        omissions.push(json!({
            "section": "conversation_window",
            "id": dropped.evidence_id,
            "reason": "section_budget"
        }));
        let dropped_tokens = estimate_tokens(&dropped);
        total = total.saturating_sub(dropped_tokens);
    }
}

fn trim_insight_to_budget(insight: &mut Insight, max_tokens: u32, omissions: &mut Vec<Value>) {
    let mut total = estimate_tokens(insight);
    while total > max_tokens {
        let mut dropped_tokens = 0;
        if let Some(item) = insight.hypotheses.pop() {
            omissions.push(json!({
                "section": "insight.hypotheses",
                "id": item.id,
                "reason": "section_budget"
            }));
            dropped_tokens = estimate_tokens(&item);
        } else if let Some(item) = insight.strategy_sketches.pop() {
            omissions.push(json!({
                "section": "insight.strategy_sketches",
                "id": item.id,
                "reason": "section_budget"
            }));
            dropped_tokens = estimate_tokens(&item);
        } else if let Some(item) = insight.patterns.pop() {
            omissions.push(json!({
                "section": "insight.patterns",
                "id": item.id,
                "reason": "section_budget"
            }));
            dropped_tokens = estimate_tokens(&item);
        } else {
            break;
        }
        total = total.saturating_sub(dropped_tokens);
    }
}

fn drop_last_insight(insight: &mut Insight, omissions: &mut Vec<Value>) -> bool {
    if let Some(item) = insight.hypotheses.pop() {
        omissions.push(json!({ "section": "insight.hypotheses", "id": item.id, "reason": "budget" }));
        return true;
    }
    if let Some(item) = insight.strategy_sketches.pop() {
        omissions.push(json!({ "section": "insight.strategy_sketches", "id": item.id, "reason": "budget" }));
        return true;
    }
    if let Some(item) = insight.patterns.pop() {
        omissions.push(json!({ "section": "insight.patterns", "id": item.id, "reason": "budget" }));
        return true;
    }
    false
}

fn drop_last_episode(episodes: &mut Vec<Episode>, omissions: &mut Vec<Value>) -> bool {
    if let Some(item) = episodes.pop() {
        omissions.push(json!({ "section": "episodes", "id": item.episode_id, "reason": "budget" }));
        return true;
    }
    false
}

fn drop_oldest_turn(turns: &mut Vec<ConversationTurn>, omissions: &mut Vec<Value>) -> bool {
    if turns.is_empty() {
        return false;
    }
    let dropped = turns.remove(0);
    omissions.push(json!({
        "section": "conversation_window",
        "id": dropped.evidence_id,
        "reason": "budget"
    }));
    true
}

fn drop_last_procedure(
    procedures: &mut Vec<memweft_types::Procedure>,
    omissions: &mut Vec<Value>,
) -> bool {
    if let Some(item) = procedures.pop() {
        omissions.push(json!({ "section": "procedures", "id": item.procedure_id, "reason": "budget" }));
        return true;
    }
    false
}

fn drop_last_fact(facts: &mut Vec<Fact>, omissions: &mut Vec<Value>) -> bool {
    if let Some(item) = facts.pop() {
        omissions.push(json!({ "section": "facts", "id": item.fact_id, "reason": "budget" }));
        return true;
    }
    false
}

fn drop_last_key_quote(quotes: &mut Vec<KeyQuote>, omissions: &mut Vec<Value>) -> bool {
    if let Some(item) = quotes.pop() {
        omissions.push(json!({ "section": "key_quotes", "id": item.evidence_id, "reason": "budget" }));
        return true;
    }
    false
}

fn estimate_packet_tokens(packet: &MemoryPacket) -> u32 {
    let mut total = 0;
    total += estimate_tokens(&packet.short_term.working_state);
    total += estimate_tokens(&packet.short_term.rolling_summary);
    total += estimate_tokens(&packet.short_term.key_quotes);
    total += estimate_tokens(&packet.short_term.conversation_window);
    total += estimate_tokens(&packet.long_term.facts);
    total += estimate_tokens(&packet.long_term.procedures);
    total += estimate_tokens(&packet.long_term.episodes);
    total += estimate_tokens(&packet.insight);
    total
}

fn compute_section_usage(packet: &MemoryPacket) -> JsonMap {
    let mut usage = JsonMap::new();
    usage.insert(
        "working_state".to_string(),
        json!(estimate_tokens(&packet.short_term.working_state)),
    );
    usage.insert(
        "rolling_summary".to_string(),
        json!(estimate_tokens(&packet.short_term.rolling_summary)),
    );
    usage.insert(
        "key_quotes".to_string(),
        json!(estimate_tokens(&packet.short_term.key_quotes)),
    );
    usage.insert(
        "conversation_window".to_string(),
        json!(estimate_tokens(&packet.short_term.conversation_window)),
    );
    usage.insert(
        "facts".to_string(),
        json!(estimate_tokens(&packet.long_term.facts)),
    );
    usage.insert(
        "procedures".to_string(),
        json!(estimate_tokens(&packet.long_term.procedures)),
    );
    usage.insert(
        "episodes".to_string(),
        json!(estimate_tokens(&packet.long_term.episodes)),
    );
    usage.insert("insight".to_string(), json!(estimate_tokens(&packet.insight)));
    usage
}

fn estimate_tokens<T: Serialize>(value: &T) -> u32 {
    let text = serde_json::to_string(value).unwrap_or_default();
    let chars = text.chars().count();
    ((chars as f64 / 4.0).ceil() as u32).max(1)
}

fn build_explain(request: &BuildRequest, packet: &MemoryPacket) -> JsonMap {
    let mut explain = JsonMap::new();
    explain.insert("policy_id".to_string(), json!(request.policy_id));
    explain.insert(
        "candidate_counts".to_string(),
        json!({
            "facts": packet.long_term.facts.len(),
            "procedures": packet.long_term.procedures.len(),
            "episodes": packet.long_term.episodes.len(),
            "insights": insight_total(&packet.insight),
        }),
    );
    explain.insert(
        "candidate_limits".to_string(),
        json!({
            "max_total": request.policy.max_total_candidates,
            "facts": request.policy.max_facts,
            "procedures": request.policy.max_procedures,
            "episodes": request.policy.max_episodes,
            "insights": request.policy.max_insights,
        }),
    );
    explain.insert(
        "time_window_days".to_string(),
        json!(request.policy.episode_time_window_days),
    );
    explain.insert(
        "determinism".to_string(),
        json!({
            "facts": "fact_key, fact_id",
            "procedures": "priority desc, procedure_id",
            "episodes": "recency_score desc, episode_id",
            "insights": "validation_state desc, confidence desc, id",
        }),
    );
    explain
}

fn default_budget() -> Budget {
    Budget {
        max_tokens: 2048,
        per_section: JsonMap::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{InMemoryStore, StmState};
    use memweft_types::{Fact, FactStatus, InsightItem, InsightType, Validity};
    use crate::WorkingStatePatch;

    fn sample_scope() -> Scope {
        Scope {
            tenant_id: "default".to_string(),
            user_id: "user1".to_string(),
            agent_id: "agent1".to_string(),
            session_id: "session1".to_string(),
            run_id: "run1".to_string(),
        }
    }

    #[test]
    fn builds_packet_with_filters() {
        let store = InMemoryStore::new();
        let scope = sample_scope();

        store
            .patch_working_state(
                &scope,
                WorkingStatePatch {
                    goal: Some("ship v1".to_string()),
                    ..WorkingStatePatch::default()
                },
            )
            .unwrap();

        store
            .update_stm(
                &scope,
                StmState {
                    rolling_summary: "summary".to_string(),
                    key_quotes: vec![KeyQuote {
                        evidence_id: "e1".to_string(),
                        quote: "quote".to_string(),
                        role: memweft_types::Role::User,
                        ts: None,
                    }],
                },
            )
            .unwrap();

        store
            .upsert_fact(
                &scope,
                Fact {
                    fact_id: "f1".to_string(),
                    fact_key: "pref.color".to_string(),
                    value: json!("blue"),
                    status: FactStatus::Active,
                    validity: Validity::default(),
                    confidence: 0.8,
                    sources: vec!["e1".to_string()],
                    scope_level: memweft_types::ScopeLevel::User,
                    notes: String::new(),
                },
            )
            .unwrap();

        store
            .upsert_fact(
                &scope,
                Fact {
                    fact_id: "f2".to_string(),
                    fact_key: "deprecated".to_string(),
                    value: json!("old"),
                    status: FactStatus::Deprecated,
                    validity: Validity::default(),
                    confidence: 0.2,
                    sources: vec![],
                    scope_level: memweft_types::ScopeLevel::User,
                    notes: String::new(),
                },
            )
            .unwrap();

        store
            .append_episode(
                &scope,
                Episode {
                    episode_id: "ep1".to_string(),
                    time_range: memweft_types::TimeRange {
                        start: Utc::now(),
                        end: None,
                    },
                    summary: "did something".to_string(),
                    highlights: vec![],
                    tags: vec!["alpha".to_string()],
                    entities: vec![],
                    sources: vec![],
                    compression_level: memweft_types::CompressionLevel::Raw,
                    recency_score: None,
                },
            )
            .unwrap();

        store
            .append_insight(
                &scope,
                InsightItem {
                    id: "i1".to_string(),
                    kind: InsightType::Hypothesis,
                    statement: "maybe".to_string(),
                    trigger: memweft_types::InsightTrigger::Synthesis,
                    confidence: 0.3,
                    validation_state: memweft_types::ValidationState::Unvalidated,
                    tests_suggested: vec![],
                    expires_at: "run_end".to_string(),
                    sources: vec![],
                },
            )
            .unwrap();

        let mut request = BuildRequest::new(scope.clone(), Purpose::Planner);
        request.cues.tags = vec!["alpha".to_string()];
        let packet = build_memory_packet(&store, request).unwrap();

        assert_eq!(packet.long_term.facts.len(), 1);
        assert_eq!(packet.long_term.episodes.len(), 1);
        assert_eq!(packet.insight.hypotheses.len(), 1);
        assert_eq!(packet.short_term.working_state.goal, "ship v1");
    }
}
