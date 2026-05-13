import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

# 上游模型服务地址
HERMES = "http://localhost:8642"

# 简单鉴权 Key（用于转发给上游）
API_KEY = "change-me-local-dev"

app = FastAPI()

def is_valid_openai_chunk(obj):
    # 判断是否为 OpenAI 流式 chunk（必须包含 choices 或 error）
    return isinstance(obj, dict) and ("choices" in obj or "error" in obj)

@app.get("/v1/models")
async def models():
    # 模拟 OpenAI models 接口
    return {
        "object": "list",
        "data": [
            {
                "id": "hermes-agent",
                "object": "model",
                "created": 0,
                "owned_by": "openai"
            }
        ]
    }

@app.post("/v1/chat/completions")
async def proxy(req: Request):
    # 读取 OpenAI 请求体
    body = await req.json()

    # 是否流式输出
    stream = body.get("stream", False)

    # 转发给上游的请求头
    headers = {
        "Authorization": f"Bearer {API_KEY}"
    }

    # =========================
    # 流式模式（SSE 转发）
    # =========================
    if stream:
        async def gen():
            async with httpx.AsyncClient(timeout=None) as client:
                # 请求上游流式接口
                async with client.stream(
                    "POST",
                    f"{HERMES}/v1/chat/completions",
                    json=body,
                    headers=headers
                ) as r:

                    # 逐行读取 SSE 数据
                    async for line in r.aiter_lines():
                        if not line:
                            continue

                        # 只处理 data: 开头的 SSE 数据
                        if not line.startswith("data:"):
                            continue

                        data = line[5:].strip()

                        # 流结束标记
                        if data == "[DONE]":
                            yield "data: [DONE]\n\n"
                            return

                        # JSON 解析
                        try:
                            obj = json.loads(data)
                        except Exception:
                            continue

                        # 只透传合法 OpenAI chunk
                        if is_valid_openai_chunk(obj):
                            yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    # =========================
    # 非流式模式（普通 JSON）
    # =========================
    else:
        async with httpx.AsyncClient(timeout=None) as client:
            # 请求上游
            r = await client.post(
                f"{HERMES}/v1/chat/completions",
                json=body,
                headers=headers
            )

        # JSON 解析失败处理
        try:
            obj = r.json()
        except Exception:
            return JSONResponse({"error": "invalid upstream"}, status_code=500)

        # =========================
        # 兼容非 OpenAI 标准返回
        # =========================
        if "choices" not in obj:
            # 尝试提取可用内容字段
            content = (
                obj.get("content")
                or obj.get("message")
                or json.dumps(obj, ensure_ascii=False)
            )

            # 包装成 OpenAI chat.completion 格式
            obj = {
                "id": "chatcmpl-proxy",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "hermes-agent"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": content
                        },
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            }

        # 返回标准 OpenAI JSON
        return JSONResponse(obj)
