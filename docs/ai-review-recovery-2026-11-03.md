# AI 리뷰 배치 장애 조사 및 복구 — 2026-11-03 KST

## 결과와 현재 적용 범위

- 사용자가 이번 리뷰 배치의 DB를 `qiuojsjorjnuedludklw`로 확인했다. 앞선 데이터 보정 SQL의 `sijsjhnbapsbhfpabbjr`와 별개다.
- 로컬 코드 수정 및 한 장소 실제 API 검증 완료. `130712`: 본문 10개, Gemini `gemini-3.5-flash-lite` HTTP 200, 검증된 요약 반환, 전체 38.88초, 생성 요청 5.354초. DB 쓰기·claim·알림은 각각 0이다.
- Python 전체 203개 통과(최종 소규모 후속 변경 전). 마지막 변경 후 관련 회귀 41개와 큐 SQL PGlite 통합 검사 통과. 후속 결과는 `artifacts/ai-review-recovery/final-regression.log`에 보관한다.
- 운영 큐 함수 교체는 자동 승인 검토가 **거절**했다. 실행되지 않았으며 DB 함수는 기존 상태다. 사유: 구체적인 운영 함수 교체 및 큐 변이에 대한 명시적 승인 부족. 저장소 push/배포·Actions 강제 실행·운영 캐시 초기화도 하지 않았다.

## 1. 조기 종료와 쿼터 상태

### 확인된 사실

원격 실행 `41585234684`의 보고서는 selected=50, places_started=1, processed=0,
deferred=50, quota_429_total=0, model_calls={}이다. 첫 장소 130712에서 네이버 5개와
카카오 5개의 읽을 수 있는 본문을 수집했지만 Gemini 요청은 없었다. 종료 사유는
`daily_quota_exceeded`, 보고서 처리 시간은 22.5초다.

해당 실행 로그는 `gemini-quota-v1-2026-11-02-41579330475-1` 캐시 복원을 보여준다.
저장 파일은 `artifacts/gemini-key-pool/state.json`, 설정은 `GEMINI_KEY_POOL_STATE`다.
원격 로그의 테스트 단계에는 가짜 429도 있으므로 실제 호출 횟수는 운영 보고서로 구분했다.

기존 v1 파일은 Pacific 날짜와 SHA-256 키 지문 목록을 저장했다. 시작 시 날짜가 같고
지문이 일치하는 키만 차단한다. 다음 Pacific 날짜 및 키 교체 시 복원되지 않는 코드가
이미 있었다. 따라서 **영구 날짜 락으로 확정할 수 없다**. 11월 3일 14:40 KST는 여전히
Pacific 11월 2일이다. 이 시점의 11월 2일 캐시 복원 자체는 정상적인 날짜 동작이다.
Google의 RPD 리셋은 Pacific 자정이며 KST 자정이 아니다.

### 수정

- v2 상태에 키별 `blocked_at`을 추가하고 4시간 TTL(`GEMINI_QUOTA_BLOCK_TTL_SECONDS=14400`) 적용.
- 실행 중에도 `current()`/`exhaust()`에서 날짜 변경과 TTL을 검사한다. 날짜 변경은 일시 차단도 해제한다.
- 기존 v1 상태는 파일 수정 시각을 이용해 유한 TTL로 호환한다. 잘못된 미래/비유한 타임스탬프는 해제한다.
- TTL 만료는 제한된 재시도를 허용할 뿐 실제 할당량을 초기화하지 않는다. 다시 실제 일일 소진 응답을 받으면 다시 차단한다.
- 일시 429로 키 풀이 소진된 경우 `key_pool_exhausted`로 구분한다. 모든 키가 실제 일일 소진으로 차단된 경우만 `daily_quota_exceeded`다. 실제 처리/해제 오류는 성공으로 숨기지 않는다.
- 비429 오류를 사이에 둔 429를 연속 429로 잘못 누적하지 않도록 streak를 초기화한다.

수동 초기화(로컬 파일만 변경, API 호출 없음):

```powershell
python scripts/gemini_key_pool.py --reset-state artifacts/gemini-key-pool/state.json
```

Actions에는 `workflow_dispatch.reset_quota_state` boolean을 추가했다. 배포 후 true로 실행하면
**캐시 복원 이후** 위 명령을 수행하고 빈 상태를 새 캐시로 저장한다. 로컬 파일만 삭제해도
원격 캐시가 삭제되지는 않는다. 기존 프로세스의 메모리 차단 해제에는 새 프로세스가 필요하다.

## 2. HTTP 400의 원인 분리 및 수정

원천 후기 수집 장애가 아니라 Gemini 구조화 출력 스키마 경로에서 재현됐다.
동일 인증키와 `gemini-3.5-flash-lite`로 다음 결과를 얻었다.

| 비교 요청 | 실제 HTTP 결과 |
|---|---|
| 130712 기존 실제 후기/기존 스키마 요청 | 400 INVALID_ARGUMENT |
| responseJsonSchema로 표현만 변경한 실제 요청 | 400 INVALID_ARGUMENT |
| 최소 텍스트 요청 | 200 |
| 최소 JSON Schema | 200 |
| 후기 + 분류 전체 복합 JSON Schema | 400 INVALID_ARGUMENT |
| 후기 스키마만 사용 | 200 |
| 같은 복합 스키마에서 filter_tags.items.enum만 제거 | 200 |
| 수정 후 130712 실제 후기 전체 요청 | 200, 요약 검증 통과 |

서버가 제공한 메시지는 `Request contains an invalid argument.`이며 더 구체적인 설명은 없었다.
따라서 Google 내부 실패 원인을 특정할 수는 없지만, **큰 태그 enum을 포함한 배열 스키마가
400을 유발한다는 것은 통제 비교로 재현**했다. 공식 문서도 복잡한 스키마가 거절될 수 있음을 설명한다.
모델명 자체가 잘못됐다는 가설은 같은 모델의 200 응답으로 배제됐다. 모든 fallback 모델의
현재 프로젝트별 가용성을 검증했다는 의미는 아니다.

수정은 REST `generationConfig.responseJsonSchema`, 소문자 JSON Schema 타입 및 null 표현을
사용한다. `contents`에는 명시적 `role=user`를 추가했다. 큰 태그 enum은 전송 스키마에서
제거하되 허용 목록 전체를 시스템 지시에 포함하고 **기존 서버 whitelist 검증은 유지**했다.
배열 타입·최대 12개·L1/L2 enum·근거 검증은 유지한다. 전역 PM 분류 원본은 변경하지 않았다.
이 경로는 requests 기반 REST 호출이며, Google SDK Python의 snake_case 인자를 REST JSON에
그대로 보내는 방식으로 바꾸지 않았다.

오류 보고에는 실제 응답 JSON의 `error.code/status/message`를 길이 제한·키 마스킹 후 기록한다.
요청 본문과 인증 헤더는 기록하지 않는다. 키별 이벤트에도 식별자만 붙인다.

증거: `before-payload-fix.json`, `after-payload-fix.json`, `contract-probe.json`,
`contract-isolation.json`, `final-smoke.json` (모두 `artifacts/ai-review-recovery/`).

## 3. database_57014와 운영 SQL 불일치

관리 API의 읽기 전용 조회는 HTTP **201**로 성공했다. 이 API는 200만 성공으로 판단하면 안 된다.
연결 Supabase MCP는 여전히 다른 프로젝트만 노출하지만, 로컬 관리 자격증명으로 지정 프로젝트를
직접 조회할 수 있었다. 이전 날짜 문서의 관리 인증 실패를 현재 상태로 가정하지 않았다.

운영 `enqueue_due_ai_reviews` 실제 정의는 다음 결함을 가진 구버전이었다.

- blocking `pg_advisory_xact_lock(62020120)` 사용.
- 기존 미완료 작업 확인보다 먼저 전체 활성 장소의 INSERT를 시도.
- 기존 job 제외 선필터 및 `LIMIT batch_limit` 부재.
- `ON CONFLICT DO NOTHING`으로 기존 행을 버리기 전까지 스캔과 조인/충돌 검사를 수행.

`ai_review_jobs_pending_idx`는 이미 존재한다. 활성 content_id 부분 인덱스도
`places_active_id_feed_idx` 이름으로 존재한다. 따라서 인덱스가 없다고 단정하거나
같은 인덱스를 추가 생성할 필요는 없다. 조사 순간 다른 세션의 blocking PID는 없었다.
이 관측이 과거 타임아웃 당시 lock 경합이 없었다는 증거는 아니다. 57014의 과거 정확한
실행/대기 시간 기여도는 당시 DB 로그나 실행 계획 없이는 확정하지 않는다.

`sql/ai_review_refresh.sql`에는 이미 제한·선필터·비차단 락 개선안이 있었으나 해당 운영
함수에 미반영이었다. 이번에는 함수 단위 lock_timeout=2s를 추가하고 기존 pending
부분 인덱스가 standalone 패치에서도 보장되게 했다. 워커는 55P03/57014 등의 명시적
트랜잭션 실패만 제한 재시도하고 기존 큐 작업은 계속 시도한다.

### 승인할 운영 변경

`scripts/deploy_ai_review_queue_recovery.py --project qiuojsjorjnuedludklw`

1. 현재 함수 정의를 `artifacts/ai-review-recovery/enqueue-before.sql`에 백업한다.
2. 검토된 `sql/ai_review_refresh.sql`에서 **enqueue_due_ai_reviews 함수만** 추출해 트랜잭션으로 교체한다.
3. 기존 함수 identity/권한과 다른 완료·보관 함수는 변경하지 않는다. 동등한 인덱스도 추가하지 않는다.
4. `enqueue_due_ai_reviews(1,180)`으로 검증하고 시간·반영 건수를 보고한다. 검증은 최대 1건 큐 생성/재등록을 할 수 있다.

이 작업은 자동 승인 검토에서 거절됐으며 우회 실행하지 않았다. 승인 후 실행할 수 있는
구체적인 배포 도구까지 준비된 상태다. 원복은 백업된 CREATE OR REPLACE FUNCTION을
같은 프로젝트에 트랜잭션으로 재적용한다.

## 4. 검증과 재현

```powershell
python -m unittest -q
node --experimental-strip-types --test tests/node/review-refresh.test.ts
python scripts/verify_ai_review_recovery.py --expected-project qiuojsjorjnuedludklw --content-id 130712
```

검증 도구는 DB SELECT만 수행하고 운영 키 풀 상태를 변경하지 않는다. 한 모델의 초기 생성 1회와
기존 근거 교정 최대 1회만 허용한다. 서버 결과 검증에 통과한 경우만 success=true를 저장한다.
이번 정상 결과는 basic_info fallback이 아닌 실제 후기 요약이다. 데이터 저장 성공이나 운영
예약 배치 복구 완료를 의미하지 않는다.

정상 API 결과를 파일에 저장한 뒤 Windows CP949 콘솔이 원문 유래 Unicode 문자 출력에서
오류를 냈다. API 성공과 콘솔 오류를 구분하며, 검증 CLI에 UTF-8 출력을 명시해 보완했다.

Windows의 tsx 초기화가 `uv_os_get_passwd ENOMEM`으로 실패해 SQL 통합 테스트는 Node의
기본 타입 제거 기능으로 실행했고 통과했다. Python 실행 환경은 로컬 설치 3.14이며
Actions의 Python 3.12 CI 결과를 이번 로컬 결과로 대체 주장하지 않는다.

배포 후 필요 작업: 운영 큐 함수 교체 승인, 검토된 소스의 원격 반영, 첫 수동 배치 limit=1,
캐시 reset 필요 시 일회성 플래그 사용, 결과 저장·lease 해제·후기분석/basic_info 구분 확인.
유료 티어 전환·결제·쿼터 증액은 수행하지 않았다. 프로젝트의 실제 무료 티어 여부/사용량은
AI Studio 운영 설정에서 관리한다.

공식 근거:
- https://ai.google.dev/gemini-api/docs/rate-limits — 프로젝트 단위 한도와 Pacific 자정 리셋.
- https://ai.google.dev/gemini-api/docs/generate-content/structured-output — JSON Schema와 복잡성 제약.
- https://ai.google.dev/api/generate-content — generateContent REST 계약.
