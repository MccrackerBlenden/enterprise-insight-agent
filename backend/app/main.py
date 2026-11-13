"""FastAPI backend with actual SSE progress events for the LangGraph agent."""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .data import ensure_demo_db
from .db import overview
from .config import is_ready
from .ops.api import router as ops_router, store as ops_store, task_store as ops_tasks


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_demo_db()
    ops_store.init()
    ops_tasks.init()
    yield


app = FastAPI(title="Enterprise Insight Agent API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=False, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "X-Ops-Demo-Token"])
app.include_router(ops_router)


class RunRequest(BaseModel):
    question: str = Field(min_length=5, max_length=500)


@app.get("/api/health")
def health():
    return {"status": "ok", "model_ready": is_ready(), "synthetic_data": True}


@app.get("/api/overview")
def summary():
    return overview()


@app.post("/api/run/stream")
def stream_run(request: RunRequest):
    def events() -> Iterator[str]:
        yield 'event: status\ndata: {"message":"任务已接收"}\n\n'
        if not is_ready():
            yield 'event: error\ndata: {"message":"尚未配置模型：在 backend/.env 填写 OPENAI_API_KEY 和 OPENAI_MODEL；当前未运行真实 Agent。"}\n\n'
            return
        try:
            from .agent import run_agent_stream
            for event_type, payload in run_agent_stream(request.question):
                yield f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception:
            yield 'event: error\ndata: {"message":"执行中出现未预期的内部错误，请查看后端日志"}\n\n'

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
