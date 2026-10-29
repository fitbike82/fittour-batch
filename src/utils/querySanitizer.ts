/** 상호명의 법인 표기, 괄호 내용, 대표 지점 표기와 특수문자를 제거합니다. */
export function sanitizePlaceTitle(title: string): string {
  let clean = title.normalize('NFKC');
  // Remove nested parentheses/brackets from the inside out.
  let previous: string;
  do {
    previous = clean;
    clean = clean.replace(/\([^()]*\)|\[[^\[\]]*\]/g, ' ');
  } while (clean !== previous);
  return clean
    .replace(/주식회사|직영점|\d+호점|본점/g, ' ')
    .replace(/[^a-zA-Z0-9가-힣\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export type SmartQueryParams = { title: string; sido?: string; sigungu?: string };

/** 지역 + 정제 상호명, 정제 상호명 순서로 중복 없는 검색어를 반환합니다. */
export function buildSmartQueries(params: SmartQueryParams): string[] {
  const cleanTitle = sanitizePlaceTitle(params.title);
  // Never send an empty title or a region-only query to a provider.
  if (!cleanTitle) return [];
  const region = sanitizePlaceTitle(params.sigungu?.trim() || params.sido?.trim() || '')
    .replace(/(?:특별자치도|특별자치시|특별시|광역시|시|군|구|도)$/, '')
    .trim();
  return [...new Set([
    ...(region && !cleanTitle.includes(region) ? [`${region} ${cleanTitle}`] : []),
    cleanTitle,
  ])];
}
