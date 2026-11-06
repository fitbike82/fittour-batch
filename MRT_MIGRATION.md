# MRT Collector 이관

운영 패키지 15개는 affiliate-analyzer의 `src/mrt_collector`를 수정 없이 복사했다.
런타임은 Python 3.10+ 표준 라이브러리만 필요하다. 기존 requirements는 변경하지 않는다.
`PYTHONPATH=src python -m mrt_collector --help`로 기존 CLI를 사용한다.
`db/` SQL 및 타입 매핑은 로컬 설계/SQLite 테스트용이며 자동 적용하지 않는다.
기존 config 2개를 복사했다. data/regions_kr.json은 제외 지시에 따라 이관하지 않았으므로
전국 지역 기반 수집은 별도 지역 데이터 준비 후 사용한다.

## Actions

`.github/workflows/mrt_api_smoke.yml`은 workflow_dispatch만 지원한다.
저장소 Secret `MRT_API_KEY` 등록 후 수동 실행한다. 로컬 실 API 호출은 수행하지 않았다.
검색 1회 성공 후 적격 숫자 ID와 HTTPS URL이 있는 첫 상품만 사용한다.
MyLink 최대 1회, MCP getTnaDetail 최대 1회이며 재시도는 없다.
MCP initialize와 initialized 알림은 프로토콜 준비 요청으로 별도 발생한다.
HTTP 오류(401/403/429 포함), 전송 오류, API/MCP 오류 시 즉시 실패하고 이후 호출하지 않는다.
상품이 없으면 후속 호출을 건너뛴다. 정상 MyLink 응답에 검증 링크가 없으면 실패한다.
원본·인증정보·오류 본문을 로그/아티팩트로 출력하지 않는다. 응답은 임시 디렉터리에서 삭제한다.
매 실행은 연결 검증용이며 영속 상품/링크 DB를 만들지 않는다. 수동 재실행은 같은 상품의
링크를 다시 요청할 수 있다. 운영 CLI의 영속 DB 기반 링크 재사용과 429 재시도는 그대로 유지한다.

## DB 검토 (2026-11-06)

연결된 Supabase 프로젝트는 fitbike-prod(farjyjcvduthawpdjuqe)뿐이다.
읽기 전용 메타데이터 조회에서 상품 테이블은 타이어/배터리/브레이크 계열이며,
전체 비시스템 스키마의 affiliate/mrt/myreal/tour_product 이름 테이블은 없었다.
FitTour 운영 DB와 동일하다는 근거가 없으므로 이 결과로 FitTour 테이블 부재를 단정하지 않는다.
**재사용 가능 테이블과 신규 필요 여부의 최종 판단은 FitTour DB 연결 후 확인해야 한다.**
동일 구조가 없다면 독립 비공개 mrt_collector 스키마의 affiliate_companies,
affiliate_products, affiliate_links 및 product_observations/product_prices/product_locations,
collector_runs/collector_migrations와 작업 체크포인트 테이블을 신규 후보로 검토한다.
기존 부품 상품 테이블은 MRT 상품 저장에 재사용하지 않는다. 실제 DB 변경은 없다.

| SQLite 키/컬럼 | PostgreSQL 대응/주의 |
|---|---|
| INTEGER PRIMARY KEY id | BIGINT identity, 원본 ID 보존; 기존 ID 충돌 시 명시적 매핑 필요 |
| affiliate_company_id, affiliate_product_id, product_id, observation_id | BIGINT FK; 부모 먼저 적재, 다른 시스템 ID와 병합 금지 |
| company code | TEXT UNIQUE; 기존 회사 ID=1을 가정하지 말고 code로 매핑 |
| company + provider_product_id + product_type | 상품 복합 UNIQUE 유지; provider ID는 TEXT |
| company + target_url + placement | 링크 UNIQUE 유지; placement 간 동일 상품/URL 링크 재사용 정책 보존 |
| event_key, job_id/job_key, run_id | TEXT 및 원본 PK/UNIQUE 보존; event_key 재적재 중복 방지 |
| *_json, raw_json, value_json | JSONB로 디코딩하고 *_json_text로 원문 보존; 숫자/문자열 타입 유지 |
| *_at | TIMESTAMPTZ; NULL은 SQL NULL, 조건별 가격/원본 관측 유지 |
| is_active | 기존 설계는 BIGINT; 기존 boolean 테이블 재사용 시 명시적 변환 |

전체 컬럼 매핑은 db/type_mapping.json, 원본 PostgreSQL 설계는 db/postgres_schema.sql 참고.
실 DB 적재, sequence 조정, RLS/권한 검증은 수행하지 않았다.
