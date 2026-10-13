# realestate 원격 MCP 서버

브이월드(api.vworld.kr)와 공공데이터포털(apis.data.go.kr) 오픈API를 감싸
**주소 하나로** 공시지가·주택가격·토지이용계획·토지특성·실거래가·건축물대장을 조회하는 원격 MCP 서버입니다.
구조와 접근 제어(`MCP_AUTH_TOKEN`)는 korean-law / dart 서버와 같습니다.

## 제공 도구

| 도구 | 내용 | 필요한 키 |
|---|---|---|
| `find_parcel` | 주소(지번·도로명) → PNU(19자리)·법정동코드·지번 | 브이월드 |
| `property_profile` | 한 번에: 공시지가(전 연도) + 토지특성 + 토지이용계획 + 개별주택가격 | 브이월드 |
| `land_price` | 개별공시지가(원/㎡) | 브이월드 |
| `housing_price` | 개별주택가격 / 공동주택가격(동·호) | 브이월드 |
| `land_use` | 토지이용계획(용도지역·지구·구역) | 브이월드 |
| `land_characteristics` | 지목·면적·이용상황·형상·도로접면 | 브이월드 |
| `real_transactions` | 실거래가 12종(아파트·연립·단독·오피스텔 매매/전월세, 토지, 상업업무용, 분양권, 공장창고) | 공공데이터포털 |
| `building_register` | 건축물대장(표제부·총괄표제부·전유부·층별·주택가격 등) | 공공데이터포털 |
| `vworld_api` | 그 밖의 브이월드 API 직접 호출 | 브이월드 |
| `health_check` | 키 설정·브이월드 연결 상태 점검 | |

토지이용계획확인서 **발급본**과 개발계획 **고시문 원문**은 오픈API가 없어 토지이음(eum.go.kr)을 안내합니다.

---

## 1. 호스팅 위치 — 국내 IP 필수

브이월드는 **해외 IP 대역을 방화벽으로 차단**하는 것으로 알려져 있습니다.
Render(싱가포르) 같은 해외 호스팅에서는 브이월드 호출이 실패할 수 있으므로
**Cloudtype(cloudtype.io, 국내 리전)** 처럼 한국 IP로 나가는 곳에 배포합니다.

## 2. 배포 — Cloudtype

1. cloudtype.io 가입(GitHub 로그인) → **새 프로젝트** → **GitHub 저장소** 에서 `qwer` 선택
2. 언어/프리셋: **Python** (버전 3.12)
   - 디렉토리(컨텍스트) 경로: `realestate`
   - Build: `pip install -r requirements.txt`
   - Start: `python server.py` (비워두면 `Procfile` 사용)
   - Port: `8000`
3. 환경변수

   | Key | Value |
   |---|---|
   | `VWORLD_API_KEY` | 브이월드 인증키 |
   | `VWORLD_DOMAIN` | 브이월드 인증키에 등록한 서비스 URL(도메인) |
   | `DATA_GO_KR_KEY` | (선택) 공공데이터포털 일반 인증키(Decoding) |
   | `MCP_AUTH_TOKEN` | 접근 토큰 |

4. 배포 후 `https://<앱주소>/healthz` → `ok`

> **VWORLD_DOMAIN:** 브이월드는 인증키에 등록된 서비스 URL과 `domain` 파라미터(및 Referer)가 맞아야 응답합니다.
> 브이월드 마이페이지 → 인증키 관리에서 서비스 URL을 Cloudtype 앱 주소로 바꾸고, 같은 값을 `VWORLD_DOMAIN` 에 넣으세요.

## 3. claude.ai 에 등록

Settings → Connectors → Add custom connector

```
https://<앱주소>/s/<MCP_AUTH_TOKEN>/mcp
```

등록 후 대화에서 `health_check` 를 먼저 호출해 `"vworld": "ok"` 인지 확인합니다.

## 4. 공공데이터포털 키(실거래가·건축물대장)

data.go.kr 로그인 후 아래 API 를 각각 **활용신청**(대부분 자동승인)하면, 계정의 일반 인증키 하나로 모두 호출됩니다.

- 국토교통부_아파트 매매 실거래가 상세 자료 / 아파트 전월세 실거래가 자료
- 국토교통부_연립다세대·단독/다가구·오피스텔 매매·전월세 실거래가 자료
- 국토교통부_토지 매매 / 상업업무용 부동산 매매 / 아파트 분양권전매 / 공장 및 창고 등 매매 신고 자료
- 국토교통부_건축HUB_건축물대장정보 서비스

필요한 것만 신청해도 됩니다. 미신청 API 는 해당 도구에서만 오류가 납니다.

## 5. 로컬 확인

```bash
pip install -r requirements.txt
python test_local.py --mock          # 가짜 API 로 검증(키·인터넷 불필요)
VWORLD_API_KEY=... VWORLD_DOMAIN=... python test_local.py   # 실제 API(국내 IP 에서)
```

## 6. 환경변수 정리

| 변수 | 필수 | 설명 |
|---|---|---|
| `VWORLD_API_KEY` | ✅ | 브이월드 인증키 |
| `VWORLD_DOMAIN` | 권장 | 인증키에 등록한 서비스 URL |
| `DATA_GO_KR_KEY` | | 공공데이터포털 일반 인증키(Encoding 키를 넣어도 자동 변환) |
| `MCP_AUTH_TOKEN` | 권장 | 접근 토큰 |
| `PORT` | | 기본 8000 |
| `VWORLD_API_HOST` / `DATA_GO_KR_HOST` | | API 호스트 재정의 |
| `RE_MAX_CHARS` | | 응답 최대 길이, 기본 60000 |
| `MCP_ALLOWED_HOSTS` | | DNS 리바인딩 보호용 허용 호스트 |
