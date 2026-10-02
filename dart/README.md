# opendart 원격 MCP 서버

금융감독원 전자공시 OPEN API(opendart.fss.or.kr)를 감싸는 **원격 MCP 서버**입니다.
상위 폴더의 korean-law 서버와 구조·접근 제어(`MCP_AUTH_TOKEN`)·배포 방식이 같고,
claude.ai 일반 대화창의 "커스텀 커넥터"로 등록해 씁니다.

## 제공 도구

| 도구 | 설명 |
|---|---|
| `find_corp` | 회사명·종목코드 → DART 고유번호(corp_code) |
| `company_info` | 기업개황(대표자, 업종, 주소, 결산월 등) |
| `search_disclosures` | 공시 목록 검색(기간·공시유형·법인구분) |
| `financials` | 재무제표 주요계정(여러 회사 비교 가능) / 전체 재무제표(연결·별도) |
| `report_info` | 정기보고서 주요정보(배당, 최대주주, 임원, 직원, 보수, 자기주식, 감사의견 등 16종) |
| `shareholding` | 대량보유 상황보고 / 임원·주요주주 소유보고 |
| `get_document` | 공시 원문을 텍스트로 읽기(길면 offset 으로 이어 읽기) |
| `dart_api` | 그 밖의 OpenDART JSON API 직접 호출(주요사항보고서 등) |

인증키는 서버가 자동으로 붙이며, 응답에 표시되는 요청 URL에서도 가려집니다.

---

## 1. 로컬 확인

```bash
pip install -r requirements.txt
python test_local.py --mock                  # 가짜 DART 서버로 검증(인증키 불필요)
DART_API_KEY=<인증키> python test_local.py   # 실제 OpenDART 로 검증
```

---

## 2. 배포 — Render

저장소 루트의 `render.yaml` 에 `opendart-mcp` 서비스(`rootDir: dart`)가 정의되어 있습니다.

- **Blueprint 로 korean-law 를 만들었다면:** Render 대시보드 → Blueprints → 해당 Blueprint →
  **Manual Sync** 를 누르면 `opendart-mcp` 가 새로 생깁니다.
- **수동으로 만들었다면:** New → Web Service → 같은 저장소 선택 후
  - Root Directory: `dart`
  - Build Command: `pip install -r requirements.txt`
  - Start Command: `python server.py`
  - Health Check Path: `/healthz`

환경변수(Environment 탭):

| Key | Value |
|---|---|
| `DART_API_KEY` | OpenDART 인증키(40자) |
| `MCP_AUTH_TOKEN` | 접근 토큰(`python -c "import secrets; print(secrets.token_urlsafe(32))"`) |

배포 후 `https://opendart-mcp.onrender.com/healthz` 가 `ok` 이면 정상입니다.

> 무료 플랜은 15분 무요청 시 잠들고, 무료 인스턴스 시간(월 750시간)은 계정 내 서비스가 나눠 씁니다.
> korean-law 와 둘 다 UptimeRobot 등으로 상시 깨워두면 월 한도를 넘을 수 있습니다.

---

## 3. claude.ai 에 등록

1. claude.ai → Settings → **Connectors** → **Add custom connector**
2. Name: `OpenDART` / Remote MCP server URL:
   ```
   https://opendart-mcp.onrender.com/s/<MCP_AUTH_TOKEN>/mcp
   ```
   (끝의 `/mcp` 필수, OAuth 칸은 비워 둠)
3. 대화창 도구 메뉴에서 커넥터를 켜고 질문합니다.

예시 질문: "삼성전자 2024년 사업보고서 매출액·영업이익 알려줘",
"카카오 최근 3개월 주요사항보고 공시 찾아서 내용 요약해줘"

Claude Code 에서는:

```bash
claude mcp add --transport http opendart https://opendart-mcp.onrender.com/s/<TOKEN>/mcp
```

---

## 4. 환경변수 정리

| 변수 | 필수 | 설명 |
|---|---|---|
| `DART_API_KEY` | ✅ | OpenDART 인증키 |
| `MCP_AUTH_TOKEN` | 권장 | 접근 토큰. 비우면 인증 없이 공개됨 |
| `PORT` | | 리슨 포트, 기본 8000 (Render 가 자동 주입) |
| `MCP_ALLOWED_HOSTS` | | DNS 리바인딩 보호용 허용 호스트(쉼표 구분) |
| `DART_API_HOST` | | 기본 `https://opendart.fss.or.kr` |
| `DART_MAX_CHARS` | | 응답 최대 길이, 기본 60000 |
| `MCP_JSON_RESPONSE` | | `1` 이면 SSE 대신 순수 JSON 응답 |
