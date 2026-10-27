# TourAPI 운영 승인 반영 및 50건 실검증 보고서

작성일: 2026-10-27 KST
저장소: fitbike82/fittour-batch

## 변경 결과

기존 활성 수집기는 이미 KorService2의 areaBasedList2, detailCommon2, detailIntro2, detailImage2를 사용하고 있었으며 카탈로그는 음식점 39·숙박 32를 포함한 8개 유형을 지원했습니다. 이를 유지하면서 실제 병목이었던 1,000회 한도와 순차 호출을 개선했습니다.

- 국문 KorService2 각 operation의 KST 일일 한도를 100,000회로 상향했습니다. CLI에서 1~100,000 사이로 낮춰 사용할 수 있습니다.
- 승인 범위가 별도인 KorWithService2는 기본 1,000회를 유지하며 `--with-endpoint-daily-limit`로 분리했습니다. 국문 승인으로 다른 서비스의 승인까지 추정하지 않았습니다.
- 한도 정책 증가 시 승인 이전 차단 기록을 당일 한 번만 재개합니다. 사용량 카운터와 과거 기록은 초기화하지 않으며, 운영 한도에서 다시 발생한 실제 22/429 차단은 유지합니다.
- 재시도와 모든 페이지 요청을 예약 후 호출합니다. detailImage2와 음식점 메뉴 이미지 variant는 같은 operation 한도를 공유합니다. 실제 일일 초과 22와 초당 초과 23의 구분·제한 재시도를 유지했습니다.
- 카탈로그는 페이지당 100건, DB 쓰기 배치 1,000건입니다. 음식점·숙박 외 기존 관광/문화/행사/코스/레포츠/쇼핑 유형도 유지합니다.
- 카탈로그 workflow_dispatch의 mode로 full 전국 백필 또는 sync 증분 갱신을 선택합니다. 예약 실행은 기존 sync를 유지합니다. full 실행은 증분 체크포인트를 임의 전진시키지 않습니다.
- 상세 워크플로우는 최대 후보 50,000곳, 네트워크 프리페치 4워커, 공유 요청 시작 간격 0.1초로 실행합니다. 일일 승인과 초당 처리 한도는 다르므로 공급자 code 23 백오프를 유지합니다.
- SQLite RLock·트랜잭션으로 예산 예약/페이지 캐시를 보호하고, 워커별 HTTP Session을 분리합니다. 기존 단건 CLI 호환을 위해 workers 기본은 1이며 운영 워크플로우가 명시적으로 4를 지정합니다.
- DB 쓰기는 기존 트랜잭션 RPC를 순차 사용합니다. API 페이지 체크포인트가 먼저 저장되고 DB 성공 후에만 커밋 마커를 기록하므로 네트워크 순단·DB 실패 후 이어받기가 가능합니다. 전체 서브 이미지 페이지와 음식점 메뉴 이미지 variant를 계속 수집합니다.
- 실행 시간 2,700초를 유지합니다. 후보 상한은 한 번에 완료할 장소 수 보장이 아니며, 시간 제한 후 다음 실행이 미완료 페이지/엔드포인트를 이어받습니다.

## 실제 50건 검증

사용자 승인 범위 내에서 `scripts/verify_tour_production_live.py`로 운영 API와 실제 FitTour DB를 검증했습니다. 샘플은 전국 목록에서 음식점 25곳 + 숙박 25곳입니다. 알림은 전송하지 않았고 전국 전체 수집도 강제 실행하지 않았습니다.

| 항목 | 결과 |
| --- | --- |
| areaBasedList2 | 2회 / HTTP 200 모두 성공 / numOfRows=100 |
| 음식점 39 | 공급자 전체 13,415건 중 25건 선택 |
| 숙박 32 | 공급자 전체 2,915건 중 25건 선택 |
| detailCommon2 | 50회 / HTTP 200 모두 성공 |
| detailIntro2 | 50회 / HTTP 200 모두 성공 |
| detailImage2 | 50회 / HTTP 200 모두 성공 |
| 전체 HTTP | 152회, 모두 200 |
| 마스터 DB 적재 | 신규 44건·갱신 6건·비활성화 0건, 선택한 50개 ID 조회 확인 |
| 상세 DB 적재 | 50개 ID × 3 endpoints = 150개 페이로드, 원본 수정 버전 일치 확인 |
| 이미지 DB 조회 | 샘플에 연결된 이미지 레코드 323개 확인(기존 포함 조회 수이며 신규 증가량으로 표시하지 않음) |
| 검증 오류 | 0건 |

실제 응답은 v2 접근·형식·샘플 저장 성공을 확인한 것입니다. 100,000회 소진 실험은 수행하지 않았으며 승인 수치는 사용자가 제공한 운영계정 조건을 기준으로 설정했습니다.

## 회귀 검증

`python -B -m unittest`: 기존 120개와 이전 키 풀 검증을 포함한 전체 140개 통과. 신규 검증은 100,000회 경계, 공유 이미지 한도, 8개 스레드 동시 예약의 초과 방지, 승인 후 한 번만 차단 재개/사용량 보존, 4워커 음식점 12개 엔드포인트 수집, 100건 v2 목록 요청 및 키 디코딩입니다.

두 수정 워크플로우의 YAML 구문과 workflow_dispatch를 검증했습니다. 원격 CI 및 커밋 정보는 최종 적용 확인에 기록합니다.

## 변경 파일

- scripts/sync_places_universe.py: 페이지당 100건 및 Decoding 키 처리.
- scripts/sync_tour_enrichment_full.py: 국문 운영 한도, 서비스별 한도, 제한 병렬 프리페치, SQLite 보호, 한도 정책 전환, DB 읽기 배치 최적화.
- scripts/verify_tour_production_live.py: 알림 없는 50건 실검증·버전 일치 확인 도구.
- tests/test_tour_production.py: 운영 전환 회귀 테스트.
- .github/workflows/daily_tour_batch.yml: 국문 100,000회·별도 서비스 1,000회·4워커 운영 실행.
- .github/workflows/daily_places_sync.yml: full/sync 입력 및 1,000건 DB 배치.
- README.md: 실행 예시와 운영 정책.
- .gitignore: 이 공개 검증 보고서만 추적 예외 추가.
- docs/tour-production-upgrade.md: 이 보고서.

실제 응답·체크포인트는 Git 제외 경로 artifacts/tour-production-validation/에 보관했습니다. 실제 키·환경파일·SQLite·공급자 원문은 커밋하지 않습니다. local-trip-ai 파일은 수정하지 않았습니다. 기존 DB 스키마나 이력 마이그레이션은 수정하지 않고 이미 배포된 적재 RPC를 사용합니다.

공식 서비스 및 오류 코드 근거: https://www.data.go.kr/data/15101578/openapi.do
