import { createRequire } from "node:module";

export type PoolAccess = "read" | "read_write";
export type ConflictPolicy = "private_first" | "read_order" | "error";
export interface MemoryConfig {
  readPools: { poolId: string; access?: PoolAccess }[];
  defaultWritePool?: string | null;
  conflictPolicy?: ConflictPolicy;
}
export interface Scope { user_id: string; tenant_id?: string; agent_id?: string;
  memory_config?: { read_pools: { pool_id: string; access?: PoolAccess }[]; default_write_pool?: string | null; conflict_policy?: ConflictPolicy }
}
export interface PoolRef { pool_id: string; key: string }
export interface PoolRevision extends PoolRef { revision: number }
export interface PoolFact extends Fact { pool_id: string; revision: number | null; writer_agent_id: string }
export interface PoolWriteOptions { poolId?: string; expectedRevision?: number }

export interface Document { namespace: string[]; key: string; value: Record<string, unknown>; revision: number; created_at: string; updated_at: string }
export interface Fact { fact_id: string; fact_key: string; value: unknown; status: string }
export type Target = "task" | "reflection";
export interface Proposal { task_type: string; target?: Target; content: string; proposer_version: string; source_keys?: string[]; source_pools?: PoolRef[] }
export interface Policy { min_cases: number; min_gain: number; max_case_regression: number; max_candidate_cost: number; max_candidate_latency_ms: number }
export interface Evaluation { dataset_version: string; evaluator_version: string; cases: { case_id: string; baseline_score: number; candidate_score: number; candidate_cost: number; candidate_latency_ms: number }[] }
export interface Strategy { version: string; parent: string | null; proposal: Proposal; pool_revisions: PoolRevision[] }
export interface Job { id: string; proposal: Proposal; policy: Policy; dataset_version: string; evaluator_version: string; case_ids: string[]; baseline: Strategy | null; status: "evaluating" | "accepted" | "rejected" | "failed" | "cancelled"; reason: string; evaluation: Evaluation | null; pool_revisions: PoolRevision[] }
export interface Feedback { id: string; task_type: string; session_id: string; run_id: string; success: boolean; details?: unknown }
export interface StartJob { id: string; proposal: Proposal; policy?: Policy; dataset_version: string; evaluator_version: string; case_ids: string[] }
export interface ContextOptions { maxTokens?: number; conversationWindow?: number; maxFacts?: number; includeMessages?: boolean; taskType?: string; query?: string }
export interface SqliteOptions { backgroundCheckpointMs?: number; walReclaimThresholdBytes?: number | null }
export interface ContextData { text: string; memories: Fact[]; messages: Document[]; strategies: Strategy[]; report: Record<string, unknown> }
export class Context implements ContextData {
  text: string; memories: Fact[]; messages: Document[]; strategies: Strategy[]; report: Record<string, unknown>;
  constructor(data: ContextData) { this.text = data.text; this.memories = data.memories; this.messages = data.messages; this.strategies = data.strategies; this.report = data.report; }
  explain() { return this.report; }
}
interface Native { request(payload: string): Promise<string>; close(): void }

export class Memory {
  private constructor(private readonly native: Native) {}
  close(): void { this.native.close(); }
  storageStatus(): Promise<Record<string, unknown>> { return this.request({op: "storage_status"}); }
  static async open(options: { path?: string; inMemory?: boolean; sqliteOptions?: SqliteOptions } = {}): Promise<Memory> {
    const require = createRequire(import.meta.url);
    let binding;
    try { binding = require("../native/memweft.node"); }
    catch (cause) { throw new Error("MemWeft native addon unavailable. In a source checkout run npm run build:native in typescript/.", { cause }); }
    const sqlite = options.sqliteOptions;
    return new Memory(await binding.NativeMemory.open(options.inMemory ? ":memory:" : options.path ?? "data/memweft.db",
      sqlite && JSON.stringify({ background_checkpoint_ms: sqlite.backgroundCheckpointMs,
        wal_reclaim_threshold_bytes: sqlite.walReclaimThresholdBytes })));
  }
  user(userId: string, options: { tenantId?: string; agentId?: string; memoryConfig?: MemoryConfig } = {}): UserMemory {
    const config = options.memoryConfig;
    return new UserMemory(this, { user_id: userId, tenant_id: options.tenantId, agent_id: options.agentId,
      memory_config: config && { read_pools: config.readPools.map(p => ({ pool_id: p.poolId, access: p.access })),
        default_write_pool: config.defaultWritePool, conflict_policy: config.conflictPolicy } });
  }
  /** Internal versioned Rust request contract; framework adapters share this path. */
  async request<T>(request: Record<string, unknown>): Promise<T> { return JSON.parse(await this.native.request(JSON.stringify(request))) as T; }
}
export class UserMemory {
  readonly learning: Learning;
  constructor(private readonly memory: Memory, readonly scope: Scope) { this.learning = new Learning(this); }
  request<T>(op: string, input: Record<string, unknown> = {}): Promise<T> { return this.memory.request<T>({ op, scope: this.scope, ...input }); }
  remember(value: unknown, options: PoolWriteOptions & { key: string }): Promise<PoolFact> {
    return this.request("remember", { value, key: options.key, pool_id: options.poolId, expected_revision: options.expectedRevision });
  }
  memories(options: { poolId?: string } = {}): Promise<PoolFact[]> { return this.request("memories", { pool_id: options.poolId }); }
  forget(key: string, options: PoolWriteOptions = {}): Promise<boolean> {
    return this.request("forget", { key, pool_id: options.poolId, expected_revision: options.expectedRevision });
  }
  session(sessionId: string): Session { return new Session(this, sessionId); }
}
export class Session {
  constructor(readonly user: UserMemory, readonly sessionId: string) {}
  addMessage(role: "user" | "assistant" | "tool" | "system", content: string, options: { eventId?: string; runId?: string } = {}): Promise<Document> {
    return this.user.request("add_message", { session_id: this.sessionId, role, content, event_id: options.eventId, run_id: options.runId });
  }
  messages(): Promise<Document[]> { return this.user.request("messages", { session_id: this.sessionId }); }
  clear(): Promise<number> { return this.user.request("clear_session", { session_id: this.sessionId }); }
  async context(options: ContextOptions = {}): Promise<Context> {
    const result = await this.user.request<ContextData>("context", { session_id: this.sessionId, options: {
      max_tokens: options.maxTokens, conversation_window: options.conversationWindow,
      max_facts: options.maxFacts, include_messages: options.includeMessages, task_type: options.taskType,
      query: options.query,
    }});
    return new Context(result);
  }
}
export class Learning {
  constructor(private readonly user: UserMemory) {}
  feedback(feedback: Feedback): Promise<Feedback> { return this.user.request("feedback", { feedback }); }
  start(input: StartJob): Promise<Job> { return this.user.request("learning_start", { ...input }); }
  submit(id: string, evaluation: Evaluation): Promise<Job> { return this.user.request("learning_submit", { id, evaluation }); }
  get(id: string): Promise<Job> { return this.user.request("learning_get", { id }); }
  jobs(): Promise<Job[]> { return this.user.request("learning_jobs"); }
  cancel(id: string, reason = ""): Promise<Job> { return this.user.request("learning_cancel", { id, reason }); }
  active(taskType: string, target: Target = "task"): Promise<Strategy | null> { return this.user.request("learning_active", { task_type: taskType, target }); }
  rollback(taskType: string, options: { expectedVersion: string; version?: string | null; target?: Target }): Promise<Strategy | null> {
    return this.user.request("learning_rollback", { task_type: taskType, expected_version: options.expectedVersion, version: options.version ?? null, target: options.target });
  }
}
