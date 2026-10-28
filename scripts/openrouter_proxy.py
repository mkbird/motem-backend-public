"""
透明 HTTP → HTTPS 反代，用来 dump SDK ↔ OpenRouter 的请求/响应。

用途：诊断 Claude Code SDK 子进程发给 OpenRouter 的真实 messages 体积
构成（system / tools / messages 各占多少 token，cache_creation 是否生效
等）。SDK 的 --debug api 只写日志摘要，看不到 request body；本 proxy
把完整 JSON 落盘。

启动：
    conda run -n robot python scripts/openrouter_proxy.py

CLI 看到 plain http://localhost:8080，无需 TLS-MITM；
proxy 内部用 https 转发到 https://openrouter.ai。

切换 backend 走 proxy：
    .env: ROBOT_ANTHROPIC_BASE_URL="http://127.0.0.1:8080/api"
然后完整重启 backend（uvicorn --reload 不读 .env，需要 kill 整树重启）。

dump 文件：/tmp/robot-claude-debug/req_<ts>.json + resp_<ts>.json
"""
import json
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8080
UPSTREAM = "https://openrouter.ai"
DUMP_DIR = Path("/tmp/robot-claude-debug")
DUMP_DIR.mkdir(parents=True, exist_ok=True)


async def proxy(request: web.Request) -> web.StreamResponse:
    ts = int(time.time() * 1000)
    raw_body = await request.read()
    upstream_url = UPSTREAM + request.path_qs

    req_headers = dict(request.headers)
    for k in list(req_headers):
        if k.lower() in ("authorization", "x-api-key"):
            v = req_headers[k]
            req_headers[k] = v[:12] + "..." + v[-4:] if len(v) > 20 else "***"
    req_dump = {
        "ts_ms": ts,
        "method": request.method,
        "url": upstream_url,
        "headers": req_headers,
        "body_size": len(raw_body),
    }
    try:
        req_dump["body_json"] = json.loads(raw_body.decode("utf-8"))
    except Exception:
        req_dump["body_raw_preview"] = raw_body[:500].decode("utf-8", "replace")

    req_file = DUMP_DIR / f"req_{ts}.json"
    req_file.write_text(json.dumps(req_dump, ensure_ascii=False, indent=2))
    print(f"[REQ] {request.method} {request.path_qs}  body={len(raw_body):>7}B  → {req_file.name}")

    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "content-length")}

    async with ClientSession(timeout=ClientTimeout(total=600)) as sess:
        async with sess.request(
            request.method, upstream_url,
            headers=fwd_headers, data=raw_body, allow_redirects=False,
        ) as upstream_resp:
            resp_headers = {k: v for k, v in upstream_resp.headers.items()
                            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")}
            resp = web.StreamResponse(status=upstream_resp.status, headers=resp_headers)
            await resp.prepare(request)

            chunks = []
            usage_seen = None
            async for chunk in upstream_resp.content.iter_any():
                chunks.append(chunk)
                await resp.write(chunk)
                try:
                    txt = chunk.decode("utf-8", "ignore")
                    if '"usage"' in txt and '"input_tokens"' in txt:
                        for line in txt.splitlines():
                            line = line.strip()
                            if line.startswith("data:"):
                                line = line[5:].strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                                if isinstance(obj, dict) and "usage" in obj:
                                    usage_seen = obj["usage"]
                                elif isinstance(obj, dict) and "message" in obj:
                                    u = obj.get("message", {}).get("usage")
                                    if u:
                                        usage_seen = u
                            except Exception:
                                pass
                except Exception:
                    pass

            await resp.write_eof()

            full_body = b"".join(chunks)
            resp_dump = {
                "ts_ms": ts,
                "status": upstream_resp.status,
                "body_size": len(full_body),
                "usage": usage_seen,
                "headers": dict(upstream_resp.headers),
                "body_tail_4kb": full_body[-4096:].decode("utf-8", "replace"),
            }
            resp_file = DUMP_DIR / f"resp_{ts}.json"
            resp_file.write_text(json.dumps(resp_dump, ensure_ascii=False, indent=2))
            print(f"[RESP] {upstream_resp.status}  body={len(full_body):>7}B  usage={usage_seen}  → {resp_file.name}")

            return resp


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def main() -> None:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/_health", health)
    app.router.add_route("*", "/{tail:.*}", proxy)
    print(f"[proxy] listening on http://{LISTEN_HOST}:{LISTEN_PORT}  → {UPSTREAM}")
    print(f"[proxy] dump dir: {DUMP_DIR}")
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()
