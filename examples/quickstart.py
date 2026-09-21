"""Run twice to verify persistence. No API key or framework required."""
from memweft import Memory

memory = Memory("data/quickstart.db")
alice = memory.user("alice")
alice.remember("喜欢简短、直接的回答", key="reply_style")
chat = alice.session("chat-001")
chat.add_message("user", "帮我解释 Rust 的所有权", event_id="question-1")
print(chat.context(max_tokens=1000).text)
print("Stored messages:", len(chat.messages()))
