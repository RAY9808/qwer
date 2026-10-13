#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
realestate remote MCP server (Streamable HTTP)

브이월드(api.vworld.kr) 와 공공데이터포털(apis.data.go.kr) 오픈API 를 감싸
주소 하나로 공시지가·주택가격·토지이용계획·토지특성·실거래가·건축물대장을 조회하는
원격 MCP 서버. 구조와 접근 제어는 상위 폴더의 korean-law / dart 서버와 같다.

환경변수:
  VWORLD_API_KEY    (필수) 브이월드 오픈API 인증키
  VWORLD_DOMAIN     (권장) 인증키 발급 시 등록한 서비스 URL(도메인). domain 파라미터와
                    Referer 로 함께 보낸다. 예: realestate-mcp.cloudtype.app
  DATA_GO_KR_KEY    (선택) 공공데이터포털 일반 인증키(Decoding 권장). 실거래가·건축물대장용.
  MCP_AUTH_TOKEN    (권장) 접근 토큰. 설정 시 아래 두 가지 방법으로만 접근 허용
                      1) URL 경로:  https://<host>/s/<TOKEN>/mcp
                      2) 헤더:      Authorization: Bearer <TOKEN>
  PORT              (선택) 리슨 포트, 기본 8000
  VWORLD_API_HOST   (선택) 기본값 https://api.vworld.kr
  DATA_GO_KR_HOST   (선택) 기본값 https://apis.data.go.kr
  RE_MAX_CHARS      (선택) 응답 본문 최대 길이, 기본 60000
  MCP_JSON_RESPONSE (선택) 1 이면 SSE 대신 순수 JSON 응답 사용
  MCP_ALLOWED_HOSTS (선택) DNS 리바인딩 보호용 허용 호스트 목록(쉼표 구분)

주의: 브이월드는 해외 IP 대역을 차단하는 것으로 알려져 있어 국내 IP 호스팅을 권장한다.

필요 패키지: mcp>=2.0, uvicorn
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Annotated, Literal, Optional

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

VWORLD_KEY = os.environ.get("VWORLD_API_KEY", "").strip()
VWORLD_DOMAIN = os.environ.get("VWORLD_DOMAIN", "").strip()
DATA_KEY = os.environ.get("DATA_GO_KR_KEY", "").strip()
VWORLD_HOST = os.environ.get("VWORLD_API_HOST", "https://api.vworld.kr").rstrip("/")
DATA_HOST = os.environ.get("DATA_GO_KR_HOST", "https://apis.data.go.kr").rstrip("/")
MAX_CHARS = int(os.environ.get("RE_MAX_CHARS", "60000"))
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
JSON_RESPONSE = os.environ.get("MCP_JSON_RESPONSE", "").strip() in ("1", "true", "yes")
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

if "%" in DATA_KEY:  # Encoding 키를 넣은 경우 Decoding 키로 되돌린다(이중 인코딩 방지)
    DATA_KEY = urllib.parse.unquote(DATA_KEY)

SECRET_PARAMS = ("key", "serviceKey")

# 국토교통부 실거래가 (공공데이터포털, 1613000)
RTMS = {
    "apt_trade": ("RTMSDataSvcAptTrade", "아파트 매매"),
    "apt_rent": ("RTMSDataSvcAptRent", "아파트 전월세"),
    "rh_trade": ("RTMSDataSvcRHTrade", "연립다세대 매매"),
    "rh_rent": ("RTMSDataSvcRHRent", "연립다세대 전월세"),
    "sh_trade": ("RTMSDataSvcSHTrade", "단독/다가구 매매"),
    "sh_rent": ("RTMSDataSvcSHRent", "단독/다가구 전월세"),
    "offi_trade": ("RTMSDataSvcOffiTrade", "오피스텔 매매"),
    "offi_rent": ("RTMSDataSvcOffiRent", "오피스텔 전월세"),
    "land_trade": ("RTMSDataSvcLandTrade", "토지 매매"),
    "nrg_trade": ("RTMSDataSvcNrgTrade", "상업업무용 매매"),
    "silv_trade": ("RTMSDataSvcSilvTrade", "분양/입주권 전매"),
    "indu_trade": ("RTMSDataSvcInduTrade", "공장/창고 등 매매"),
}
# 활용신청하지 않은 서비스면 대체 서비스로 한 번 재시도한다(아파트 매매 기본 → 상세)
RTMS_FALLBACK = {"RTMSDataSvcAptTrade": ("RTMSDataSvcAptTradeDev", "아파트 매매(상세)")}

RtmsKind = Literal[
    "apt_trade", "apt_rent", "rh_trade", "rh_rent", "sh_trade", "sh_rent",
    "offi_trade", "offi_rent", "land_trade", "nrg_trade", "silv_trade", "indu_trade",
]

# 건축HUB 건축물대장 (공공데이터포털, 1613000/BldRgstHubService)
BLD_OPS = {
    "title": ("getBrTitleInfo", "표제부"),
    "recap": ("getBrRecapTitleInfo", "총괄표제부"),
    "basis": ("getBrBasisOulnInfo", "기본개요"),
    "floor": ("getBrFlrOulnInfo", "층별개요"),
    "expos": ("getBrExposInfo", "전유부"),
    "expos_area": ("getBrExposPubuseAreaInfo", "전유공용면적"),
    "jijigu": ("getBrJijiguInfo", "지역지구구역"),
    "house_price": ("getBrHsprcInfo", "주택가격"),
}
BldOp = Literal["title", "recap", "basis", "floor", "expos", "expos_area", "jijigu", "house_price"]

PNU_RE = re.compile(r"^\d{19}$")


# ---------------------------------------------------------------- HTTP


def _mask(url: str) -> str:
    for p in SECRET_PARAMS:
        url = re.sub(r"([?&]%s=)[^&]*" % p, r"\1***", url)
    return url


def _fetch(base: str, params: dict, referer: str = "") -> bytes:
    query = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None and v != ""}, encoding="utf-8"
    )
    url = "%s?%s" % (base, query)
    headers = {"User-Agent": "realestate-mcp/1.0 (+python-urllib)", "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer if "://" in referer else "https://" + referer
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:500].decode("utf-8", errors="replace")
        raise RuntimeError("HTTP %s 오류: %s\n요청 URL: %s\n응답 일부: %s" % (exc.code, exc.reason, _mask(url), body))
    except urllib.error.URLError as exc:
        raise RuntimeError("네트워크 오류: %s\n요청 URL: %s" % (exc.reason, _mask(url)))


def _dump(data) -> str:
    body = json.dumps(data, ensure_ascii=False, indent=2)
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "\n... (응답이 길어 %d자에서 잘렸습니다)" % MAX_CHARS
    return body


# ---------------------------------------------------------------- 브이월드


def _vworld(path: str, params: dict) -> dict:
    if not VWORLD_KEY:
        raise RuntimeError("환경변수 VWORLD_API_KEY 가 설정되지 않았습니다.")
    q = {"key": VWORLD_KEY, "domain": VWORLD_DOMAIN, "format": "json", **params}
    raw = _fetch("%s/%s" % (VWORLD_HOST, path.lstrip("/")), q, VWORLD_DOMAIN)
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        hint = ""
        if text[:200].lower().lstrip().startswith(("<!doctype", "<html")):
            hint = "\n(HTML 응답: 브이월드 방화벽이 해외 IP 를 차단했을 수 있습니다. 국내 IP 호스팅을 사용하세요.)"
        raise RuntimeError("브이월드가 JSON 이 아닌 응답을 반환했습니다(%s).%s\n응답 일부:\n%s" % (path, hint, text[:800]))
    resp = data.get("response") if isinstance(data, dict) else None
    if isinstance(resp, dict) and str(resp.get("status", "")).upper() == "ERROR":
        err = resp.get("error") or {}
        raise RuntimeError(
            "브이월드 오류 %s: %s (%s). 인증키·VWORLD_DOMAIN(등록한 서비스 URL)을 확인하세요."
            % (err.get("code", "?"), err.get("text", ""), path)
        )
    return data


def _search_items(query: str, category: str) -> list:
    data = _vworld(
        "req/search",
        {
            "service": "search", "request": "search", "version": "2.0", "crs": "EPSG:4326",
            "size": 10, "page": 1, "query": query, "type": "ADDRESS", "category": category,
        },
    )
    resp = data.get("response") or {}
    if str(resp.get("status", "")).upper() != "OK":
        return []
    return ((resp.get("result") or {}).get("items")) or []


def _pnu_at_point(x: str, y: str) -> Optional[dict]:
    data = _vworld(
        "req/data",
        {
            "service": "data", "request": "GetFeature", "data": "LP_PA_CBND_BUBUN",
            "geomFilter": "POINT(%s %s)" % (x, y), "crs": "EPSG:4326",
            "geometry": "false", "attribute": "true", "size": 1,
        },
    )
    resp = data.get("response") or {}
    feats = (((resp.get("result") or {}).get("featureCollection") or {}).get("features")) or []
    return (feats[0].get("properties") or {}) if feats else None


def _pnu_parts(pnu: str) -> dict:
    return {
        "pnu": pnu,
        "lawd_cd": pnu[:5],
        "sigunguCd": pnu[:5],
        "bjdongCd": pnu[5:10],
        "mountain": pnu[10] == "2",
        "platGbCd": "1" if pnu[10] == "2" else "0",
        "bun": pnu[11:15],
        "ji": pnu[15:19],
    }


def _resolve_blocking(target: str) -> dict:
    """주소(지번/도로명) 또는 PNU 를 받아 필지 정보를 돌려준다."""
    target = (target or "").strip()
    if PNU_RE.match(target):
        return {**_pnu_parts(target), "address": None, "candidates": []}
    if not target:
        raise RuntimeError("주소 또는 19자리 PNU 를 입력하세요.")

    items = _search_items(target, "PARCEL")
    source = "PARCEL"
    if not items:
        items = _search_items(target, "ROAD")
        source = "ROAD"
    if not items:
        raise RuntimeError("주소를 찾지 못했습니다: %s (지번 주소로 다시 시도해 보세요)" % target)

    first = items[0]
    addr = first.get("address") or {}
    point = first.get("point") or {}
    pnu = str(first.get("id") or "") if source == "PARCEL" else ""
    cadastral = None
    if not PNU_RE.match(pnu):
        if not (point.get("x") and point.get("y")):
            raise RuntimeError("주소 좌표를 얻지 못해 PNU 를 계산할 수 없습니다: %s" % target)
        cadastral = _pnu_at_point(point["x"], point["y"])
        pnu = str((cadastral or {}).get("pnu") or "")
        if not PNU_RE.match(pnu):
            raise RuntimeError("좌표에 해당하는 필지를 찾지 못했습니다: %s" % target)

    candidates = [
        {
            "id": it.get("id"),
            "parcel": (it.get("address") or {}).get("parcel"),
            "road": (it.get("address") or {}).get("road"),
        }
        for it in items[:5]
    ]
    return {
        **_pnu_parts(pnu),
        "address": {"parcel": addr.get("parcel") or (cadastral or {}).get("addr"), "road": addr.get("road")},
        "point": point,
        "matched_by": source,
        "candidates": candidates,
    }


def _ned(api: str, pnu: str, **params) -> dict:
    return _vworld("ned/data/" + api, {"pnu": pnu, "numOfRows": 100, "pageNo": 1, **params})


# ---------------------------------------------------------------- 공공데이터포털


def _xml_items(raw: bytes, label: str) -> dict:
    text = raw.decode("utf-8", errors="replace").strip()
    if text.startswith("{"):
        try:
            return {"raw": json.loads(text)}
        except ValueError:
            pass
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise RuntimeError("%s: XML 이 아닌 응답입니다. 인증키·활용신청 승인을 확인하세요.\n응답 일부:\n%s" % (label, text[:500]))
    auth = root.findtext(".//returnAuthMsg")
    if auth:
        raise RuntimeError(
            "%s: 공공데이터포털 오류 %s (%s). 해당 API 활용신청 승인 여부와 인증키를 확인하세요."
            % (label, auth, root.findtext(".//returnReasonCode") or "")
        )
    code = (root.findtext(".//header/resultCode") or "").strip()
    if code and code not in ("00", "000"):
        raise RuntimeError("%s: 오류 %s %s" % (label, code, root.findtext(".//header/resultMsg") or ""))
    items = [{c.tag: (c.text or "").strip() for c in item} for item in root.iter("item")]
    total = root.findtext(".//body/totalCount")
    return {"totalCount": int(total) if total and total.isdigit() else len(items), "items": items}


def _data_go(path: str, params: dict, label: str) -> dict:
    if not DATA_KEY:
        raise RuntimeError(
            "환경변수 DATA_GO_KR_KEY 가 설정되지 않았습니다. 공공데이터포털(data.go.kr)에서 "
            "해당 API 를 활용신청하고 일반 인증키(Decoding)를 설정하세요."
        )
    raw = _fetch("%s/%s" % (DATA_HOST, path), {"serviceKey": DATA_KEY, **params})
    return _xml_items(raw, label)


def _months(start: str, count: int) -> list[str]:
    y, m = int(start[:4]), int(start[4:6])
    out = []
    for _ in range(count):
        out.append("%04d%02d" % (y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def _rtms_blocking(kind: str, lawd_cd: str, deal_ymd: str, months: int, keyword: str, limit: int) -> dict:
    svc, label = RTMS[kind]
    words = [w for w in (keyword or "").split() if w]
    rows, totals = [], {}
    for ym in _months(deal_ymd, months):
        page = 1
        while True:
            params = {"LAWD_CD": lawd_cd, "DEAL_YMD": ym, "pageNo": page, "numOfRows": 1000}
            try:
                res = _data_go("1613000/%s/get%s" % (svc, svc), params, label)
            except RuntimeError:
                if svc not in RTMS_FALLBACK or totals:
                    raise
                svc, label = RTMS_FALLBACK[svc]
                res = _data_go("1613000/%s/get%s" % (svc, svc), params, label)
            totals[ym] = res.get("totalCount", 0)
            for it in res.get("items", []):
                blob = " ".join(it.values())
                if all(w in blob for w in words):
                    rows.append(it)
            if page * 1000 >= res.get("totalCount", 0) or not res.get("items") or page >= 10:
                break
            page += 1
    return {
        "kind": "%s (%s)" % (kind, label),
        "lawd_cd": lawd_cd,
        "months": totals,
        "keyword": keyword or None,
        "matched": len(rows),
        "returned": min(len(rows), limit),
        "items": rows[:limit],
        "note": "금액 단위는 만원. 실거래가는 시군구(LAWD_CD)·계약월 단위로 조회되므로 keyword 로 단지명·법정동·지번을 거른다.",
    }


# ---------------------------------------------------------------- MCP

mcp = MCPServer(
    name="realestate",
    version="1.0.0",
    instructions=(
        "대한민국 부동산 공적 정보를 조회한다. 모든 필지 도구는 주소(지번·도로명) 또는 19자리 PNU 를 받는다. "
        "한 번에 개요를 보려면 property_profile, 개별 항목은 land_price / housing_price / land_use / "
        "land_characteristics / building_register, 시세는 real_transactions 를 쓴다. "
        "토지이용계획확인서 '발급본'과 개발계획 고시문 원문은 API 로 제공되지 않으므로 토지이음(eum.go.kr)을 안내한다."
    ),
)

TargetArg = Annotated[str, Field(description="주소(지번 또는 도로명) 또는 19자리 PNU. 예: 서울특별시 종로구 세종로 1-68")]


async def _resolve(target: str) -> dict:
    return await anyio.to_thread.run_sync(_resolve_blocking, target)


async def _run(fn, *args):
    return await anyio.to_thread.run_sync(fn, *args)


@mcp.tool(description="주소를 필지고유번호(PNU, 19자리)와 법정동코드·지번으로 변환한다. 후보 주소도 함께 보여준다.")
async def find_parcel(target: TargetArg) -> str:
    return _dump(await _resolve(target))


@mcp.tool(description="개별공시지가(원/㎡)를 조회한다. year 를 비우면 제공되는 모든 연도를 돌려준다.")
async def land_price(
    target: TargetArg,
    year: Annotated[Optional[str], Field(description="기준연도 4자리")] = None,
) -> str:
    p = await _resolve(target)
    data = await _run(lambda: _ned("getIndvdLandPriceAttr", p["pnu"], stdrYear=year))
    return _dump({"parcel": p, "result": data})


@mcp.tool(
    description=(
        "주택 공시가격을 조회한다. kind=individual 이면 개별주택가격(단독·다가구), "
        "kind=apartment 면 공동주택가격(아파트·연립·다세대, dong/ho 로 호실 지정)."
    )
)
async def housing_price(
    target: TargetArg,
    kind: Annotated[Literal["individual", "apartment"], Field(description="individual=개별주택, apartment=공동주택")] = "individual",
    year: Annotated[Optional[str], Field(description="기준연도 4자리")] = None,
    dong: Annotated[Optional[str], Field(description="공동주택 동 이름. 예: 101")] = None,
    ho: Annotated[Optional[str], Field(description="공동주택 호 이름. 예: 1203")] = None,
) -> str:
    p = await _resolve(target)
    if kind == "individual":
        data = await _run(lambda: _ned("getIndvdHousingPriceAttr", p["pnu"], stdrYear=year))
    else:
        data = await _run(lambda: _ned("getApartHousingPriceAttr", p["pnu"], stdrYear=year, dongNm=dong, hoNm=ho))
    return _dump({"parcel": p, "result": data})


@mcp.tool(
    description=(
        "토지이용계획(용도지역·지구·구역, 저촉 여부)을 조회한다. 토지이용계획확인서 내용에 해당한다. "
        "법적 효력 있는 발급본은 토지이음(eum.go.kr)·정부24 에서 발급해야 한다."
    )
)
async def land_use(target: TargetArg) -> str:
    p = await _resolve(target)
    data = await _run(lambda: _ned("getLandUseAttr", p["pnu"]))
    return _dump({"parcel": p, "result": data, "certificate": "발급본: https://www.eum.go.kr (토지이용계획 열람·확인서 발급)"})


@mcp.tool(description="토지특성(지목, 면적, 용도지역, 이용상황, 지형높이·형상, 도로접면)을 조회한다.")
async def land_characteristics(
    target: TargetArg,
    year: Annotated[Optional[str], Field(description="기준연도 4자리")] = None,
) -> str:
    p = await _resolve(target)
    data = await _run(lambda: _ned("getLandCharacteristics", p["pnu"], stdrYear=year))
    return _dump({"parcel": p, "result": data})


@mcp.tool(
    description=(
        "주소 하나로 필지 개요를 한 번에 조회한다: PNU, 개별공시지가(전 연도), 토지특성, 토지이용계획, "
        "개별주택가격. 항목별로 실패해도 나머지는 돌려준다. 실거래가는 real_transactions 로 따로 조회."
    )
)
async def property_profile(target: TargetArg) -> str:
    p = await _resolve(target)
    out: dict = {"parcel": p}

    def section(name, api):
        try:
            out[name] = _ned(api, p["pnu"])
        except Exception as exc:  # 항목별 실패 허용
            out[name] = {"error": str(exc)}

    for name, api in (
        ("land_price", "getIndvdLandPriceAttr"),
        ("land_characteristics", "getLandCharacteristics"),
        ("land_use", "getLandUseAttr"),
        ("individual_housing_price", "getIndvdHousingPriceAttr"),
    ):
        await _run(section, name, api)
    out["next"] = {
        "real_transactions": "lawd_cd=%s 로 real_transactions 호출" % p["lawd_cd"],
        "building_register": "building_register(target=%s)" % p["pnu"],
    }
    return _dump(out)


@mcp.tool(
    description=(
        "국토교통부 실거래가를 조회한다(공공데이터포털). 시군구 단위·계약월 단위로 받아 keyword 로 거른다. kind: "
        + ", ".join("%s(%s)" % (k, v[1]) for k, v in RTMS.items())
    )
)
async def real_transactions(
    kind: Annotated[RtmsKind, Field(description="거래 종류")],
    deal_ymd: Annotated[str, Field(description="계약년월 YYYYMM. months>1 이면 이 달부터 과거로")],
    lawd_cd: Annotated[Optional[str], Field(description="법정동코드 앞 5자리(시군구). 비우면 target 주소에서 계산")] = None,
    target: Annotated[Optional[str], Field(description="lawd_cd 대신 쓸 주소 또는 PNU")] = None,
    months: Annotated[int, Field(description="조회 개월 수(기본 1, 최대 24)")] = 1,
    keyword: Annotated[Optional[str], Field(description="결과 필터(공백 구분 AND). 예: 단지명, 법정동명, 지번")] = None,
    limit: Annotated[int, Field(description="최대 반환 건수(기본 100, 최대 500)")] = 100,
) -> str:
    if not re.fullmatch(r"\d{6}", deal_ymd or ""):
        raise RuntimeError("deal_ymd 는 YYYYMM 6자리여야 합니다.")
    if not lawd_cd:
        if not target:
            raise RuntimeError("lawd_cd 또는 target(주소) 중 하나가 필요합니다.")
        lawd_cd = (await _resolve(target))["lawd_cd"]
    res = await _run(
        _rtms_blocking, kind, lawd_cd[:5], deal_ymd,
        max(1, min(int(months or 1), 24)), keyword or "", max(1, min(int(limit or 100), 500)),
    )
    return _dump(res)


@mcp.tool(
    description=(
        "건축물대장(건축HUB)을 조회한다(공공데이터포털). op: "
        + ", ".join("%s(%s)" % (k, v[1]) for k, v in BLD_OPS.items())
    )
)
async def building_register(
    target: TargetArg,
    op: Annotated[BldOp, Field(description="조회 항목(기본 title=표제부)")] = "title",
    limit: Annotated[int, Field(description="최대 건수(기본 100)")] = 100,
) -> str:
    p = await _resolve(target)
    api, label = BLD_OPS[op]
    res = await _run(
        _data_go,
        "1613000/BldRgstHubService/" + api,
        {
            "sigunguCd": p["sigunguCd"], "bjdongCd": p["bjdongCd"], "platGbCd": p["platGbCd"],
            "bun": p["bun"], "ji": p["ji"], "numOfRows": max(1, min(int(limit or 100), 1000)), "pageNo": 1,
        },
        "건축물대장 " + label,
    )
    return _dump({"parcel": p, "op": "%s (%s)" % (op, label), "result": res})


@mcp.tool(
    description=(
        "전용 도구가 없는 브이월드 API 를 직접 호출한다. path 예: 'ned/data/getLandUseAttr', "
        "'req/data', 'req/search'. 인증키·domain·format=json 은 자동으로 붙는다."
    )
)
async def vworld_api(
    path: Annotated[str, Field(description="api.vworld.kr 아래 경로. 예: ned/data/ladfrlList")],
    params: Annotated[Optional[dict[str, str]], Field(description="요청 파라미터")] = None,
) -> str:
    if not re.fullmatch(r"(ned/data|ned/wfs|ned/wms|req)/[A-Za-z0-9]+", path or ""):
        raise RuntimeError("path 는 'ned/data/<API명>' 또는 'req/<서비스>' 형식이어야 합니다.")
    params = {k: v for k, v in (params or {}).items() if k not in ("key", "domain")}
    return _dump(await _run(_vworld, path, params))


@mcp.tool(description="서버 설정과 외부 API 연결 상태를 점검한다(키 설정 여부, 브이월드 응답 여부).")
async def health_check() -> str:
    out = {
        "vworld_key": bool(VWORLD_KEY),
        "vworld_domain": VWORLD_DOMAIN or None,
        "data_go_kr_key": bool(DATA_KEY),
        "today": datetime.date.today().isoformat(),
    }
    try:
        await _run(_search_items, "서울특별시 종로구 세종로 1-68", "PARCEL")
        out["vworld"] = "ok"
    except Exception as exc:
        out["vworld"] = "error: %s" % exc
    return _dump(out)


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
            await _plain(send, 200, "realestate MCP server. endpoint: /mcp")
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
    if not VWORLD_KEY:
        print("경고: VWORLD_API_KEY 가 비어 있습니다. 필지 관련 도구가 모두 실패합니다.", flush=True)
    if not DATA_KEY:
        print("안내: DATA_GO_KR_KEY 가 비어 있습니다. 실거래가·건축물대장 도구가 비활성입니다.", flush=True)
    if not AUTH_TOKEN:
        print("경고: MCP_AUTH_TOKEN 이 비어 있습니다. 서버가 인증 없이 공개됩니다.", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
