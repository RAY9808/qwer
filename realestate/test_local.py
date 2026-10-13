#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
로컬 스모크 테스트.

server.py 를 별도 프로세스로 띄우고 실제 HTTP 요청으로 MCP 동작을 확인한다.

실행:
  python test_local.py --mock     # 가짜 브이월드·공공데이터포털로 검증(키·인터넷 불필요)
  VWORLD_API_KEY=<키> VWORLD_DOMAIN=<등록 도메인> [DATA_GO_KR_KEY=<키>] python test_local.py
                                  # 실제 API 로 검증(국내 IP 에서 실행)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("TEST_PORT", "8776"))
MOCK_PORT = PORT + 1
TOKEN = "test-token-1234567890"
MOCK_VW = "mock-vworld-key"
MOCK_DG = "mock+data/key=="  # 특수문자 포함 키가 그대로 전달되는지 확인
BASE = "http://127.0.0.1:%d" % PORT
MCP_URL = "%s/s/%s/mcp" % (BASE, TOKEN)
ACCEPT = "application/json, text/event-stream"

ADDR = "서울특별시 종로구 세종로 1-68"
PNU = "1111011900100010068"

# ---------------------------------------------------------------- 가짜 API

RTMS_XML = """<?xml version="1.0" encoding="UTF-8"?><response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items>
<item><aptNm>경희궁의아침</aptNm><umdNm>내수동</umdNm><jibun>72</jibun><dealAmount>150,000</dealAmount><dealYear>{y}</dealYear><dealMonth>{m}</dealMonth></item>
<item><aptNm>광화문스페이스본</aptNm><umdNm>사직동</umdNm><jibun>9</jibun><dealAmount>180,000</dealAmount><dealYear>{y}</dealYear><dealMonth>{m}</dealMonth></item>
</items><numOfRows>1000</numOfRows><pageNo>1</pageNo><totalCount>2</totalCount></body></response>"""

AUTH_ERR_XML = """<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>
<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""


class MockApi(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, ctype="application/json;charset=UTF-8"):
        if not isinstance(body, bytes):
            body = (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        path = url.path

        if path.startswith("/1613000/"):
            if q.get("serviceKey") != MOCK_DG:
                return self._send(AUTH_ERR_XML, "text/xml")
            if "BldRgstHubService" in path:
                assert q["sigunguCd"] == "11110" and q["bun"] == "0001" and q["platGbCd"] == "0", q
                return self._send(
                    "<response><header><resultCode>00</resultCode></header><body><items>"
                    "<item><bldNm>정부서울청사</bldNm><mainPurpsCdNm>업무시설</mainPurpsCdNm></item>"
                    "</items><totalCount>1</totalCount></body></response>", "text/xml")
            if "AptTrade/" in path and q["LAWD_CD"] == "11680":  # 기본 미신청 상황
                return self._send(AUTH_ERR_XML, "text/xml")
            ym = q["DEAL_YMD"]
            return self._send(RTMS_XML.format(y=ym[:4], m=int(ym[4:])), "text/xml")

        if q.get("key") != MOCK_VW:
            return self._send({"response": {"status": "ERROR", "error": {"code": "INCORRECT_KEY", "text": "인증키 정보가 올바르지 않습니다."}}})
        if path == "/req/search":
            if q["category"] == "PARCEL" and "세종로" in q["query"]:
                return self._send({"response": {"status": "OK", "result": {"items": [
                    {"id": PNU, "address": {"parcel": "세종로 1-68", "road": ""}, "point": {"x": "126.975", "y": "37.575"}}]}}})
            if q["category"] == "ROAD" and "세종대로" in q["query"]:
                return self._send({"response": {"status": "OK", "result": {"items": [
                    {"id": "11110101310001200009400000", "address": {"road": ADDR, "parcel": ""}, "point": {"x": "126.975", "y": "37.575"}}]}}})
            return self._send({"response": {"status": "NOT_FOUND"}})
        if path == "/req/data":
            return self._send({"response": {"status": "OK", "result": {"featureCollection": {"features": [
                {"properties": {"pnu": PNU, "addr": "서울특별시 종로구 세종로 1-68"}}]}}}})
        if path.startswith("/ned/data/"):
            api = path.rsplit("/", 1)[-1]
            assert q["pnu"] == PNU, q
            body = {
                "getIndvdLandPriceAttr": {"indvdLandPrices": {"field": [{"stdrYear": "2025", "pblntfPclnd": "45000000"}], "totalCount": "1"}},
                "getLandUseAttr": {"landUses": {"field": [{"prposAreaDstrcCodeNm": "일반상업지역", "cnflcAtNm": "포함"}]}},
                "getLandCharacteristics": {"landCharacteristicss": {"field": [{"lndcgrCodeNm": "대", "lndpclAr": "11234"}]}},
            }.get(api, {"response": {"status": "NOT_FOUND"}})
            return self._send(body)
        self._send({"response": {"status": "ERROR", "error": {"code": "INVALID_RANGE", "text": "unknown"}}})


# ---------------------------------------------------------------- MCP 호출


def _post(url, payload, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": ACCEPT, **(headers or {})}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")


def _parse(ctype, text):
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError("SSE 응답에 data 라인이 없습니다:\n%s" % text[:500])
    return json.loads(text)


_id = [0]


def _rpc(method, params=None):
    _id[0] += 1
    payload = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        payload["params"] = params
    status, ctype, text = _post(MCP_URL, payload)
    assert status == 200, text[:500]
    return _parse(ctype, text)


def _tool(name, arguments):
    result = _rpc("tools/call", {"name": name, "arguments": arguments})["result"]
    return bool(result.get("isError")), result["content"][0]["text"]


def wait_ready(timeout=40.0):
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/healthz", timeout=3) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last = exc
        time.sleep(0.5)
    raise RuntimeError("서버가 뜨지 않았습니다: %s" % last)


def main() -> int:
    mock = "--mock" in sys.argv
    env = dict(os.environ)
    if mock:
        httpd = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockApi)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        host = "http://127.0.0.1:%d" % MOCK_PORT
        env.update(VWORLD_API_KEY=MOCK_VW, DATA_GO_KR_KEY=MOCK_DG, VWORLD_API_HOST=host, DATA_GO_KR_HOST=host, VWORLD_DOMAIN="localhost")
    elif not env.get("VWORLD_API_KEY"):
        print("환경변수 VWORLD_API_KEY 가 필요합니다(또는 --mock).")
        return 2
    env.update(MCP_AUTH_TOKEN=TOKEN, PORT=str(PORT), PYTHONIOENCODING="utf-8")

    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.Popen([sys.executable, os.path.join(here, "server.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_ready()
        print("[1] /healthz 200 OK")
        try:
            _post(BASE + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
            print("[2] 실패: 토큰 없이 접근이 허용됨")
            return 1
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            print("[2] 토큰 없는 접근 401 OK")

        info = _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "smoke-test", "version": "1.0"}})["result"]["serverInfo"]
        assert info["name"] == "realestate", info
        names = sorted(t["name"] for t in _rpc("tools/list", {})["result"]["tools"])
        print("[3] initialize/tools_list OK -> %s" % ", ".join(names))

        err, text = _tool("find_parcel", {"target": ADDR})
        assert not err, text
        assert json.loads(text)["pnu"] == PNU, text
        print("[4] find_parcel(지번) OK -> PNU %s" % PNU)

        if mock:
            err, text = _tool("find_parcel", {"target": "서울특별시 종로구 세종대로 209"})
            assert not err and json.loads(text)["pnu"] == PNU, text
            print("[4b] find_parcel(도로명 → 좌표 → 연속지적도) OK")

        err, text = _tool("property_profile", {"target": ADDR})
        assert not err, text
        prof = json.loads(text)
        assert "error" not in prof["land_price"] and "error" not in prof["land_use"], text[:1500]
        assert "key=" not in text or "key=***" in text, "인증키 노출"
        print("[5] property_profile OK -> 공시지가·토지이용계획·토지특성")

        if mock or env.get("DATA_GO_KR_KEY"):
            err, text = _tool("real_transactions", {"kind": "apt_trade", "target": ADDR, "deal_ymd": "202501", "months": 2, "keyword": "내수동"})
            assert not err, text
            res = json.loads(text)
            print("[6] real_transactions OK -> %d개월, %d건 매칭" % (len(res["months"]), res["matched"]))
            if mock:
                err, text = _tool("real_transactions", {"kind": "apt_trade", "lawd_cd": "11680", "deal_ymd": "202501"})
                assert not err and "상세" in json.loads(text)["kind"], text[:500]
                print("[6b] apt_trade 기본 미신청 → 상세 자료로 자동 전환 OK")
            err, text = _tool("building_register", {"target": ADDR})
            assert not err, text
            print("[7] building_register OK")

        if mock:
            err, text = _tool("vworld_api", {"path": "../etc", "params": {}})
            assert err
            print("[8] vworld_api 경로 검증 OK")

        print("\n전체 통과.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
