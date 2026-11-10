"""电商小二 · 全链路自检脚本。

在五个应用都启动之后运行，用真实的 HTTP / WebSocket 请求走一遍主要业务流程：
AI 咨询 → 连续提问合并 → 规则问答 → 越权访问保护 → 转人工 → 客服接入与回复 → 结束服务交还 AI → 管理端指标。

用法：
    python scripts/xiaoer.py test            （推荐，由启动器选择解释器）
    customer-service/.venv/…/python scripts/smoke_test.py

脚本使用演示账号 user_002（王芳）与 agent_001（客服小周），只产生对话记录，不会修改订单。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

import httpx
import websockets

CUSTOMER_URL = os.environ.get("CUSTOMER_SERVICE_URL", "http://127.0.0.1:8000")
ECOMMERCE_URL = os.environ.get("ECOMMERCE_SERVICE_URL", "http://127.0.0.1:8001")
AI_URL = os.environ.get("AI_SERVICE_URL", "http://127.0.0.1:8002")
REPLY_TIMEOUT = float(os.environ.get("SMOKE_REPLY_TIMEOUT", "90"))

results: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return ok


class Realtime:
    """一个已认证的 WebSocket 连接，把收到的事件放进队列。"""

    def __init__(self, token: str):
        self._token = token
        self._socket = None
        self._task: asyncio.Task | None = None
        self.events: asyncio.Queue[dict] = asyncio.Queue()

    async def __aenter__(self) -> "Realtime":
        url = CUSTOMER_URL.replace("http", "ws", 1) + "/api/v1/realtime"
        self._socket = await websockets.connect(url, proxy=None)
        await self._socket.send(json.dumps({"type": "authenticate", "token": self._token}))
        first = json.loads(await asyncio.wait_for(self._socket.recv(), 10))
        if first.get("event_type") != "connected":
            raise RuntimeError(f"实时通道认证失败：{first}")
        self._task = asyncio.create_task(self._pump())
        return self

    async def _pump(self) -> None:
        try:
            async for raw in self._socket:
                await self.events.put(json.loads(raw))
        except Exception:  # noqa: BLE001 - 连接关闭
            pass

    async def next(self, predicate, timeout: float = REPLY_TIMEOUT) -> dict | None:
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                event = await asyncio.wait_for(self.events.get(), remaining)
            except TimeoutError:
                return None
            if predicate(event):
                return event
        return None

    def drain(self) -> list[dict]:
        drained = []
        while not self.events.empty():
            drained.append(self.events.get_nowait())
        return drained

    async def __aexit__(self, *_) -> None:
        if self._task:
            self._task.cancel()
        if self._socket:
            await self._socket.close()


async def login(client: httpx.AsyncClient, user_id: str, role: str) -> str:
    response = await client.post(
        f"{ECOMMERCE_URL}/api/v1/auth/demo-token", json={"user_id": user_id, "role": role}
    )
    response.raise_for_status()
    return response.json()["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def send(client: httpx.AsyncClient, token: str, content: dict, message_type: str = "text") -> None:
    response = await client.post(
        f"{CUSTOMER_URL}/api/v1/chat/messages",
        headers=bearer(token),
        json={"message_id": f"smoke_{uuid.uuid4().hex}", "type": message_type, "content": content},
    )
    response.raise_for_status()


def is_reply(event: dict) -> bool:
    return event.get("role") in ("AI", "HUMAN")


def text_of(event: dict | None) -> str:
    return ((event or {}).get("content") or {}).get("text") or ""


async def close_open_handoffs(client: httpx.AsyncClient, staff_token: str, user_id: str) -> None:
    """上一次自检如果中途退出，可能留下未结束的工单；先清理，保证本次从 AI 模式开始。"""
    handoffs = (await client.get(f"{CUSTOMER_URL}/api/v1/handoffs", headers=bearer(staff_token))).json()
    for handoff in handoffs:
        if handoff["user_id"] != user_id:
            continue
        base = f"{CUSTOMER_URL}/api/v1/handoffs/{handoff['id']}"
        if handoff["status"] == "WAITING":
            await client.post(f"{base}/accept", headers=bearer(staff_token))
        await client.post(f"{base}/resolve", headers=bearer(staff_token))


async def main() -> int:
    print("电商小二 · 全链路自检\n")
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        # ------------------------------------------------------------ 1. 健康检查
        print("1. 服务健康检查")
        health: dict[str, dict] = {}
        for name, url in (("客服服务", CUSTOMER_URL), ("电商服务", ECOMMERCE_URL), ("智能服务", AI_URL)):
            try:
                response = await client.get(f"{url}/health")
                health[name] = response.json()
                detail = "、".join(f"{k}={v}" for k, v in health[name].items() if k not in ("status", "service"))
                record(response.status_code == 200, f"{name} {url}", detail)
            except Exception as exc:  # noqa: BLE001
                record(False, f"{name} {url}", f"无法连接：{exc!r}")
        if not all(ok for ok, _, _ in results):
            print("\n有服务没有启动，请先运行 start.bat（或 python scripts/xiaoer.py start）。")
            return 1

        user_token = await login(client, "user_002", "user")
        staff_token = await login(client, "agent_001", "staff")
        admin_token = await login(client, "admin_001", "admin")
        await close_open_handoffs(client, staff_token, "user_002")

        async with Realtime(user_token) as user_ws, Realtime(staff_token) as staff_ws:
            # -------------------------------------------------------- 2. AI 咨询
            print("\n2. AI 咨询（用户端 → 客服服务 → 智能服务 → 电商服务）")
            await send(client, user_token, {"text": "你好"})
            reply = await user_ws.next(is_reply)
            record(reply is not None and reply["role"] == "AI" and bool(text_of(reply)), "问候得到 AI 回复", text_of(reply)[:40])

            await send(client, user_token, {"text": "帮我查一下 ORDER_004 的状态"})
            reply = await user_ws.next(is_reply)
            record("ORDER_004" in text_of(reply), "订单查询使用了电商服务的真实数据", text_of(reply).replace("\n", " ")[:60])
            action = ((reply or {}).get("content") or {}).get("page_action") or {}
            record(str(action.get("href", "")).startswith("/me/orders/ORDER_004"), "回复带有站内页面入口", action.get("href", ""))

            await send(client, user_token, {"object_type": "product", "object_id": "PRODUCT_001"}, "object")
            reply = await user_ws.next(is_reply)
            record("Aurora Pro" in text_of(reply), "商品卡片咨询返回商品信息", text_of(reply).replace("\n", " ")[:50])

            # -------------------------------------------------------- 3. 连续提问合并
            print("\n3. 连续提问合并")
            user_ws.drain()
            await send(client, user_token, {"text": "我的订单"})
            await send(client, user_token, {"text": "什么时候发货"})
            reply = await user_ws.next(is_reply)
            extra = await user_ws.next(is_reply, timeout=4)
            record(reply is not None and extra is None, "两条连续消息只产生一次 AI 回复", text_of(reply).replace("\n", " ")[:50])

            # -------------------------------------------------------- 4. 平台规则
            print("\n4. 平台规则（知识库）")
            await send(client, user_token, {"text": "退款多久到账"})
            reply = await user_ws.next(is_reply)
            record("工作日" in text_of(reply), "规则问题由知识库回答", text_of(reply).replace("\n", " ")[:60])

            # -------------------------------------------------------- 5. 访问权限
            print("\n5. 访问权限")
            await send(client, user_token, {"text": "ORDER_001 现在什么状态"})
            reply = await user_ws.next(is_reply)
            leaked = any(word in text_of(reply) for word in ("Aurora", "世纪大道", "899"))
            record(reply is not None and not leaked, "不会泄露其他用户的订单", text_of(reply).replace("\n", " ")[:50])
            response = await client.get(f"{CUSTOMER_URL}/api/v1/handoffs", headers=bearer(user_token))
            record(response.status_code == 403, "普通用户不能访问人工工作台接口", f"HTTP {response.status_code}")
            response = await client.post(f"{AI_URL}/internal/v1/agent/runs", headers=bearer(user_token), json={})
            record(response.status_code == 403, "智能服务内部接口拒绝非客服服务的调用", f"HTTP {response.status_code}")

            # -------------------------------------------------------- 6. 人工转接
            print("\n6. 转人工 → 客服接入 → 回复 → 结束服务")
            staff_ws.drain()
            await send(client, user_token, {"text": "转人工"})
            reply = await user_ws.next(is_reply)
            record(
                (reply or {}).get("conversation_mode") == "QUEUED"
                and ((reply or {}).get("content") or {}).get("reply_type") == "REQUEST_HANDOFF",
                "AI 请求转人工，会话进入排队",
                text_of(reply)[:40],
            )
            record(await staff_ws.next(lambda e: True, timeout=10) is not None, "管理端实时收到新工单通知")

            handoffs = (await client.get(f"{CUSTOMER_URL}/api/v1/handoffs", headers=bearer(staff_token))).json()
            handoff = next((item for item in handoffs if item["user_id"] == "user_002"), None)
            if not record(handoff is not None and handoff["status"] == "WAITING", "工单出现在待处理队列", (handoff or {}).get("summary", "")[:50]):
                return summarize()
            base = f"{CUSTOMER_URL}/api/v1/handoffs/{handoff['id']}"

            transcript = (await client.get(f"{base}/messages", headers=bearer(staff_token))).json()
            record(len(transcript["messages"]) >= 6, "客服能看到原会话的完整上下文", f"{len(transcript['messages'])} 条消息")

            response = await client.post(f"{base}/accept", headers=bearer(staff_token))
            reply = await user_ws.next(lambda e: e.get("role") == "HUMAN")
            record(response.status_code == 200 and (reply or {}).get("conversation_mode") == "HUMAN", "客服接入，用户收到接入提示", text_of(reply)[:40])
            response = await client.post(f"{base}/accept", headers=bearer(staff_token))
            record(response.status_code == 409, "重复接入被拒绝（防止抢单）", f"HTTP {response.status_code}")

            staff_ws.drain()
            await send(client, user_token, {"text": "我的手表什么时候能发货？"})
            event = await staff_ws.next(lambda e: e.get("role") == "USER", timeout=10)
            record(text_of(event) == "我的手表什么时候能发货？", "人工服务期间，用户消息实时送到管理端")
            no_ai = await user_ws.next(lambda e: e.get("role") == "AI", timeout=4)
            record(no_ai is None, "人工服务期间 AI 不再插话")

            await client.post(f"{base}/messages", headers=bearer(staff_token), json={"text": "您好，已为您加急处理。"})
            reply = await user_ws.next(lambda e: e.get("role") == "HUMAN")
            record(text_of(reply) == "您好，已为您加急处理。", "客服回复实时送达用户")

            await client.post(f"{base}/resolve", headers=bearer(staff_token))
            reply = await user_ws.next(lambda e: e.get("role") == "HUMAN")
            record((reply or {}).get("conversation_mode") == "AI", "结束人工服务，会话交还 AI", text_of(reply)[:40])

            await send(client, user_token, {"text": "谢谢"})
            reply = await user_ws.next(is_reply)
            record((reply or {}).get("role") == "AI", "之后的消息重新由 AI 接待", text_of(reply)[:30])

        # ------------------------------------------------------------ 7. 管理端
        print("\n7. 管理端数据")
        service = (await client.get(f"{CUSTOMER_URL}/api/v1/admin/metrics", headers=bearer(admin_token))).json()
        record(service.get("conversations", 0) >= 1 and service.get("handoffs", 0) >= 1, "客服服务指标", f"会话 {service.get('conversations')}，人工介入 {service.get('handoffs')}")
        commerce = (await client.get(f"{ECOMMERCE_URL}/api/v1/admin/summary", headers=bearer(admin_token))).json()
        record(commerce.get("orders", 0) >= 1, "电商服务汇总", f"商品 {commerce.get('products')}，订单 {commerce.get('orders')}，售后 {commerce.get('after_sales')}")
        ai = (await client.get(f"{AI_URL}/api/v1/admin/metrics", headers=bearer(admin_token))).json()
        record(ai.get("agent_runs", 0) >= 1, "智能服务指标", f"AI Runs {ai.get('agent_runs')}，平均 {ai.get('average_latency_ms')} ms")
        runs = (await client.get(f"{AI_URL}/api/v1/runs", headers=bearer(admin_token))).json()
        detail = (await client.get(f"{AI_URL}/api/v1/runs/{runs[0]['id']}", headers=bearer(admin_token))).json()
        record("run" in detail and "tool_calls" in detail, "运行审计可查看结果与工具调用", f"最近一次：{detail['run']['state']}（{detail['run']['model_name']}）")
        chunks = (await client.get(f"{AI_URL}/api/v1/knowledge", headers=bearer(admin_token))).json()
        record(len(chunks) >= 1, "知识库已有内容", f"{len(chunks)} 个切片")
        response = await client.get(f"{AI_URL}/api/v1/runs", headers=bearer(staff_token))
        record(response.status_code == 403, "客服账号不能查看管理员的运行审计", f"HTTP {response.status_code}")

    return summarize()


def summarize() -> int:
    failed = [name for ok, name, _ in results if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"自检未通过：{len(failed)} / {len(results)} 项失败")
        for name in failed:
            print(f"  - {name}")
        return 1
    print(f"自检通过：{len(results)} 项全部成功")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
