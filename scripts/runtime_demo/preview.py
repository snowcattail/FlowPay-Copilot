"""Seed and serve a deterministic payment incident for README screenshots.

This is a local simulation. It uses an isolated SQLite event log and no model,
payment, or document service credentials.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]

SESSION_ID = "session_readme_payment_webhook_preview"
RUN_ID = "run_readme_payment_webhook_preview"
DATA_DIR = ROOT / "var" / "readme-preview"


class EventStore:
    """Small append-only SQLite event writer for this isolated preview."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (session_id TEXT, seq INTEGER, data TEXT, PRIMARY KEY(session_id, seq))")

    def read(self, session_id: str) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM events WHERE session_id=? ORDER BY seq", (session_id,))]

    def append(self, session_id: str, items: list[tuple[str, dict]], run_id: str | None = None, call_id: str | None = None, parent_call_id: str | None = None) -> None:
        with sqlite3.connect(self.path) as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id=?", (session_id,)).fetchone()[0]
            for kind, payload in items:
                seq += 1
                event = {"schema_version": 1, "event_id": f"evt_preview_{seq:03}", "session_id": session_id, "seq": seq, "run_id": run_id, "call_id": call_id, "parent_call_id": parent_call_id, "timestamp": datetime.now(UTC).isoformat(), "type": kind, "payload": payload}
                db.execute("INSERT INTO events VALUES (?, ?, ?)", (session_id, seq, json.dumps(event, ensure_ascii=False)))


def seed() -> None:
    store = EventStore(DATA_DIR / "sessions.sqlite")
    if store.read(SESSION_ID):
        return
    workflow = {"id": "s4", "version": "1"}
    question = (
        "生产环境 acct_prod_8821 的订单 ord_20261112_7842 已显示支付成功，"
        "但业务系统没有收到 Webhook。请检查支付状态、投递重试与端点配置，给出可执行的排障结论。"
    )
    evidence = [
        {
            "id": "doc:KB-WEBHOOK-RETRY#c003",
            "title": "Webhook 投递与重试策略",
            "text": "2xx 才视为投递成功。HTTP 410 表示端点已永久失效；重试会保留投递记录，但需先恢复订阅端点并确认签名密钥。",
            "score": 0.96,
            "source": {"collection_id": "flowpay_docs_v1_qwen3_06b", "doc_id": "KB-WEBHOOK-RETRY", "chunk_no": 3, "path": "knowledge/webhook-delivery.md", "sha256": "simulation-preview", "locator": {"section": "失败响应与重试"}, "metadata": {"simulation": True}},
        },
        {
            "id": "doc:KB-PAYMENT-STATE#c002",
            "title": "支付状态与异步通知核对",
            "text": "payment.status=succeeded 表示支付已完成；Webhook 是独立的异步投递链路。不要因为通知失败重复发起扣款。",
            "score": 0.93,
            "source": {"collection_id": "flowpay_docs_v1_qwen3_06b", "doc_id": "KB-PAYMENT-STATE", "chunk_no": 2, "path": "knowledge/payment-lifecycle.md", "sha256": "simulation-preview", "locator": {"section": "成功状态"}, "metadata": {"simulation": True}},
        },
    ]
    answer = """## 排障结论

订单 `ord_20261112_7842` 对应支付 `pay_9f31c2` 已于 **14:26:08 UTC 成功**，金额 ¥1,280.00；没有二次扣款迹象。支付成功与 Webhook 投递是两条独立链路，因此此时不应重新发起支付。[2]

故障落在通知端点：`payment.succeeded` 的三次投递均收到 **HTTP 410 Gone**，最后一次重试耗尽。410 表明当前回调地址已失效；恢复端点和签名配置后，再对该事件执行安全重放。[1]

| 检查项 | 结果 | 下一步 |
| --- | --- | --- |
| 支付记录 | `succeeded`，¥1,280.00 | 保持订单已支付 |
| Webhook 投递 | 3 次失败，410 Gone | 修复 `/hooks/flowpay/v2` 路由 |
| 签名验证 | 端点迁移后待核对 | 核对生产环境签名密钥 |

```bash
# 端点恢复并通过 2xx 健康检查后，按事件 ID 重放（示意）
flowpay events replay evt_pay_6b73d1 --account acct_prod_8821
```

重放后确认接收端返回 `2xx`、事件 ID 去重成功，并核对订单只入账一次。[1][2]
"""
    store.append(SESSION_ID, [("session.created", {"workflow": workflow})])
    store.append(SESSION_ID, [
        ("message.user", {"content": question}),
        ("run.started", {"request_id": "readme-preview", "input": question, "resumed": False, "resumed_from": None, "config": {}, "workflow": workflow}),
    ], RUN_ID)

    def model(call_id: str, messages: list[dict], content: str, tool_calls: list[dict] | None, duration: int, usage: dict) -> None:
        store.append(SESSION_ID, [("model.request", {"request": {"provider": "scripted", "simulation": True, "body": {"model": "payment-incident-preview", "messages": messages, "tools": [], "stream": True}}, "capabilities": {"tools": True, "streaming": True, "reasoning": False}, "output": "answer"})], RUN_ID, call_id)
        if content:
            store.append(SESSION_ID, [("model.delta", {"content": content})], RUN_ID, call_id)
        store.append(SESSION_ID, [("model.response", {"completion": {"id": call_id, "object": "chat.completion", "model": "payment-incident-preview", "choices": [{"index": 0, "message": {"role": "assistant", "content": content, "tool_calls": tool_calls}, "finish_reason": "tool_calls" if tool_calls else "stop"}], "usage": usage}, "simulation": True, "duration_ms": duration})], RUN_ID, call_id)

    def tool(call_id: str, parent: str, name: str, args: dict, result: dict, duration: int) -> None:
        store.append(SESSION_ID, [("tool.started", {"name": name, "arguments": json.dumps(args, ensure_ascii=False)})], RUN_ID, call_id, parent)
        store.append(SESSION_ID, [("tool.result", {"name": name, "result": result, "duration_ms": duration})], RUN_ID, call_id, parent)

    calls = [
        {"id": "call_search_documents", "type": "function", "function": {"name": "search_documents", "arguments": json.dumps({"query": "Webhook HTTP 410 retry payment succeeded", "limit": 5})}},
        {"id": "call_query_payments", "type": "function", "function": {"name": "query_payments", "arguments": json.dumps({"account_id": "acct_prod_8821", "environment": "production", "order_id": "ord_20261112_7842"})}},
        {"id": "call_list_delivery_attempts", "type": "function", "function": {"name": "list_delivery_attempts", "arguments": json.dumps({"account_id": "acct_prod_8821", "environment": "production", "event_id": "evt_pay_6b73d1"})}},
    ]
    model("model_investigate", [{"role": "user", "content": question}], "", calls, 842, {"prompt_tokens": 1184, "completion_tokens": 172, "total_tokens": 1356})
    tool("call_search_documents", "model_investigate", "search_documents", {"query": "Webhook HTTP 410 retry payment succeeded", "limit": 5}, {"hits": evidence, "mode": "hybrid", "count": 2}, 237)
    tool("call_query_payments", "model_investigate", "query_payments", {"account_id": "acct_prod_8821", "environment": "production", "order_id": "ord_20261112_7842"}, {"payment_id": "pay_9f31c2", "status": "succeeded", "amount": 128000, "currency": "CNY", "succeeded_at": "2026-11-12T14:26:08Z", "event_id": "evt_pay_6b73d1"}, 184)
    tool("call_list_delivery_attempts", "model_investigate", "list_delivery_attempts", {"account_id": "acct_prod_8821", "environment": "production", "event_id": "evt_pay_6b73d1"}, {"delivery_id": "del_1a2b3c", "endpoint": "https://merchant.example/hooks/flowpay/v2", "attempts": [{"attempt": 1, "http_status": 410, "duration_ms": 193}, {"attempt": 2, "http_status": 410, "duration_ms": 205}, {"attempt": 3, "http_status": 410, "duration_ms": 198}], "status": "exhausted"}, 219)
    model("model_conclusion", [{"role": "user", "content": question}, {"role": "tool", "content": "支付成功，Webhook HTTP 410，三次投递失败"}], answer, None, 1694, {"prompt_tokens": 2368, "completion_tokens": 548, "total_tokens": 2916})
    store.append(SESSION_ID, [("message.assistant", {"content": answer, "evidence": evidence}), ("run.finished", {"status": "completed", "state": {"simulation": True, "incident": "webhook_410"}})], RUN_ID)


def main() -> None:
    seed()
    print(f"Preview session: {SESSION_ID}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8000), PreviewHandler).serve_forever()


class PreviewHandler(BaseHTTPRequestHandler):
    def respond(self, value: object, status: int = 200) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        store = EventStore(DATA_DIR / "sessions.sqlite")
        events = store.read(SESSION_ID)
        if path == "/api/health":
            return self.respond({"status": "ok"})
        if path == "/api/meta":
            return self.respond({"protocol_version": 1, "workflow": {"id": "s4", "version": "1", "label": "交互式深度排障", "description": "支付与 Webhook 的多步调查",}, "model": "payment-incident-preview · simulated", "examples": [], "reply_hint": "本地仿真会话"})
        if path == "/api/sessions":
            return self.respond([{"id": SESSION_ID, "workflow": {"id": "s4", "version": "1"}, "title": events[1]["payload"]["content"][:36], "updated_at": events[-1]["timestamp"], "last_seq": events[-1]["seq"], "status": "completed"}])
        if path == f"/api/sessions/{SESSION_ID}/events":
            return self.respond(events)
        if path == f"/api/sessions/{SESSION_ID}":
            return self.respond({"id": SESSION_ID, "workflow": {"id": "s4", "version": "1"}, "title": events[1]["payload"]["content"][:36], "status": "completed", "created_at": events[0]["timestamp"], "updated_at": events[-1]["timestamp"], "last_seq": events[-1]["seq"], "state": {}, "messages": [], "runs": []})
        if path == f"/api/sessions/{SESSION_ID}/export":
            encoded = ("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            return self.wfile.write(encoded)
        if path == f"/api/sessions/{SESSION_ID}/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b": preview heartbeat\n\n")
                    self.wfile.flush()
                    import time
                    time.sleep(10)
            except (BrokenPipeError, ConnectionResetError):
                return
        return self.respond({"detail": "Not found"}, 404)


if __name__ == "__main__":
    main()
