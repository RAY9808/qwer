#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
opendart remote MCP server (Streamable HTTP)

금융감독원 전자공시 OPEN API(opendart.fss.or.kr)를 감싸는 원격 MCP 서버.
claude.ai 의 "커스텀 커넥터"·ChatGPT 개발자 모드 커넥터로 등록할 수 있도록 Streamable HTTP 로 노출한다.
구조와 접근 제어 방식은 상위 폴더의 korean-law 서버와 같다.

환경변수:
  DART_API_KEY      (필수) opendart.fss.or.kr 에서 발급받은 인증키(40자)
  MCP_AUTH_TOKEN    (권장) 접근 토큰. 설정 시 아래 두 가지 방법으로만 접근 허용
                      1) URL 경로:  https://<host>/s/<TOKEN>/mcp
                      2) 헤더:      Authorization: Bearer <TOKEN>
                    비워두면 인증 없이 누구나 접근 가능(비권장).
  PORT              (선택) 리슨 포트, 기본 8000
  DART_API_HOST     (선택) 기본값 https://opendart.fss.or.kr
  DART_MAX_CHARS    (선택) 응답 본문 최대 길이, 기본 60000
  MCP_JSON_RESPONSE (선택) 1 이면 SSE 대신 순수 JSON 응답 사용
  MCP_ALLOWED_HOSTS (선택) DNS 리바인딩 보호용 허용 호스트 목록(쉼표 구분).
                    비워두면 보호를 끄고 TokenGate 만으로 접근을 통제한다.

필요 패키지: mcp>=2.0, uvicorn
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from typing import Annotated, Literal, Optional

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

API_KEY = os.environ.get("DART_API_KEY", "").strip()
HOST = os.environ.get("DART_API_HOST", "https://opendart.fss.or.kr").rstrip("/")
MAX_CHARS = int(os.environ.get("DART_MAX_CHARS", "60000"))
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
JSON_RESPONSE = os.environ.get("MCP_JSON_RESPONSE", "").strip() in ("1", "true", "yes")
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

STATUS_MESSAGES = {
    "010": "등록되지 않은 인증키입니다",
    "011": "사용할 수 없는 인증키입니다",
    "012": "접근할 수 없는 IP입니다",
    "014": "파일이 존재하지 않습니다",
    "020": "요청 제한(일일 한도)을 초과했습니다",
    "021": "조회 가능한 회사 개수(최대 100건)를 초과했습니다",
    "100": "필드의 부적절한 값입니다",
    "101": "부적절한 접근입니다",
    "800": "시스템 점검 중입니다",
    "900": "정의되지 않은 오류입니다",
    "901": "개인정보 보유기간 만료로 사용할 수 없는 키입니다",
}

# 정기보고서 주요정보 API (모두 corp_code, bsns_year, reprt_code 를 받는다)
REPORT_APIS = {
    "irdsSttus": "증자(감자) 현황",
    "alotMatter": "배당에 관한 사항",
    "tesstkAcqsDspsSttus": "자기주식 취득 및 처분 현황",
    "hyslrSttus": "최대주주 현황",
    "hyslrChgSttus": "최대주주 변동현황",
    "mrhlSttus": "소액주주 현황",
    "exctvSttus": "임원 현황",
    "empSttus": "직원 현황",
    "hmvAuditIndvdlBySttus": "이사·감사의 개인별 보수현황",
    "hmvAuditAllSttus": "이사·감사 전체의 보수현황",
    "indvdlByPay": "개인별 보수지급 금액(5억 이상 상위 5인)",
    "otrCprInvstmntSttus": "타법인 출자현황",
    "stockTotqySttus": "주식의 총수 현황",
    "accnutAdtorNmNdAdtOpinion": "회계감사인의 명칭 및 감사의견",
    "detScritsIsuAcmslt": "채무증권 발행실적",
    "cprndNrdmpBlce": "회사채 미상환 잔액",
}

ReportApi = Literal[
    "irdsSttus", "alotMatter", "tesstkAcqsDspsSttus", "hyslrSttus", "hyslrChgSttus",
    "mrhlSttus", "exctvSttus", "empSttus", "hmvAuditIndvdlBySttus", "hmvAuditAllSttus",
    "indvdlByPay", "otrCprInvstmntSttus", "stockTotqySttus", "accnutAdtorNmNdAdtOpinion",
    "detScritsIsuAcmslt", "cprndNrdmpBlce",
]

ReprtCode = Literal["11011", "11012", "11013", "11014"]
REPRT_DESC = "보고서 코드. 11011=사업보고서(기본), 11012=반기, 11013=1분기, 11014=3분기"


# ---------------------------------------------------------------- HTTP


def _require_key() -> None:
    if not API_KEY:
        raise RuntimeError(
            "환경변수 DART_API_KEY 가 설정되지 않았습니다. "
            "opendart.fss.or.kr 에서 발급받은 인증키를 설정하세요."
        )


def _url(path: str, params: dict) -> tuple[str, str]:
    """(실제 요청 URL, 인증키를 가린 표시용 URL)"""
    query = {k: v for k, v in params.items() if v is not None and v != ""}
    shown = urllib.parse.urlencode(query, encoding="utf-8")
    real = urllib.parse.urlencode({"crtfc_key": API_KEY, **query}, encoding="utf-8")
    return "%s/api/%s?%s" % (HOST, path, real), "%s/api/%s?%s" % (HOST, path, shown)


def _fetch(url: str, shown: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "opendart-mcp/1.0 (+python-urllib)", "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP %s 오류: %s\n요청 URL: %s" % (exc.code, exc.reason, shown))
    except urllib.error.URLError as exc:
        raise RuntimeError("네트워크 오류: %s\n요청 URL: %s" % (exc.reason, shown))


def _raise_status(status: str, message: str, shown: str) -> None:
    hint = STATUS_MESSAGES.get(status, "")
    raise RuntimeError(
        "DART 오류 %s: %s%s\n요청 URL: %s"
        % (status, message, " (%s)" % hint if hint and hint not in message else "", shown)
    )


def _check_binary_error(raw: bytes, shown: str) -> None:
    """zip 을 기대한 응답이 오류 JSON/XML 인 경우 메시지를 꺼내 예외로 바꾼다."""
    text = raw[:2000].decode("utf-8", errors="replace").strip()
    status = message = ""
    try:
        data = json.loads(text)
        status, message = str(data.get("status", "")), str(data.get("message", ""))
    except ValueError:
        m = re.search(r"<status>(.*?)</status>", text)
        status = m.group(1) if m else ""
        m = re.search(r"<message>(.*?)</message>", text)
        message = m.group(1) if m else text[:300]
    _raise_status(status or "?", message, shown)


def _json_blocking(path: str, params: dict) -> tuple[str, dict]:
    _require_key()
    url, shown = _url(path, params)
    text = _fetch(url, shown).decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError("JSON 이 아닌 응답을 받았습니다.\n요청 URL: %s\n응답 일부:\n%s" % (shown, text[:1500]))
    status = str(data.get("status", ""))
    if status and status not in ("000", "013"):
        _raise_status(status, str(data.get("message", "")), shown)
    return shown, data


def _dump(shown: str, data) -> str:
    body = json.dumps(data, ensure_ascii=False, indent=2)
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "\n... (응답이 길어 %d자에서 잘렸습니다)" % MAX_CHARS
    return "요청 URL: %s\n\n%s" % (shown, body)


async def _call(path: str, params: dict) -> str:
    """블로킹 HTTP 호출을 워커 스레드로 넘겨 이벤트 루프를 막지 않는다."""
    shown, data = await anyio.to_thread.run_sync(_json_blocking, path, params)
    return _dump(shown, data)


# ---------------------------------------------------------------- 고유번호 캐시

_corp_cache: Optional[list[dict]] = None
_corp_lock = threading.Lock()


def _corp_list_blocking() -> list[dict]:
    """전체 회사 고유번호 목록(corpCode.xml, zip)을 한 번만 내려받아 메모리에 캐시한다."""
    global _corp_cache
    with _corp_lock:
        if _corp_cache is not None:
            return _corp_cache
        _require_key()
        url, shown = _url("corpCode.xml", {})
        raw = _fetch(url, shown, timeout=120)
        if not zipfile.is_zipfile(io.BytesIO(raw)):
            _check_binary_error(raw, shown)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read(zf.namelist()[0])
        corps = []
        for el in ET.fromstring(xml).iter("list"):
            corps.append(
                {
                    "corp_code": (el.findtext("corp_code") or "").strip(),
                    "corp_name": (el.findtext("corp_name") or "").strip(),
                    "corp_eng_name": (el.findtext("corp_eng_name") or "").strip(),
                    "stock_code": (el.findtext("stock_code") or "").strip(),
                    "modify_date": (el.findtext("modify_date") or "").strip(),
                }
            )
        _corp_cache = corps
        return corps


def _norm(s: str) -> str:
    return re.sub(r"[\s()（）㈜]|주식회사", "", s).lower()


def _find_corp_blocking(name: str, listed_only: bool, limit: int) -> dict:
    corps = _corp_list_blocking()
    q = _norm(name)
    hits = []
    for c in corps:
        if listed_only and not c["stock_code"]:
            continue
        if q == c["stock_code"] or q == c["corp_code"]:
            rank = 0
        else:
            n, e = _norm(c["corp_name"]), _norm(c["corp_eng_name"])
            if q == n or q == e:
                rank = 1
            elif n.startswith(q) or e.startswith(q):
                rank = 2
            elif q in n or q in e:
                rank = 3
            else:
                continue
        hits.append((rank, c["stock_code"] == "", len(c["corp_name"]), c))
    hits.sort(key=lambda h: h[:3])
    return {
        "query": name,
        "total": len(hits),
        "results": [h[3] for h in hits[:limit]],
        "note": "stock_code 가 있으면 상장사. 다른 도구에는 corp_code(8자리)를 넘긴다.",
    }


# ---------------------------------------------------------------- 공시 원문

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n+")


def _xml_to_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?i)<\s*(br|/p|/tr|/title|/section-\d|/table)[^>]*>", "\n", text)
    text = re.sub(r"(?i)<\s*/t[dhe][^>]*>", " | ", text)
    text = _TAG.sub("", text)
    for a, b in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        text = text.replace(a, b)
    text = _WS.sub(" ", text)
    return _NL.sub("\n\n", "\n".join(line.strip() for line in text.splitlines())).strip()


def _document_blocking(rcept_no: str, offset: int, max_chars: int) -> str:
    _require_key()
    url, shown = _url("document.xml", {"rcept_no": rcept_no})
    raw = _fetch(url, shown, timeout=120)
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        _check_binary_error(raw, shown)
    parts = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = sorted(zf.namelist())
        for n in names:
            parts.append("===== %s =====\n%s" % (n, _xml_to_text(zf.read(n))))
    full = "\n\n".join(parts)
    chunk = full[offset: offset + max_chars]
    header = (
        "접수번호: %s\n뷰어: https://dart.fss.or.kr/dsaf001/main.do?rcpNo=%s\n"
        "파일: %s\n전체 길이: %d자, 이번 구간: %d~%d자"
        % (rcept_no, rcept_no, ", ".join(names), len(full), offset, offset + len(chunk))
    )
    if offset + len(chunk) < len(full):
        header += "\n(이어서 읽으려면 offset=%d 로 다시 호출)" % (offset + len(chunk))
    return header + "\n\n" + chunk


# ---------------------------------------------------------------- MCP

# 모든 도구는 조회 전용이다. ChatGPT 개발자 모드는 readOnlyHint 가 없는 도구를
# 쓰기 작업으로 보고 호출마다 확인을 요구하므로 명시해 둔다.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

mcp = MCPServer(
    name="opendart",
    version="1.0.0",
    instructions=(
        "금융감독원 전자공시(DART) OPEN API 로 기업 공시·재무정보를 조회한다. "
        "먼저 find_corp 로 회사명→corp_code(8자리 고유번호)를 얻은 뒤 다른 도구에 넘긴다. "
        "공시 본문은 search_disclosures 결과의 rcept_no(접수번호)를 get_document 에 넘겨 읽는다. "
        "전용 도구가 없는 API(주요사항보고서, 증권신고서 등)는 dart_api 로 직접 호출한다."
    ),
)


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "회사명(국문/영문 일부), 종목코드(6자리) 또는 고유번호로 DART 고유번호(corp_code)를 찾는다. "
        "상장사와 정확히 일치하는 이름이 먼저 나온다."
    )
)
async def find_corp(
    name: Annotated[str, Field(description="회사명 또는 종목코드. 예: 삼성전자, 카카오, 005930")],
    listed_only: Annotated[bool, Field(description="True면 상장사만")] = False,
    limit: Annotated[int, Field(description="최대 결과 수(기본 20, 최대 100)")] = 20,
) -> str:
    result = await anyio.to_thread.run_sync(
        _find_corp_blocking, name, listed_only, max(1, min(int(limit or 20), 100))
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(annotations=READ_ONLY, description="기업개황(정식명칭, 대표자, 법인구분, 업종, 주소, 설립일, 결산월 등)을 조회한다.")
async def company_info(
    corp_code: Annotated[str, Field(description="고유번호 8자리(find_corp 결과)")],
) -> str:
    return await _call("company.json", {"corp_code": corp_code})


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "공시 목록을 검색한다(list.json). corp_code 를 비우면 전체 회사 대상이며 이때 기간은 최대 3개월이다. "
        "결과의 rcept_no 를 get_document 에 넘기면 본문을 읽을 수 있다."
    )
)
async def search_disclosures(
    corp_code: Annotated[Optional[str], Field(description="고유번호 8자리")] = None,
    bgn_de: Annotated[Optional[str], Field(description="시작일 YYYYMMDD")] = None,
    end_de: Annotated[Optional[str], Field(description="종료일 YYYYMMDD(기본 오늘)")] = None,
    pblntf_ty: Annotated[
        Optional[Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]],
        Field(description="공시유형. A=정기, B=주요사항보고, C=발행, D=지분, E=기타, F=외부감사, G=펀드, H=자산유동화, I=거래소, J=공정위"),
    ] = None,
    pblntf_detail_ty: Annotated[Optional[str], Field(description="공시상세유형. 예: A001=사업보고서, A002=반기, A003=분기, B001=주요사항보고서")] = None,
    corp_cls: Annotated[Optional[Literal["Y", "K", "N", "E"]], Field(description="법인구분. Y=유가, K=코스닥, N=코넥스, E=기타")] = None,
    last_reprt_at: Annotated[Optional[Literal["Y", "N"]], Field(description="Y면 최종보고서만")] = None,
    sort: Annotated[Optional[Literal["date", "crp", "rpt"]], Field(description="정렬. date=접수일자(기본), crp=회사명, rpt=보고서명")] = None,
    sort_mth: Annotated[Optional[Literal["asc", "desc"]], Field(description="정렬방법(기본 desc)")] = None,
    page_no: Annotated[int, Field(description="페이지 번호")] = 1,
    page_count: Annotated[int, Field(description="페이지당 건수(최대 100, 기본 20)")] = 20,
) -> str:
    return await _call(
        "list.json",
        {
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "pblntf_ty": pblntf_ty,
            "pblntf_detail_ty": pblntf_detail_ty,
            "corp_cls": corp_cls,
            "last_reprt_at": last_reprt_at,
            "sort": sort,
            "sort_mth": sort_mth,
            "page_no": page_no or 1,
            "page_count": min(int(page_count or 20), 100),
        },
    )


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "재무제표를 조회한다. full=False 면 주요계정(매출액·영업이익·당기순이익·자산·부채·자본 등, "
        "corp_code 를 쉼표로 여러 개 넣으면 회사 간 비교), full=True 면 전체 재무제표(단일회사). "
        "2015년 이후 데이터만 제공된다."
    )
)
async def financials(
    corp_code: Annotated[str, Field(description="고유번호 8자리. full=False 일 때 쉼표로 여러 개 가능")],
    bsns_year: Annotated[str, Field(description="사업연도 4자리. 예: 2025")],
    reprt_code: Annotated[ReprtCode, Field(description=REPRT_DESC)] = "11011",
    full: Annotated[bool, Field(description="True면 전체 재무제표")] = False,
    fs_div: Annotated[Literal["CFS", "OFS"], Field(description="full=True 일 때 CFS=연결(기본), OFS=별도")] = "CFS",
) -> str:
    params = {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code}
    if full:
        return await _call("fnlttSinglAcntAll.json", {**params, "fs_div": fs_div})
    path = "fnlttMultiAcnt.json" if "," in corp_code else "fnlttSinglAcnt.json"
    return await _call(path, params)


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "정기보고서(사업·반기·분기보고서)의 주요정보를 조회한다. api: "
        + ", ".join("%s(%s)" % (k, v) for k, v in REPORT_APIS.items())
    )
)
async def report_info(
    api: Annotated[ReportApi, Field(description="조회할 항목 코드")],
    corp_code: Annotated[str, Field(description="고유번호 8자리")],
    bsns_year: Annotated[str, Field(description="사업연도 4자리")],
    reprt_code: Annotated[ReprtCode, Field(description=REPRT_DESC)] = "11011",
) -> str:
    return await _call(api + ".json", {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code})


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "지분공시를 조회한다. kind=major 면 대량보유 상황보고(5% 룰), "
        "kind=executive 면 임원·주요주주 소유보고."
    )
)
async def shareholding(
    corp_code: Annotated[str, Field(description="고유번호 8자리")],
    kind: Annotated[Literal["major", "executive"], Field(description="major=대량보유, executive=임원·주요주주")] = "major",
) -> str:
    path = "majorstock.json" if kind == "major" else "elestock.json"
    return await _call(path, {"corp_code": corp_code})


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "공시서류 원문을 텍스트로 읽는다(document.xml). rcept_no 는 search_disclosures 결과의 14자리 접수번호. "
        "길면 offset 으로 이어 읽는다."
    )
)
async def get_document(
    rcept_no: Annotated[str, Field(description="접수번호 14자리")],
    offset: Annotated[int, Field(description="읽기 시작 위치(문자 수, 기본 0)")] = 0,
    max_chars: Annotated[int, Field(description="이번에 읽을 최대 길이(기본 30000)")] = 30000,
) -> str:
    return await anyio.to_thread.run_sync(
        _document_blocking, rcept_no, max(0, int(offset or 0)), max(1000, min(int(max_chars or 30000), MAX_CHARS))
    )


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "전용 도구가 없는 OpenDART JSON API 를 직접 호출한다. path 는 '<API명>.json' 형식. "
        "예: 주요사항보고서 piicDecsn.json(유상증자결정), cvbdIsDecsn.json(전환사채발행결정), "
        "dfOcr.json(부도발생), bsnTrfDecsn.json(영업양도결정), mgDecsn.json(합병결정) 등은 "
        "corp_code, bgn_de, end_de 를 받는다. 인증키는 자동으로 붙는다. "
        "API 목록: https://opendart.fss.or.kr/guide/main.do"
    )
)
async def dart_api(
    path: Annotated[str, Field(description="API 파일명. 예: piicDecsn.json")],
    params: Annotated[Optional[dict[str, str]], Field(description="요청 파라미터. 예: {\"corp_code\": \"00126380\", \"bgn_de\": \"20250101\", \"end_de\": \"20251231\"}")] = None,
) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]+\.json", path or ""):
        raise RuntimeError("path 는 '<API명>.json' 형식이어야 합니다. 예: piicDecsn.json")
    params = {k: v for k, v in (params or {}).items() if k != "crtfc_key"}
    return await _call(path, params)


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
            await _plain(send, 200, "opendart MCP server. endpoint: /mcp")
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
        allowed_origins=[
            "https://claude.ai", "https://*.claude.ai",
            "https://chatgpt.com", "https://*.chatgpt.com", "https://chat.openai.com",
        ],
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
    if not API_KEY:
        print("경고: DART_API_KEY 가 비어 있습니다. 모든 도구 호출이 실패합니다.", flush=True)
    if not AUTH_TOKEN:
        print("경고: MCP_AUTH_TOKEN 이 비어 있습니다. 서버가 인증 없이 공개됩니다.", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
