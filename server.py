#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
korean-law remote MCP server (Streamable HTTP)

국가법령정보 공동활용 OPEN API(law.go.kr/DRF)를 감싸는 원격 MCP 서버.
claude.ai 의 "커스텀 커넥터"로 등록할 수 있도록 Streamable HTTP 로 노출한다.

기존 stdio 서버(korean_law_mcp.py)와 도구 이름·인자·동작이 동일하다.

환경변수:
  LAW_API_OC        (필수) law.go.kr 오픈API OC 값(보통 이메일 ID)
  MCP_AUTH_TOKEN    (권장) 접근 토큰. 설정 시 아래 두 가지 방법으로만 접근 허용
                      1) URL 경로:  https://<host>/s/<TOKEN>/mcp
                      2) 헤더:      Authorization: Bearer <TOKEN>
                    비워두면 인증 없이 누구나 접근 가능(비권장).
  PORT              (선택) 리슨 포트, 기본 8000
  LAW_API_HOST      (선택) 기본값 https://www.law.go.kr
  LAW_MAX_CHARS     (선택) 응답 본문 최대 길이, 기본 60000
  MCP_JSON_RESPONSE (선택) 1 이면 SSE 대신 순수 JSON 응답 사용
  MCP_ALLOWED_HOSTS (선택) DNS 리바인딩 보호용 허용 호스트 목록(쉼표 구분).
                    예: korean-law-mcp.onrender.com
                    비워두면 보호를 끄고 TokenGate 만으로 접근을 통제한다.

필요 패키지: mcp>=2.0, uvicorn
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated, Literal, Optional

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

OC = os.environ.get("LAW_API_OC", "").strip()
HOST = os.environ.get("LAW_API_HOST", "https://www.law.go.kr").rstrip("/")
MAX_CHARS = int(os.environ.get("LAW_MAX_CHARS", "60000"))
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
JSON_RESPONSE = os.environ.get("MCP_JSON_RESPONSE", "").strip() in ("1", "true", "yes")
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

TARGETS = {
    "law": "법령",
    "eflaw": "시행일 법령",
    "lsHistory": "법령 연혁",
    "prec": "판례",
    "detc": "헌재결정례",
    "expc": "법령해석례",
    "decc": "행정심판례",
    "admrul": "행정규칙",
    "ordin": "자치법규",
    "trty": "조약",
    "licbyl": "별표·서식",
}

TargetCode = Literal[
    "law", "eflaw", "lsHistory", "prec", "detc",
    "expc", "decc", "admrul", "ordin", "trty", "licbyl",
]


# ---------------------------------------------------------------- HTTP


def _call_api_blocking(endpoint: str, params: dict):
    if not OC:
        raise RuntimeError(
            "환경변수 LAW_API_OC 가 설정되지 않았습니다. "
            "law.go.kr 오픈API에서 발급받은 OC 값을 설정하세요."
        )
    query = {"OC": OC, "type": "JSON"}
    for key, value in params.items():
        if value is None or value == "":
            continue
        query[key] = value
    url = "%s/DRF/%s?%s" % (HOST, endpoint, urllib.parse.urlencode(query, encoding="utf-8"))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "korean-law-mcp/2.0 (+python-urllib)",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP %s 오류: %s\n요청 URL: %s" % (exc.code, exc.reason, url))
    except urllib.error.URLError as exc:
        raise RuntimeError("네트워크 오류: %s\n요청 URL: %s" % (exc.reason, url))

    text = raw.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        snippet = text[:1500]
        raise RuntimeError(
            "JSON 이 아닌 응답을 받았습니다. OC 값 또는 파라미터를 확인하세요.\n"
            "요청 URL: %s\n응답 일부:\n%s" % (url, snippet)
        )
    return url, data


async def _call(endpoint: str, params: dict) -> str:
    """블로킹 HTTP 호출을 워커 스레드로 넘겨 이벤트 루프를 막지 않는다."""
    url, data = await anyio.to_thread.run_sync(_call_api_blocking, endpoint, params)
    body = json.dumps(data, ensure_ascii=False, indent=2)
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "\n... (응답이 길어 %d자에서 잘렸습니다)" % MAX_CHARS
    return "요청 URL: %s\n\n%s" % (url, body)


def _clamp_display(display: Optional[int]) -> int:
    return min(int(display or 20), 100)


# ---------------------------------------------------------------- MCP

mcp = MCPServer(
    name="korean-law",
    version="2.0.0",
    instructions=(
        "대한민국 법령·판례를 국가법령정보 OPEN API 에서 조회한다. "
        "먼저 search_law / search_precedent 로 목록을 얻고, 반환된 "
        "법령MST 또는 판례일련번호를 get_law / get_precedent 에 넘겨 본문을 읽는다."
    ),
)


@mcp.tool(
    description=(
        "대한민국 현행 법령을 검색한다(국가법령정보 OPEN API, target=law). "
        "법령명 또는 본문 키워드로 목록을 조회하고, 결과의 법령일련번호(법령MST)를 "
        "get_law 에 넘겨 본문을 읽는다."
    )
)
async def search_law(
    query: Annotated[str, Field(description="검색어. 예: 민법, 개인정보 보호법, 근로기준법")],
    search: Annotated[Optional[Literal[1, 2]], Field(description="1=법령명 검색(기본), 2=본문 검색")] = 1,
    display: Annotated[Optional[int], Field(description="결과 개수(최대 100, 기본 20)")] = 20,
    page: Annotated[Optional[int], Field(description="페이지 번호(기본 1)")] = 1,
    sort: Annotated[Optional[str], Field(description="정렬. lasc/ldes(법령명), dasc/ddes(공포일자) 등")] = None,
    ef_yd: Annotated[Optional[str], Field(description="시행일자 범위. 예: 20200101~20241231")] = None,
    anc_yd: Annotated[Optional[str], Field(description="공포일자 범위. 예: 20200101~20241231")] = None,
) -> str:
    return await _call(
        "lawSearch.do",
        {
            "target": "law",
            "query": query,
            "search": search or 1,
            "display": _clamp_display(display),
            "page": page or 1,
            "sort": sort,
            "efYd": ef_yd,
            "ancYd": anc_yd,
        },
    )


@mcp.tool(
    description=(
        "법령 본문을 조회한다(target=law). search_law 결과의 법령MST(권장) 또는 법령ID, "
        "혹은 정확한 법령명을 지정한다. jo 로 특정 조문만 받을 수 있다."
    )
)
async def get_law(
    mst: Annotated[Optional[str], Field(description="법령마스터번호(법령MST). search_law 결과에 포함")] = None,
    id: Annotated[Optional[str], Field(description="법령ID")] = None,
    name: Annotated[Optional[str], Field(description="법령명(정확히 일치해야 함)")] = None,
    jo: Annotated[Optional[str], Field(description="조문 지정. 6자리(조4+항2). 예: 제2조=000200, 제406조=040600")] = None,
    ef_yd: Annotated[Optional[str], Field(description="시행일자(YYYYMMDD). 연혁 조문 조회 시")] = None,
) -> str:
    if not mst and not id and not name:
        raise RuntimeError("mst, id, name 중 하나는 반드시 지정해야 합니다.")
    return await _call(
        "lawService.do",
        {"target": "law", "MST": mst, "ID": id, "LM": name, "JO": jo, "efYd": ef_yd},
    )


@mcp.tool(
    description=(
        "판례를 검색한다(target=prec). 사건명 또는 본문 키워드로 조회하고, "
        "판례일련번호를 get_precedent 에 넘긴다."
    )
)
async def search_precedent(
    query: Annotated[str, Field(description="검색어. 예: 사해행위취소, 손해배상")],
    search: Annotated[Optional[Literal[1, 2]], Field(description="1=사건명(기본), 2=본문")] = 1,
    display: Annotated[Optional[int], Field(description="결과 개수(최대 100, 기본 20)")] = 20,
    page: Annotated[Optional[int], Field(description="페이지 번호")] = 1,
    org: Annotated[Optional[str], Field(description="법원종류. 400201=대법원, 400202=하위법원")] = None,
    court: Annotated[Optional[str], Field(description="법원명. 예: 대법원, 서울고등법원")] = None,
    date_range: Annotated[Optional[str], Field(description="선고일자 범위. 예: 20200101~20241231")] = None,
    sort: Annotated[Optional[str], Field(description="정렬 코드")] = None,
) -> str:
    return await _call(
        "lawSearch.do",
        {
            "target": "prec",
            "query": query,
            "search": search or 1,
            "display": _clamp_display(display),
            "page": page or 1,
            "org": org,
            "curt": court,
            "prncYd": date_range,
            "sort": sort,
        },
    )


@mcp.tool(
    description="판례 본문 전문을 조회한다(target=prec). search_precedent 결과의 판례일련번호가 필요하다."
)
async def get_precedent(
    id: Annotated[str, Field(description="판례일련번호")],
) -> str:
    if not id:
        raise RuntimeError("판례일련번호(id)가 필요합니다. search_precedent 로 먼저 조회하세요.")
    return await _call("lawService.do", {"target": "prec", "ID": id})


@mcp.tool(
    description=(
        "법령/판례 외의 자료를 검색한다. target: "
        + ", ".join("%s(%s)" % (k, v) for k, v in TARGETS.items())
    )
)
async def search(
    target: Annotated[TargetCode, Field(description="조회 대상 코드")],
    query: Annotated[str, Field(description="검색어")],
    search: Annotated[Optional[Literal[1, 2]], Field(description="1=제목(기본), 2=본문")] = 1,
    display: Annotated[Optional[int], Field(description="결과 개수(최대 100)")] = 20,
    page: Annotated[Optional[int], Field(description="페이지 번호")] = 1,
) -> str:
    return await _call(
        "lawSearch.do",
        {
            "target": target,
            "query": query,
            "search": search or 1,
            "display": _clamp_display(display),
            "page": page or 1,
        },
    )


@mcp.tool(
    description="법령/판례 외 자료의 본문을 조회한다. search 결과의 일련번호(id) 또는 mst 를 넘긴다."
)
async def get(
    target: Annotated[TargetCode, Field(description="조회 대상 코드")],
    id: Annotated[Optional[str], Field(description="일련번호")] = None,
    mst: Annotated[Optional[str], Field(description="마스터번호")] = None,
    jo: Annotated[Optional[str], Field(description="조문 지정(6자리)")] = None,
) -> str:
    if not id and not mst:
        raise RuntimeError("id 또는 mst 중 하나는 반드시 지정해야 합니다.")
    return await _call("lawService.do", {"target": target, "ID": id, "MST": mst, "JO": jo})


# ---------------------------------------------------------------- 접근 제어


async def _plain(send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


class TokenGate:
    """
    MCP_AUTH_TOKEN 이 설정된 경우에만 동작하는 얇은 ASGI 게이트.

    허용 경로:
      /s/<TOKEN>/mcp            -> 내부적으로 /mcp 로 재작성
      /mcp  + Authorization: Bearer <TOKEN>

    /healthz 는 항상 공개(호스팅 헬스체크용).
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token
        self.prefix = "/s/" + token if token else ""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or "/"

        if path in ("/healthz", "/health"):
            await _plain(send, 200, "ok")
            return
        if path == "/":
            await _plain(send, 200, "korean-law MCP server. endpoint: /mcp")
            return

        if not self.token:
            await self.app(scope, receive, send)
            return

        if path == self.prefix or path.startswith(self.prefix + "/"):
            rest = path[len(self.prefix):] or "/"
            scope = dict(scope)
            scope["path"] = rest
            raw = scope.get("raw_path")
            if raw:
                scope["raw_path"] = raw[len(self.prefix.encode("utf-8")):] or b"/"
            await self.app(scope, receive, send)
            return

        header = _header(scope, b"authorization")
        if header.startswith("Bearer ") and secrets.compare_digest(header[7:].strip(), self.token):
            await self.app(scope, receive, send)
            return

        await _plain(send, 401, "unauthorized")


if ALLOWED_HOSTS:
    _security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=ALLOWED_HOSTS,
        allowed_origins=["https://claude.ai", "https://*.claude.ai"],
    )
else:
    # 배포 호스트명을 모르는 경우. 접근 통제는 TokenGate 가 담당한다.
    _security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

app = TokenGate(
    mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=JSON_RESPONSE,
        transport_security=_security,
    ),
    AUTH_TOKEN,
)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    if not OC:
        print("경고: LAW_API_OC 가 비어 있습니다. 모든 도구 호출이 실패합니다.", flush=True)
    if not AUTH_TOKEN:
        print("경고: MCP_AUTH_TOKEN 이 비어 있습니다. 서버가 인증 없이 공개됩니다.", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
