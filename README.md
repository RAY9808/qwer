# korean-law 원격 MCP 서버

국가법령정보 OPEN API(law.go.kr/DRF)를 감싸는 **원격 MCP 서버**입니다.
기존 로컬 stdio 서버(`~/.claude/mcp-servers/korean_law_mcp.py`)와 도구·인자·동작이 같고,
전송 방식만 **Streamable HTTP** 로 바꿔 claude.ai 일반 대화창의 "커스텀 커넥터"로 등록할 수 있게 했습니다.

제공 도구: `search_law`, `get_law`, `search_precedent`, `get_precedent`, `search`, `get`

> 같은 방식의 **전자공시(OpenDART) 원격 MCP 서버**는 [`dart/`](dart/README.md) 폴더에 있습니다.

---

## 1. 접근 토큰

이 서버는 `MCP_AUTH_TOKEN` 으로 보호됩니다. 두 가지 방법 중 하나로 접근합니다.

| 방법 | 형태 |
|---|---|
| URL 경로 (claude.ai 등록용) | `https://<호스트>/s/<TOKEN>/mcp` |
| 헤더 | `POST https://<호스트>/mcp` + `Authorization: Bearer <TOKEN>` |

claude.ai 커스텀 커넥터는 임의 헤더를 넣을 수 없으므로 **URL 경로 방식**을 씁니다.

토큰은 저장소에 커밋하지 않습니다. 아래로 생성해 Render 환경변수에만 넣으세요.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

이 문서에서 `<여기에-본인-토큰>` 으로 표기된 자리에 생성한 값을 넣으면 됩니다.

> 토큰이 URL에 들어가므로 이 주소를 남에게 공유하지 마세요.
> 유출되면 환경변수 `MCP_AUTH_TOKEN` 만 새 값으로 바꾸면 즉시 무효화됩니다.

---

## 2. 로컬 확인

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe test_local.py
```

`test_local.py` 는 서버를 띄워 헬스체크 → 401 차단 → initialize → tools/list →
`get_law(민법, 제406조)` 실호출까지 확인합니다. (이미 전체 통과 확인 완료)

직접 띄워보려면:

```bash
LAW_API_OC=<본인-OC값> MCP_AUTH_TOKEN=<TOKEN> PORT=8000 python server.py
```

---

## 3. 배포 — Render (무료 플랜, 권장)

### 3-1. GitHub 저장소 만들기

이 폴더에서:

```bash
git init && git add -A && git commit -m "korean-law remote MCP server"
```

GitHub에 비공개(private) 저장소를 만들고 푸시합니다.

```bash
git remote add origin https://github.com/<사용자명>/korean-law-mcp.git && git branch -M main && git push -u origin main
```

### 3-2. Render에서 서비스 생성

1. [render.com](https://render.com) 가입/로그인
2. **New → Web Service** → 위 GitHub 저장소 연결
3. 설정값 (저장소에 `render.yaml` 이 있으면 자동으로 채워집니다)
   - Runtime: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python server.py`
   - Health Check Path: `/healthz`
4. **Environment** 탭에서 환경변수 추가

   | Key | Value |
   |---|---|
   | `LAW_API_OC` | `<본인-OC값>` |
   | `MCP_AUTH_TOKEN` | 위에서 만든 토큰 |
   | `MCP_ALLOWED_HOSTS` | (배포 후) `korean-law-mcp.onrender.com` |

5. Deploy 완료 후 주소 확인 (예: `https://korean-law-mcp.onrender.com`)

### 3-3. 동작 확인

```bash
curl https://korean-law-mcp.onrender.com/healthz
```

`ok` 가 나오면 정상입니다.

> **무료 플랜 주의:** 15분간 요청이 없으면 서버가 잠들고, 다음 요청 때 30~60초간
> 콜드 스타트가 걸립니다. 이 때 claude.ai 커넥터 연결이 실패할 수 있습니다.
> 대응책은 (a) 유료 인스턴스로 올리거나, (b) 외부 모니터링(UptimeRobot 등)으로
> `/healthz` 를 10분마다 호출해 깨워두는 것입니다.

---

## 4. 배포 — Docker (Railway / Fly.io / 개인 서버)

`Dockerfile` 이 포함되어 있습니다.

```bash
docker build -t korean-law-mcp .
```

```bash
docker run -p 8000:8000 -e LAW_API_OC=<본인-OC값> -e MCP_AUTH_TOKEN=<TOKEN> korean-law-mcp
```

Railway/Fly.io 는 이 Dockerfile을 그대로 인식합니다. 환경변수만 동일하게 넣으면 됩니다.
어느 쪽이든 **HTTPS 주소**여야 claude.ai에 등록할 수 있습니다.

---

## 5. claude.ai 일반 대화창에 등록

1. [claude.ai](https://claude.ai) → 좌하단 프로필 → **Settings(설정)**
2. **Connectors(커넥터)** → **Add custom connector**
3. 입력값
   - **Name**: `국가법령정보` (자유롭게)
   - **Remote MCP server URL**:
     ```
     https://korean-law-mcp.onrender.com/s/<여기에-본인-토큰>/mcp
     ```
     (호스트명과 토큰은 실제 배포값으로 바꾸세요. **끝의 `/mcp` 를 빠뜨리지 마세요.**)
   - OAuth Client ID / Secret: **비워 둡니다**
4. **Add** → 커넥터 목록에 `korean-law` 도구 6개가 보이면 성공
5. 대화창 입력란의 도구/커넥터 아이콘에서 이 커넥터를 켜고 질문하면 됩니다.

예시 질문: "민법 제406조 조문 가져와줘", "사해행위취소 대법원 판례 찾아줘"

---

## 6. Claude Code(로컬 CLI)에서 원격 서버를 쓰고 싶다면

기존 stdio 설정을 그대로 두어도 되지만, 원격으로 바꾸려면:

```bash
claude mcp add --transport http korean-law-remote https://korean-law-mcp.onrender.com/s/<TOKEN>/mcp
```

---

## 7. 환경변수 정리

| 변수 | 필수 | 설명 |
|---|---|---|
| `LAW_API_OC` | ✅ | law.go.kr 오픈API OC 값 (보통 신청 시 쓴 이메일 ID) |
| `MCP_AUTH_TOKEN` | 권장 | 접근 토큰. 비우면 인증 없이 공개됨 |
| `PORT` | | 리슨 포트, 기본 8000 (Render/Railway가 자동 주입) |
| `MCP_ALLOWED_HOSTS` | | DNS 리바인딩 보호용 허용 호스트(쉼표 구분). 비우면 보호 해제 |
| `LAW_API_HOST` | | 기본 `https://www.law.go.kr` |
| `LAW_MAX_CHARS` | | 응답 최대 길이, 기본 60000 |
| `MCP_JSON_RESPONSE` | | `1` 이면 SSE 대신 순수 JSON 응답 |

---

## 8. 파일 구성

```
server.py         원격 MCP 서버 본체 (도구 6개 + 토큰 게이트)
test_local.py     로컬 스모크 테스트
requirements.txt  의존성 (mcp>=2.0, uvicorn)
Dockerfile        컨테이너 배포용
render.yaml       Render Blueprint
.env.example      환경변수 예시
```
