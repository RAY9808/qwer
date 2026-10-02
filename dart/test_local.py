#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
로컬 스모크 테스트.

server.py 를 별도 프로세스로 띄우고 실제 HTTP 요청으로
  1) /healthz 공개 여부
  2) 토큰 없는 접근이 401 인지
  3) 토큰 경로로 initialize / tools/list / tools/call 이 동작하는지
를 확인한다.

실행:
  DART_API_KEY=<인증키> python test_local.py   # 실제 OpenDART 호출
  python test_local.py --mock                  # 가짜 DART 서버로 호출(인증키·인터넷 불필요)
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("TEST_PORT", "8766"))
MOCK_PORT = PORT + 1
TOKEN = "test-token-1234567890"
MOCK_KEY = "mock-key-0000000000000000000000000000000"
BASE = "http://127.0.0.1:%d" % PORT
MCP_URL = "%s/s/%s/mcp" % (BASE, TOKEN)

ACCEPT = "application/json, text/event-stream"


# ---------------------------------------------------------------- 가짜 DART


def _zip(name: str, data: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, data.encode("utf-8"))
    return buf.getvalue()


CORP_XML = """<?xml version="1.0" encoding="UTF-8"?><result>
<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name><corp_eng_name>SAMSUNG ELECTRONICS CO,.LTD</corp_eng_name><stock_code>005930</stock_code><modify_date>20250101</modify_date></list>
<list><corp_code>00999999</corp_code><corp_name>삼성전자서비스</corp_name><corp_eng_name></corp_eng_name><stock_code> </stock_code><modify_date>20250101</modify_date></list>
<list><corp_code>00258801</corp_code><corp_name>카카오</corp_name><corp_eng_name>Kakao Corp.</corp_eng_name><stock_code>035720</stock_code><modify_date>20250101</modify_date></list>
</result>"""

DOC_XML = "<DOCUMENT><TITLE>사업보고서</TITLE><P>회사의 개요&nbsp;입니다</P><TABLE><TR><TD>매출</TD><TD>100</TD></TR></TABLE></DOCUMENT>"


class MockDart(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json;charset=UTF-8")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        name = url.path.rsplit("/", 1)[-1]
        if q.get("crtfc_key") != MOCK_KEY:
            if name.endswith(".xml"):
                self._send(b"<result><status>010</status><message>\xeb\x93\xb1\xeb\xa1\x9d\xeb\x90\x98\xec\xa7\x80 \xec\x95\x8a\xec\x9d\x80 \xed\x82\xa4\xec\x9e\x85\xeb\x8b\x88\xeb\x8b\xa4.</message></result>", "application/xml")
            else:
                self._json({"status": "010", "message": "등록되지 않은 키입니다."})
            return
        if name == "corpCode.xml":
            self._send(_zip("CORPCODE.xml", CORP_XML), "application/x-msdownload")
        elif name == "document.xml":
            self._send(_zip(q["rcept_no"] + ".xml", DOC_XML), "application/x-msdownload")
        elif name == "company.json":
            self._json({"status": "000", "message": "정상", "corp_code": q["corp_code"], "corp_name": "삼성전자(주)", "ceo_nm": "홍길동"})
        elif name == "fnlttSinglAcnt.json":
            self._json({"status": "000", "message": "정상", "list": [{"account_nm": "매출액", "thstrm_amount": "300,870,903,000,000", "bsns_year": q["bsns_year"]}]})
        elif name == "list.json":
            self._json({"status": "000", "message": "정상", "total_count": 1, "list": [{"rcept_no": "20250311001085", "report_nm": "사업보고서 (2024.12)"}]})
        else:
            self._json({"status": "013", "message": "조회된 데이타가 없습니다."})


# ---------------------------------------------------------------- MCP 호출


def _post(url: str, payload: dict, headers: dict | None = None):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": ACCEPT, **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
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


def _tool(name: str, arguments: dict, request_id: int) -> tuple[bool, str]:
    result = _rpc("tools/call", {"name": name, "arguments": arguments}, request_id)["result"]
    return bool(result.get("isError")), result["content"][0]["text"]


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
    mock = "--mock" in sys.argv
    env = dict(os.environ)
    if mock:
        httpd = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockDart)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        env["DART_API_KEY"] = MOCK_KEY
        env["DART_API_HOST"] = "http://127.0.0.1:%d" % MOCK_PORT
    elif not env.get("DART_API_KEY"):
        print(
            "환경변수 DART_API_KEY 가 필요합니다(또는 --mock).\n"
            "  예)  DART_API_KEY=<인증키> python test_local.py"
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
        print("[1/7] /healthz 200 OK")

        try:
            _post(BASE + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
            print("[2/7] 실패: 토큰 없이 접근이 허용됨")
            return 1
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, "기대 401, 실제 %s" % exc.code
            print("[2/7] 토큰 없는 접근 401 OK")

        res = _rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1.0"},
            },
        )
        info = res["result"]["serverInfo"]
        assert info["name"] == "opendart", info
        print("[3/7] initialize OK -> %s %s" % (info["name"], info.get("version", "")))

        res = _rpc("tools/list", {}, request_id=2)
        names = sorted(t["name"] for t in res["result"]["tools"])
        expected = sorted(
            ["find_corp", "company_info", "search_disclosures", "financials",
             "report_info", "shareholding", "get_document", "dart_api"]
        )
        assert names == expected, "도구 목록 불일치: %s" % names
        print("[4/7] tools/list OK -> %s" % ", ".join(names))

        err, text = _tool("find_corp", {"name": "삼성전자", "listed_only": True}, 3)
        assert not err, text
        first = json.loads(text)["results"][0]
        assert first["corp_code"] == "00126380", first
        print("[5/7] find_corp(삼성전자) OK -> %s %s" % (first["corp_code"], first["corp_name"]))

        err, text = _tool("financials", {"corp_code": "00126380", "bsns_year": "2024"}, 4)
        assert not err, text
        assert "매출액" in text and "crtfc_key" not in text, text[:800]
        print("[6/7] financials(삼성전자, 2024) OK -> '매출액' 확인, 인증키 비노출 확인")

        err, text = _tool("search_disclosures", {"corp_code": "00126380", "bgn_de": "20250101", "end_de": "20251231", "pblntf_detail_ty": "A001"}, 5)
        assert not err, text
        start = text.index("{")
        rcept_no = json.loads(text[start:])["list"][0]["rcept_no"]
        err, text = _tool("get_document", {"rcept_no": rcept_no, "max_chars": 2000}, 6)
        assert not err, text
        print("[7/7] search_disclosures → get_document(%s) OK -> %d자" % (rcept_no, len(text)))

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
