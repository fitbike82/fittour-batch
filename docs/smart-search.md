# TASK-034 스마트 블로그 검색

기존 후기 수집과 같은 블로그 검색을 대상으로 한다. 네이버 지역 검색/카카오 로컬 검색과는 별개이다.

## 설정

서버 환경에 `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`을 모두 설정하면 네이버를 우선 사용한다.
둘 중 하나라도 비어 있으면 네이버를 건너뛴다. `KAKAO_REST_API_KEY`(또는 기존
`KAKAO_API_KEY`)가 있으면 카카오가 후순위로 동작한다. 네이버만 설정해도 실행할 수 있다.
GitHub Actions에서는 같은 이름의 repository secrets를 등록한다. 실행 중인 프로세스는
환경변수 적용 후 다시 시작해야 한다. 값이 채워진 환경 파일은 커밋하지 않는다.

## TypeScript

```ts
import { buildSmartQueries } from '../src/utils/querySanitizer.js';
import { searchPlace } from '../src/lib/search.js';

buildSmartQueries({ title: '(주) 맛집 [본점]', sigungu: '강남구' });
// ['강남 맛집', '맛집']
const result = await searchPlace({ title: '(주) 맛집 [본점]', sigungu: '강남구' });
// { provider, query, documents: [{ title, url, contents, datetime }], attempts }
```

`SearchAdapter` 인터페이스를 구현해 `searchPlace(params, adapters)`에 전달하면 다른
검색 공급자도 추가할 수 있다. 기본 어댑터는 요청당 15초 제한, 최대 10개 결과를 사용한다.
네이버의 지역 포함 쿼리 → 상호명 단독 쿼리 → 카카오의 같은 후보 순으로 시도하며,
첫 비어 있지 않은 결과를 반환한다. 공급자 요청 오류가 나면 그 공급자의 나머지 후보를
생략하고 다음 공급자로 이동한다. 모두 실패하면 `all_search_providers_failed`를 던진다.
정상적인 빈 검색 결과는 빈 documents로 구분한다. `attempts`에는 원문 응답이나 인증값을 넣지 않는다.

## Python 운영 배치

`scripts/smart_search.py`를 두 기존 후기 수집 경로에 연결했다. 각 공급자에서 스마트
쿼리별로 기존 후기 접미어를 시도한다. 네이버 검색 문서를 카카오와 같은 필드로 변환한 뒤
기존 원문 수집·광고 필터·중복 제거·최소 후기 수 검증을 적용한다. 충분한 유효 후기가
확보되면 즉시 검색을 멈추고, 부족하면 다음 후보와 다음 공급자로 진행한다.
`search_reviews`와 `search_kakao_blogs`는 선택적 `sido`, `sigungu` 키워드 인자를 지원한다.
기존 호출처럼 상호명만 전달하면 정제된 상호명으로 검색한다.
마지막 공급자 요청이 실패하면 HTTP 오류를 유지해 기존 쿼터 보류 처리를 거친다.

정제 대상은 괄호 안 내용, `주식회사`, `직영점`, 숫자 호점, `본점`, 특수문자다.
`강남점` 같은 임의의 지점명은 실제 상호명과 구별할 근거가 없으므로 일괄 삭제하지 않는다.
정제 후 빈 상호명은 검색하지 않는다. HTML 제목은 검색 결과에서 그대로 보존하고,
기존 원문 처리 단계에서 정제한다. 날짜는 공급자의 원본 문자열을 보존한다.

API 계약 참고: [네이버 블로그 검색](https://developers.naver.com/docs/serviceapi/search/blog/blog.md),
[카카오 블로그 검색](https://developers.kakao.com/docs/latest/ko/daum-search/dev-guide#search-blog).
