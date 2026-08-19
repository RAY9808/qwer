#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
로컬 스모크 테스트.

server.py 를 별도 프로세스로 띄우고 실제 HTTP 요청으로
  1) /healthz 공개 여부
  2) 토큰 없는 접근이 401 인지
  3) 토큰 경로로 initialize / tools/list / tools/call 이 동작하는지
를 확인한다.

실행:  python test_local.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("TEST_PORT", "8765"))
TOKEN = "test-token-1234567890"
BASE = "http://127.0.0.1:%d" % PORT
MCP_URL = "%s/s/%s/mcp" % (BASE, TOKEN)

ACCEPT = "application/json, text/event-stream"


def _post(url: str, payload: dict, headers: dict | None = None):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": ACCEPT,
            **(headers or {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")


def _parse(content_type: str, text: str) -> dict:
    """JSON 응답과 SSE 응답을 모두 처리한다."""
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError("SSE 응답에 data 라인이 없습니다:\n%s" % text[:500])
    return json.loads(text)


def _rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    status, ctype, text = _post(MCP_URL, payload)
    assert status == 200, "HTTP %s\n%s" % (status, text[:500])
    return _parse(ctype, text)


def wait_ready(timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/healthz", timeout=3) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # 아직 안 떴다
            last = exc
        time.sleep(0.5)
    raise RuntimeError("서버가 뜨지 않았습니다: %s" % last)


def main() -> int:
    env = dict(os.environ)
    if not env.get("LAW_API_OC"):
        print(
            "환경변수 LAW_API_OC 가 필요합니다. law.go.kr 오픈API 신청 시 받은 OC 값을 지정하세요.\n"
            "  예)  LAW_API_OC=<본인-OC값> python test_local.py"
        )
        return 2
    env["MCP_AUTH_TOKEN"] = TOKEN
    env["PORT"] = str(PORT)
    env["PYTHONIOENCODING"] = "utf-8"

    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.Popen(
        [sys.executable, os.path.join(here, "server.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_ready()
        print("[1/5] /healthz 200 OK")

        # 토큰 없는 접근은 401
        try:
            _post(BASE + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
            print("[2/5] 실패: 토큰 없이 접근이 허용됨")
            return 1
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, "기대 401, 실제 %s" % exc.code
            print("[2/5] 토큰 없는 접근 401 OK")

        # initialize
        res = _rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1.0"},
            },
        )
        info = res["result"]["serverInfo"]
        assert info["name"] == "korean-law", info
        print("[3/5] initialize OK -> %s %s" % (info["name"], info.get("version", "")))

        # tools/list
        res = _rpc("tools/list", {}, request_id=2)
        names = sorted(t["name"] for t in res["result"]["tools"])
        expected = sorted(
            ["search_law", "get_law", "search_precedent", "get_precedent", "search", "get"]
        )
        assert names == expected, "도구 목록 불일치: %s" % names
        print("[4/5] tools/list OK -> %s" % ", ".join(names))

        # tools/call : 민법 제406조
        res = _rpc(
            "tools/call",
            {"name": "get_law", "arguments": {"name": "민법", "jo": "040600"}},
            request_id=3,
        )
        result = res["result"]
        assert not result.get("isError"), result
        text = result["content"][0]["text"]
        assert "채권자취소권" in text, text[:800]
        print("[5/5] tools/call get_law(민법 제406조) OK -> '채권자취소권' 확인")

        print("\n전체 통과. 배포 준비 완료.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
