#!/usr/bin/env python3
"""一键启动本地后端 + ngrok 公网暴露。"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import socket
import threading
import time
from pathlib import Path
from urllib.request import urlopen


ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8000"))
AICHATSERVER_PATH = os.getenv("AICHATSERVER_PATH", str(ROOT_DIR / "../AIChatServer"))
AI_CHAT_GRPC_ASR_PORT = int(os.getenv("AI_CHAT_GRPC_ASR_PORT", "8011"))
AI_CHAT_GRPC_TTS_PORT = int(os.getenv("AI_CHAT_GRPC_TTS_PORT", "8012"))
PYTHON_BIN = os.getenv("PYTHON_BIN", sys.executable)
UVICORN_HOST = os.getenv("UVICORN_HOST", "127.0.0.1")
KILL_EXISTING_BACKEND = os.getenv("KILL_EXISTING_BACKEND", "0") in ("1", "true", "True")

NGROK_LOG_FILE = Path(os.getenv("NGROK_LOG_FILE", str(ROOT_DIR / ".ngrok.log")))
UVICORN_LOG_FILE = Path(os.getenv("UVICORN_LOG_FILE", str(ROOT_DIR / ".uvicorn.log")))
GRPC_LOG_FILE = Path(os.getenv("GRPC_LOG_FILE", str(ROOT_DIR / ".grpc_servers.log")))

uvicorn_proc: subprocess.Popen | None = None
ngrok_proc: subprocess.Popen | None = None
grpc_proc: subprocess.Popen | None = None
backend_started_by_script = False
ngrok_started_by_script = False
grpc_started_by_script = False
_stop_event = threading.Event()
_external_backend_pids: list[int] = []


def _check_command(cmd: str, error_msg: str) -> None:
    if subprocess.run(["which", cmd], capture_output=True, text=True).returncode != 0:
        print(error_msg)
        sys.exit(1)


def _get_ngrok_https_url() -> str:
    try:
        with urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as resp:
            data = json.load(resp)
    except Exception:
        return ""

    expected_addr = f"localhost:{BACKEND_PORT}"
    for tunnel in data.get("tunnels", []):
        public_url = tunnel.get("public_url", "")
        addr = str(tunnel.get("config", {}).get("addr", ""))
        if public_url.startswith("https://") and addr == expected_addr:
            return public_url
    return ""


def _extract_ngrok_url_from_log(log_file: Path) -> str:
    """Fallback for ERR_NGROK_334: reuse already-online endpoint URL from log."""
    if not log_file.exists():
        return ""
    content = log_file.read_text(encoding="utf-8", errors="ignore")
    # Example: The endpoint 'https://xxxx.ngrok-free.dev' is already online.
    match = re.search(r"'(https://[^']+)'", content)
    return match.group(1) if match else ""

def _is_port_open(host: str, port: int, timeout_s: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _cleanup(*_: object) -> None:
    global uvicorn_proc, ngrok_proc, grpc_proc
    _stop_event.set()

    def terminate_pids(pids: list[int]) -> None:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        for _ in range(20):
            alive = False
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    alive = True
                except Exception:
                    pass
            if not alive:
                return
            time.sleep(0.1)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

    def terminate_proc(proc: subprocess.Popen | None, started_by_script: bool) -> None:
        if not started_by_script or proc is None:
            return
        if proc.poll() is not None:
            return

        try:
            # Prefer terminating the whole process group
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                return

        # Escalate if still alive
        for _ in range(20):
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    terminate_proc(uvicorn_proc, backend_started_by_script)
    terminate_proc(ngrok_proc, ngrok_started_by_script)
    terminate_proc(grpc_proc, grpc_started_by_script)

    # Optionally stop backend we didn't start (process(es) listening on BACKEND_PORT)
    if KILL_EXISTING_BACKEND and _external_backend_pids:
        terminate_pids(_external_backend_pids)


def _pids_listening_on_port(port: int) -> list[int]:
    """Return PIDs listening on TCP port (macOS)."""
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            pass
    # de-dup
    return sorted(set(pids))


def main() -> int:
    global uvicorn_proc, ngrok_proc, grpc_proc
    global ngrok_started_by_script, backend_started_by_script, grpc_started_by_script

    os.chdir(ROOT_DIR)

    _check_command("ngrok", "错误: 未找到 ngrok，请先安装并完成 ngrok config add-authtoken。")
    _check_command("uvicorn", "错误: 未找到 uvicorn，请先激活环境（例如 conda activate robot）。")

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{AICHATSERVER_PATH}:{env.get('PYTHONPATH', '')}"

    # Pass API key to gRPC subprocess explicitly — don't rely on
    # gRPC service searching for .env files by relative path.
    from robot.config import settings as _settings
    if _settings.dashscope_api_key and "QWEN_API_KEY" not in env:
        env["QWEN_API_KEY"] = _settings.dashscope_api_key

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    # Ensure ASR/TTS gRPC services are available for speech mode.
    if _is_port_open("127.0.0.1", AI_CHAT_GRPC_ASR_PORT) and _is_port_open("127.0.0.1", AI_CHAT_GRPC_TTS_PORT):
        print(f"检测到 gRPC 服务已运行：ASR={AI_CHAT_GRPC_ASR_PORT}, TTS={AI_CHAT_GRPC_TTS_PORT}")
    else:
        print(f"启动 AIChatServer gRPC: ASR={AI_CHAT_GRPC_ASR_PORT}, TTS={AI_CHAT_GRPC_TTS_PORT}")
        with GRPC_LOG_FILE.open("w", encoding="utf-8") as grpc_log:
            grpc_proc = subprocess.Popen(
                [
                    PYTHON_BIN,
                    "-m",
                    "aichat.grpc_servers",
                    "--asr-port",
                    str(AI_CHAT_GRPC_ASR_PORT),
                    "--tts-port",
                    str(AI_CHAT_GRPC_TTS_PORT),
                ],
                stdout=grpc_log,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=AICHATSERVER_PATH,
                start_new_session=True,
            )
        grpc_started_by_script = True

        for _ in range(40):
            if grpc_proc.poll() is not None:
                print(f"gRPC 服务启动失败，请查看日志: {GRPC_LOG_FILE}")
                return 1
            if _is_port_open("127.0.0.1", AI_CHAT_GRPC_ASR_PORT) and _is_port_open("127.0.0.1", AI_CHAT_GRPC_TTS_PORT):
                break
            time.sleep(0.25)
        else:
            print(f"gRPC 服务启动超时，请查看日志: {GRPC_LOG_FILE}")
            return 1

    if _is_port_open("127.0.0.1", BACKEND_PORT):
        uvicorn_proc = None
        backend_started_by_script = False
        if KILL_EXISTING_BACKEND:
            global _external_backend_pids
            _external_backend_pids = _pids_listening_on_port(BACKEND_PORT)
            if _external_backend_pids:
                print(
                    f"检测到后端已在 127.0.0.1:{BACKEND_PORT} 运行（PID={_external_backend_pids}），将随 Ctrl+C 一并停止。"
                )
            else:
                print(
                    f"检测到端口 {BACKEND_PORT} 已在使用，但无法解析 PID；Ctrl+C 仅退出脚本，不保证停掉后端。"
                )
        else:
            print(f"检测到后端已在 127.0.0.1:{BACKEND_PORT} 运行，直接复用。")
    else:
        print(f"启动后端: http://{UVICORN_HOST}:{BACKEND_PORT}")
        with UVICORN_LOG_FILE.open("w", encoding="utf-8") as uvicorn_log:
            uvicorn_proc = subprocess.Popen(
                ["uvicorn", "robot.main:app", "--host", UVICORN_HOST, "--port", str(BACKEND_PORT)],
                stdout=uvicorn_log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        backend_started_by_script = True

        # Wait until port is listening or the process exits
        for _ in range(30):
            if uvicorn_proc.poll() is not None:
                print(f"后端启动失败，请查看日志: {UVICORN_LOG_FILE}")
                return 1
            if _is_port_open("127.0.0.1", BACKEND_PORT):
                break
            time.sleep(0.2)
        else:
            print(f"后端启动超时（端口未监听），请查看日志: {UVICORN_LOG_FILE}")
            return 1

    ngrok_url = _get_ngrok_https_url()
    if not ngrok_url:
        print(f"启动 ngrok: http {BACKEND_PORT}")
        with NGROK_LOG_FILE.open("w", encoding="utf-8") as ngrok_log:
            ngrok_proc = subprocess.Popen(
                ["ngrok", "http", str(BACKEND_PORT)],
                stdout=ngrok_log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        ngrok_started_by_script = True

        for _ in range(20):
            time.sleep(1)
            ngrok_url = _get_ngrok_https_url()
            if ngrok_url:
                break
        if not ngrok_url:
            log_fallback_url = _extract_ngrok_url_from_log(NGROK_LOG_FILE)
            if log_fallback_url:
                ngrok_url = log_fallback_url
                print(f"检测到已有在线 ngrok 域名，自动复用: {ngrok_url}")
    else:
        print(f"复用已有 ngrok 隧道: {ngrok_url}")

    if not ngrok_url:
        print(f"错误: 无法获取 ngrok HTTPS 地址，请查看日志: {NGROK_LOG_FILE}")
        return 1

    print("\n启动完成：")
    print(f"- Local API:   http://127.0.0.1:{BACKEND_PORT}")
    print(f"- Public URL:  {ngrok_url}")
    print(f"- Uvicorn log: {UVICORN_LOG_FILE}")
    if grpc_started_by_script:
        print(f"- gRPC log:   {GRPC_LOG_FILE}")
    if ngrok_started_by_script:
        print(f"- Ngrok log:   {NGROK_LOG_FILE}")
    print("\n按 Ctrl+C 退出（会自动关闭本脚本启动的进程）。")

    try:
        if uvicorn_proc is not None:
            return uvicorn_proc.wait()
        while not _stop_event.is_set():
            _stop_event.wait(3600)
        return 0
    finally:
        _cleanup()


if __name__ == "__main__":
    sys.exit(main())
