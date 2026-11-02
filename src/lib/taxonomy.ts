/** PM-approved category master. Python consumes its generated JSON export. */
import { NON_FOOD_KEYWORDS, FOOD_OVERRIDE_KEYWORDS } from './century-food-filter.js';
export const TAXONOMY_VERSION = 'v2';
export const CATEGORY_L1_MAP: Record<string, string> = {
  '12': '관광지', '14': '문화시설', '15': '축제/행사', '25': '여행코스',
  '28': '레포츠', '32': '숙박', '38': '쇼핑', '39': '음식점',
};
export const CATEGORY_L2_MASTER = {
  FOOD: ['소고기/구이', '돼지고기/갈비', '곱창/특수부위', '냉면/밀면', '칼국수/국수', '만두/분식',
    '국밥/설렁탕', '해장국/찌개', '보양/추어/어탕', '한정식/정식', '게장/생선구이', '활어회/해물',
    '장어/복어', '닭/오리/백숙', '치킨/통닭', '중식', '일식/돈까스', '베이커리/빵', '떡/전통명과', '카페/음료'],
  NATURE: ['자연/힐링', '역사/유적', '공원/수목원', '테마파크'],
  CULTURE: ['박물관/미술관', '공연/전시', '체험관'],
  LEISURE: ['수상레저', '익스트림/체험', '캠핑/글램핑', '골프/스키'],
  STAY: ['호텔/리조트', '펜션/풀빌라', '한옥스테이', '게스트하우스'],
  COURSE: ['도보길/올레길', '드라이브코스', '자전거길'],
  FESTIVAL: ['계절/꽃축제', '문화/예술축제', '먹거리/특산물축제'],
  // PM baseline did not define shopping. Keep L2 nullable instead of inventing one.
  SHOPPING: [],
} as const;
const typeGroup: Record<string, keyof typeof CATEGORY_L2_MASTER> = {
  '12': 'NATURE', '14': 'CULTURE', '15': 'FESTIVAL', '25': 'COURSE',
  '28': 'LEISURE', '32': 'STAY', '38': 'SHOPPING', '39': 'FOOD',
};
export const DESSERT_L2 = ['베이커리/빵', '떡/전통명과', '카페/음료'] as const;
export const FOOD_L2_RULES: Array<{ l2: string; l1: string; regex: RegExp }> = [
  { l2: '베이커리/빵', l1: '카페/디저트', regex: /제과|제빵|베이커리|단팥빵|바게트|크림빵|빵집/ },
  // '떡' alone must not turn 떡볶이/떡갈비/떡국 into a dessert venue.
  { l2: '떡/전통명과', l1: '카페/디저트', regex: /떡(?!볶|갈비|국|만두)|호두과자|한과|전통과자|꿀빵/ },
  { l2: '카페/음료', l1: '카페/디저트', regex: /카페|까페|커피|찻집|디저트|빙수|로스터리/ },
  { l2: '소고기/구이', l1: '음식점', regex: /한우|소갈비|등심|불고기|육회|차돌박이|소고기/ },
  { l2: '돼지고기/갈비', l1: '음식점', regex: /삼겹살|돼지갈비|목살|오겹살|갈매기|돼지고기|뒷고기/ },
  { l2: '곱창/특수부위', l1: '음식점', regex: /곱창|막창|대창|특수부위/ },
  { l2: '냉면/밀면', l1: '음식점', regex: /냉면|밀면/ },
  { l2: '칼국수/국수', l1: '음식점', regex: /칼국수|국수|막국수|콩국수|쫄면|메밀/ },
  { l2: '만두/분식', l1: '음식점', regex: /만두|수제비|떡볶이|김밥|분식|찐빵/ },
  { l2: '국밥/설렁탕', l1: '음식점', regex: /설렁탕|곰탕|순대국|국밥|육개장/ },
  { l2: '해장국/찌개', l1: '음식점', regex: /해장국|감자탕|찌개|순두부|전골|두부/ },
  { l2: '보양/추어/어탕', l1: '음식점', regex: /추어탕|어탕|어죽|장어탕|흑염소|염소|올갱이|다슬기/ },
  { l2: '한정식/정식', l1: '음식점', regex: /한정식|정식|보리밥|돌솥|산채|백반|쌈밥/ },
  { l2: '게장/생선구이', l1: '음식점', regex: /게장|생선구이|굴비|갈치조림|갈치구이|고등어/ },
  { l2: '활어회/해물', l1: '음식점', regex: /횟집|활어회|모듬회|모둠회|물회|해물탕|해물찜|아구찜|조개/ },
  { l2: '장어/복어', l1: '음식점', regex: /장어|복어|복집|꼼장어/ },
  { l2: '닭/오리/백숙', l1: '음식점', regex: /백숙|삼계탕|오리|닭볶음탕|찜닭|닭갈비/ },
  { l2: '치킨/통닭', l1: '음식점', regex: /치킨|통닭|닭강정/ },
  { l2: '중식', l1: '음식점', regex: /짜장|자장|짬뽕|탕수육|중화|중식/ },
  { l2: '일식/돈까스', l1: '음식점', regex: /돈가스|돈까스|일식|초밥|스시|우동|소바|가츠/ },
];

export const TAG_NORMALIZATION_MAP: Record<string, string> = {
  돈가스: '돈까스', 돈카츠: '돈까스', 돈카쓰: '돈까스', 스시: '초밥', 까페: '카페',
  제과: '베이커리', 제빵: '베이커리', 제과제빵: '베이커리', 빵집: '베이커리',
  자장: '짜장면', 짜장: '짜장면', 자장면: '짜장면', 순대국: '순대국밥',
  모듬회: '모둠회', 아귀찜: '아구찜', 후라이드치킨: '치킨', 프라이드치킨: '치킨',
};
export const TAG_STOP_WORDS = ['등', '전문점', '전문', '일반', '판매', '소매', '요리', '도소매', '식당'];
const stopWords = new Set(TAG_STOP_WORDS);
export const RESERVED_TAGS = ['백년가게'];
export const SINGLE_CHARACTER_TAGS = ['떡', '회', '차'];
export function normalizeFilterTag(raw: string): string | null {
  const token = raw.normalize('NFKC').trim().replace(/[()（）]/g, '')
    .replace(/(?:전문점|전문|판매|도소매|소매)$/u, '').trim();
  if ((token.length < 2 && !SINGLE_CHARACTER_TAGS.includes(token)) || token.length > 50 || stopWords.has(token)) return null;
  return TAG_NORMALIZATION_MAP[token] ?? token;
}
export function extractFilterTags(rawBusiness: string): string[] {
  if (!rawBusiness) return [];
  return [...new Set(rawBusiness.split(/[,/·&|\s()（）]+/u).map(normalizeFilterTag)
    .filter((tag): tag is string => tag !== null && !RESERVED_TAGS.includes(tag)))];
}
export function resolveFoodCategory(rawText: string): { l1: string; l2: string | null } {
  const text = (rawText ?? '').normalize('NFKC').replace(/\s+/gu, '');
  for (const rule of FOOD_L2_RULES) if (rule.regex.test(text)) return { l1: rule.l1, l2: rule.l2 };
  return { l1: '음식점', l2: null };
}

const rawTags = ['갈비', '냉면', '밀면', '국수', '막국수', '칼국수', '콩국수', '쫄면', '메밀',
  '만두', '수제비', '떡볶이', '김밥', '분식', '찐빵', '소고기', '한우', '소갈비', '등심', '불고기', '육회',
  '차돌박이', '삼겹살', '돼지갈비', '목살', '오겹살', '갈매기살', '돼지고기', '뒷고기', '곱창', '막창', '대창',
  '국밥', '설렁탕', '곰탕', '순대국밥', '육개장', '해장국', '감자탕', '찌개', '순두부', '전골', '두부',
  '추어탕', '어탕', '어죽', '장어탕', '흑염소', '염소', '올갱이', '다슬기', '한정식', '정식', '보리밥',
  '돌솥', '산채', '백반', '쌈밥', '게장', '생선구이', '굴비', '갈치조림', '갈치구이', '고등어', '회',
  '횟집', '활어회', '모둠회', '물회', '해물탕', '해물찜', '아구찜', '조개', '해산물', '장어', '복어', '꼼장어',
  '백숙', '삼계탕', '오리', '닭볶음탕', '찜닭', '닭갈비', '치킨', '통닭', '닭강정', '중식', '한식', '일식',
  '짜장면', '짬뽕', '탕수육', '중화요리', '돈까스', '초밥', '우동', '소바', '가츠', '카페', '커피', '차',
  '베이커리', '단팥빵', '바게트', '크림빵', '떡', '찹쌀떡', '호두과자', '한과', '전통과자', '꿀빵', '디저트',
  '빙수', '로스터리', '파스타', '피자', '양식', '맥주', '막걸리', '와인', '주점',
  '자연', '역사', '공원', '수목원', '테마파크', '박물관', '미술관', '공연', '전시', '체험',
  '수상레저', '캠핑', '글램핑', '골프', '스키', '호텔', '리조트', '펜션', '풀빌라', '한옥',
  '게스트하우스', '도보', '올레길', '드라이브', '자전거', '꽃축제', '문화축제', '특산물', '전통시장', '공예', '의류'];
export const FILTER_TAG_MASTER = [...new Set(rawTags.map(word => TAG_NORMALIZATION_MAP[word] ?? word))].sort();

/** JSON-safe generated bridge; never edit the Python copy by hand. */
export function exportTaxonomy() {
  return {
    version: TAXONOMY_VERSION,
    categories: Object.fromEntries(Object.entries(CATEGORY_L1_MAP).map(([type, l1]) => [type, {
      l1, l2: [...CATEGORY_L2_MASTER[typeGroup[type]]],
    }])),
    dessert_l1: '카페/디저트', dessert_l2: [...DESSERT_L2],
    food_rules: FOOD_L2_RULES.map(rule => ({ l1: rule.l1, l2: rule.l2, pattern: rule.regex.source })),
    aliases: TAG_NORMALIZATION_MAP, stop_words: TAG_STOP_WORDS,
    tags: FILTER_TAG_MASTER, reserved_tags: RESERVED_TAGS, single_character_tags: SINGLE_CHARACTER_TAGS,
    non_food_keywords: NON_FOOD_KEYWORDS, food_override_keywords: FOOD_OVERRIDE_KEYWORDS,
  };
}
