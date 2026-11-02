# 잔여 백년가게 후보군 AI 매칭

## 운영 전환과 최종 보고서

최초 운영 실행은 `docs/century-ai-checkpoint.json`을 복원한다. 검증 완료된 50건
(매칭 14건, 불일치 36건)은 다시 AI 호출하지 않으며 남은 178건을 이어서 처리한다.
이후 실행은 같은 브랜치의 최신 Actions 아티팩트를 우선한다.

매 실행 뒤 `artifacts/century-ai-matching/final_summary.json`을 생성하고 Actions
작업 요약과 `century-ai-matching-state` 아티팩트에 게시한다. `remaining=0`이고
DB 매칭 식별 검증이 통과하면 `completed=true`가 된다. `db_century_total`은
DB 실측 누적 총계이며, `deployment_statuses`는 운영 전환 당시 178건의 결과다.
공급자 쿼터나 장애로 이월되면 미완료 상태와 잔여 수를 그대로 표시한다.

`workflow_dispatch`의 `summary_only=true` 또는 아래 명령으로 AI 호출 없이
체크포인트와 DB 결과를 검증하고 보고서를 다시 생성할 수 있다.

```sh
python scripts/century_ai_matching.py --summary-only
```

백년가게 매칭은 기존 places 컬럼을 사용한다. 별도 Gemini 리뷰 분류 기능의
DB 배포 순서는 `sql/place_taxonomy_setup.sql`, `sql/ai_review_refresh.sql`이다.
관리 API 토큰 만료나 DB 권한 부족 시 SQL 반영은 별도로 완료해야 한다.

```sh
python scripts/century_ai_matching.py --prepare
python scripts/century_ai_matching.py --limit 50 --slot manual
```

첫 명령은 읽기 전용으로 큐만 준비한다. 두 번째는 최대 50건을 AI 심사하고 HIGH
판정만 조건부 PATCH한다. 기본 입력은 `docs/unmatched_century_stores.json`이며,
951개 원본 항목의 연번·주요사업·지역·주소·연락처를 보존한다. 원본 대기열을
덮어쓰지 않고 결과/진행 상태를 `artifacts/century-ai-matching/`에 저장한다.

## PM 사전과 후보 압축

비외식 판정은 `src/lib/century-food-filter.ts`의 PM 블랙리스트와 구제 사전만
사용한다. `taxonomy:sync`로 내보낸 JSON을 Python도 읽는다. 구제 사전을 먼저
검사하고, 블랙리스트에 걸리면 제외하며, 그 외는 음식군이다. 빈 주요사업은 제외한다.
상호나 AI 추정으로 업종을 추가하지 않는다. 제공된 사전의 문자 포함 규칙을 그대로
사용하므로 `떡/방앗간`은 포함, `쌀국수`는 쌀 때문에 제외되고, 사전에 없는
`세탁`은 기본 포함이다. 이 정책 변경은 PM 사전 자체에서 수행해야 한다.

DB는 활성 `content_type_id=39` 음식점/카페를 500행씩 조회한다. 이미 지정된
장소는 새로운 연결 후보에서 제외한다. 주소를 한 번 정규화하고 같은 시군구
후보 목록을 재사용한다. 법인·본점 표기를 정리한 상호를 한글 자모(NFD)로
분해한 유사도가 0.72 이상이거나 핵심 상호에서 연속 2글자 이상이 같으면 후보로
삼는다. 유사도/핵심어/건물번호 점수순으로 3개까지만 AI에 제공한다.

- `unmatched_skipped_nonfood.json`: PM 사전으로 제외된 항목.
- `unmatched_no_candidate.json`: 현행 시군구/상호 규칙에서 후보를 찾지 못한 항목.
- `queue_ai_matching.json`: 1~3개 후보가 있는 AI 큐와 재개 상태.
- `report.json`: 이번 회차의 심사·매칭·검토·오류·호출 수.

후보가 없다는 것은 이 추출 규칙에서 후보를 찾지 못했다는 뜻이다. DB에 어떤
형태로도 존재하지 않음을 증명한 결과는 아니며, 비정상 주소/오타는 별도 보강 대상이다.

## 호출 제한과 분할 실행

매 HTTP POST 직전에 4.5초 간격과 60초 이동 구간당 15회 상한을 함께 검사한다.
키 교체·재시도도 같은 게이트를 통과한다. 최근 호출 시각과 최근 500회 감사용
타임스탬프를 JSON에 저장하므로 재시작 시에도 창을 이어받는다. 회차 시작에는
다른 직렬 Gemini 작업의 마지막 창을 피하기 위해 60초 대기한다. 같은 상태
디렉터리의 로컬 병렬 실행은 잠금 파일로 거부한다.

KST 날짜와 슬롯별 시작 건수를 **API 호출 전에** 기록한다. 슬롯당 50건, 모든
슬롯 합계 하루 250건이 최대다. 실패 재시도도 건수에 포함된다. 동일 슬롯 재실행은
남은 한도만 실행한다. 20분의 전체 시간 예산을 사용하며 한도/시간/쿼터 경계에서는
미완료 항목을 보존한다. 429는 일일 소진/일시 제한을 기존 Gemini 코드로 분류하고
미완료 작업을 retry로 남긴다. 호출 제한은 이 배치와 동일 조정 그룹에 적용되며,
다른 앱이 같은 Google 프로젝트로 보내는 호출/TPM/RPD까지 보장하지 않는다.

기존 리뷰 워커와 Actions `concurrency.group=ai-summary-batch`를 공유한다.
새 워크플로는 02:00/04:00/04:30/05:30/06:30 KST의 5개 cron을 정의한다.
UTC로는 17:00/19:00/19:30/20:30/21:30이다. GitHub 스케줄 지연이나 앞선
작업 때문에 실제 시작은 늦어질 수 있으며, 소스/워크플로를 배포해야 활성화된다.

## 상태 보존과 운영

Actions는 같은 브랜치의 직전 `century-ai-matching-state` 아티팩트를 복원한다.
회차가 실패해도 JSON 체크포인트를 90일 아티팩트로 보존한다. 기존 상태가 있으나
만료/다운로드 실패하면 조용히 초기화하지 않고 중단한다. 입력 SHA-256이 바뀌면
운영자가 체크포인트를 검토하기 전까지 진행하지 않는다. 최초 실행만 소스 대기열로
생성한다. 강제 종료가 업로드 전에 일어나면 일부 AI 심사는 재실행될 수 있으나,
DB의 이미 매칭된 연번과 관찰값 비교로 중복 장소 지정은 방지한다. API exactly-once
실행을 보장하는 영구 DB 작업 큐는 아니다.

Gemini 1.5/2.0을 고정하지 않는다. 2.0 Flash는 공식 종료되었으므로 저장소의
`gemini-3.5-flash-lite`가 기본이며 `CENTURY_MATCHING_MODEL` 또는 `--model`로
교체한다. 유료 티어 전환/결제/쿼터 상향은 수행하지 않는다. 실제 무료 사용 가능 여부와
프로젝트의 모델별 한도는 운영 AI Studio 설정에서 확인해야 한다.

## 판정과 DB 반영

`matched_index`는 실제 후보 개수 이내의 정수만 허용한다. 0이면 NONE이어야 하며,
반대 조합/후보 밖 ID/잘못된 reason은 거부한다. HIGH만 매칭하고 MEDIUM/LOW는
review, 0/NONE은 no_match로 보존한다. 프로바이더/파싱 오류는 최대 3회차 후 review로
넘긴다. AI가 도구를 호출하거나 임의 장소를 생성할 수 없다.

DB PATCH는 기존 리비전·상호·주소·전화·분류·태그·지정 이력을 모두 비교한다.
수동 분류와 이미 다른 CSV가 연결된 장소는 덮어쓰지 않는다. PM v2 L1/L2와
메뉴 태그를 쓰고 백년가게 태그를 보존한다. 출처는 `century_ai_match:v1`, 이력에는
AI 모델·HIGH 판정/근거·2022년 자료 날짜를 함께 저장한다. 현재 인증 유지가
확인됐다는 표시를 만들지 않는다. 새 DB 스키마나 신규 TourAPI 호출은 필요 없다.

로컬에서 JSON을 다른 디렉터리로 복사해 동시에 실행하지 않는다. 프로세스 비정상
종료로 run.lock이 남으면 해당 프로세스가 종료됐음을 확인한 후에만 잠금을 제거한다.
