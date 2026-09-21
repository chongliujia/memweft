import { BaseStore, type Operation, type OperationResults, type Item } from "@langchain/langgraph-checkpoint";
import { Memory, type Document, type UserMemory, type ContextOptions, type Feedback } from "./index.js";

function encode(op: Operation): Record<string, unknown> {
  if ("value" in op) {
    if (op.index !== undefined && op.index !== false) throw new Error("MemWeft Store does not support vector indexing");
    return { kind: "put", namespace: op.namespace, key: op.key, value: op.value };
  }
  if ("key" in op) return { kind: "get", namespace: op.namespace, key: op.key };
  if ("namespacePrefix" in op) {
    if (op.query !== undefined) throw new Error("MemWeft Store does not support semantic search");
    return { kind: "search", prefix: op.namespacePrefix, filter: op.filter, limit: op.limit ?? 10, offset: op.offset ?? 0 };
  }
  return { kind: "list", conditions: (op.matchConditions ?? []).map(c => ({ match_type: c.matchType, path: c.path })),
    max_depth: op.maxDepth ?? null, limit: op.limit, offset: op.offset };
}
function item(d: Document): Item { return { namespace: d.namespace, key: d.key, value: d.value, createdAt: new Date(d.created_at), updatedAt: new Date(d.updated_at) }; }

/** A per-user LangGraph BaseStore. Existing graph checkpointers remain independent. */
export class MemWeftStore extends BaseStore {
  private readonly user: UserMemory;
  constructor(memory: Memory, userId: string, options: { tenantId?: string; agentId?: string } = {}) { super(); this.user = memory.user(userId, options); }
  async batch<Op extends Operation[]>(operations: Op): Promise<OperationResults<Op>> {
    const encoded = operations.map(encode);
    const results = await this.user.request<unknown[]>("store_batch", { operations: encoded });
    return results.map((result, i) => {
      const op = encoded[i];
      if (op.kind === "get") return result === null ? null : item(result as Document);
      if (op.kind === "search") return (result as Document[]).map(item);
      if (op.kind === "put") return undefined;
      return result;
    }) as OperationResults<Op>;
  }
}

/** Explicit memory/context and feedback access for graph nodes. */
export class LangGraphMemory {
  constructor(readonly user: UserMemory) {}
  context(threadId: string, options: ContextOptions = {}) {
    return this.user.session(threadId).context({ ...options, includeMessages: false });
  }
  feedback(feedback: Feedback) { return this.user.learning.feedback(feedback); }
}
