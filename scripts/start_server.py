#!/usr/bin/env python3
"""Start the Robot backend server with optional gRPC ASR/TTS services."""
import atexit
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
AICHATSERVER_PATH = os.getenv("AICHATSERVER_PATH", str(ROOT_DIR / "../AIChatServer"))
ASR_PORT = int(os.getenv("AI_CHAT_GRPC_ASR_PORT", "8011"))
TTS_PORT = int(os.getenv("AI_CHAT_GRPC_TTS_PORT", "8012"))
GRPC_LOG_FILE = ROOT_DIR / ".grpc_servers.log"

if AICHATSERVER_PATH not in sys.path:
    sys.path.insert(0, AICHATSERVER_PATH)


def _load_dotenv() -> dict[str, str]:
    """Read key=value pairs from .env and return as dict."""
    env_path = ROOT_DIR / ".env"
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip()
    return result


import uvicorn

grpc_proc: subprocess.Popen | None = None


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _start_grpc() -> subprocess.Popen | None:
    if _is_port_open(ASR_PORT) and _is_port_open(TTS_PORT):
        print(f"[gRPC] 已在运行: ASR={ASR_PORT}, TTS={TTS_PORT}")
        return None

    print(f"[gRPC] 启动 ASR={ASR_PORT}, TTS={TTS_PORT} (日志: {GRPC_LOG_FILE})")
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{AICHATSERVER_PATH}:{env.get('PYTHONPATH', '')}"
    dotenv = _load_dotenv()
    qwen_key = dotenv.get("ROBOT_QWEN_API_KEY") or dotenv.get("QWEN_API_KEY", "")
    if qwen_key:
        env["QWEN_API_KEY"] = qwen_key
    log = GRPC_LOG_FILE.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "aichat.grpc_servers",
         "--asr-port", str(ASR_PORT), "--tts-port", str(TTS_PORT)],
        stdout=log, stderr=subprocess.STDOUT,
        env=env, cwd=AICHATSERVER_PATH,
    )

    import time
    for _ in range(40):
        if proc.poll() is not None:
            print(f"[gRPC] 启动失败，查看日志: {GRPC_LOG_FILE}")
            return None
        if _is_port_open(ASR_PORT) and _is_port_open(TTS_PORT):
            print("[gRPC] 启动成功")
            return proc
        time.sleep(0.25)

    print(f"[gRPC] 启动超时，查看日志: {GRPC_LOG_FILE}")
    return proc


def _cleanup():
    global grpc_proc
    if grpc_proc and grpc_proc.poll() is None:
        print("[gRPC] 正在关闭...")
        grpc_proc.terminate()
        try:
            grpc_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            grpc_proc.kill()
        grpc_proc = None


if __name__ == "__main__":
    os.chdir(ROOT_DIR)
    # The separately supplied speech SDK may bind unauthenticated gRPC services.
    # Start it only when the operator has explicitly opted in and secured ports.
    if os.getenv("ROBOT_START_GRPC", "0").lower() in {"1", "true", "yes"}:
        grpc_proc = _start_grpc()
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    uvicorn.run(
        "robot.main:app",
        host=os.getenv("ROBOT_LISTEN_HOST", "127.0.0.1"),
        port=8000,
        reload=True,
    )
