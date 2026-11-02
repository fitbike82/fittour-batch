import { parseArgs } from 'node:util';
import { config } from 'dotenv';
import { database } from '../src/lib/store.js';
import { BatchError } from '../src/lib/budget.js';
import { isMain } from '../src/lib/cli.js';

export type TaxonomyRow = {
  content_id: string; content_type_id: string | null; title: string | null;
  cat1: string | null; cat2: string | null; cat3: string | null; is_active: boolean | null;
};
const labels: Record<string, string> = {
  '12': '관광지', '14': '문화시설', '15': '축제', '25': '여행코스',
  '28': '레포츠', '32': '숙박', '38': '쇼핑', '39': '음식점',
};
const missing = '(미등록)';
const code = (value: string | null) => value?.trim() || missing;
const cell = (value: unknown) => String(value ?? '').replaceAll('|', '\\|').replace(/[\r\n]+/g, ' ');
const keywords = [
  ['카페', /카페|까페/], ['커피', /커피|coffee/], ['베이커리', /베이커리|bakery/],
  ...['냉면', '갈비', '국밥', '곰탕', '설렁탕', '추어탕', '순대', '감자탕', '해장국',
    '삼겹살', '불고기', '한우', '족발', '보쌈', '막국수', '칼국수', '국수', '우동', '라멘',
    '쌀국수', '치킨', '피자', '햄버거', '버거', '스테이크', '파스타', '초밥', '스시',
    '횟집', '해물', '해산물', '장어', '게장', '대게', '조개', '생선', '굴', '낙지', '문어',
    '오리', '닭갈비', '삼계탕', '백숙', '두부', '청국장', '된장', '한정식', '백반',
    '비빔밥', '돌솥', '죽', '분식', '김밥', '떡볶이', '만두', '중화', '짜장', '짬뽕',
    '마라', '양꼬치', '샤브', '전골', '찌개', '덮밥', '카레', '뷔페', '브런치',
    '디저트', '케이크', '도넛', '빙수', '아이스크림', '떡', '빵집', '제과', '찻집',
    '맥주', '막걸리', '와인', '포차', '주점'].map(word => [word, new RegExp(word)] as const),
  ['돈가스', /돈가스|돈까스/],
] as const;

function table(headers: string[], rows: unknown[][]): string {
  return [headers, headers.map(() => '---'), ...rows].map(row => `| ${row.map(cell).join(' | ')} |`).join('\n');
}

function distribution(rows: TaxonomyRow[], field: 'cat1' | 'cat2' | 'cat3') {
  const groups = new Map<string, { count: number; samples: string[] }>();
  for (const row of rows) {
    const key = code(row[field]);
    const group = groups.get(key) ?? { count: 0, samples: [] };
    group.count++;
    const sample = row.title?.trim();
    if (sample && group.samples.length < 5 && !group.samples.includes(sample)) group.samples.push(sample);
    groups.set(key, group);
  }
  return [...groups].sort(([a, x], [b, y]) => y.count - x.count || a.localeCompare(b));
}

export function taxonomyReport(rows: TaxonomyRow[]): string {
  const byType = new Map<string, TaxonomyRow[]>();
  for (const row of rows) {
    const key = code(row.content_type_id);
    const group = byType.get(key) ?? [];
    group.push(row); byType.set(key, group);
  }
  const sections = [
    '# places 원천 카테고리 분포',
    `조회 행: ${rows.length.toLocaleString('en-US')}건 (활성·비활성 전체)`,
    '읽기 전용 키셋 페이지 조회입니다. 조회 중 변경이 있으면 단일 시점 스냅샷과 차이가 날 수 있습니다.',
    '## content_type_id별 건수',
    table(['유형', '명칭', '전체', '활성', '비활성', '상태 미등록'],
      [...new Set([...Object.keys(labels), ...byType.keys()])].sort().map(type => {
        const group = byType.get(type) ?? [];
        return [type, labels[type] ?? '기타', group.length, group.filter(r => r.is_active === true).length,
          group.filter(r => r.is_active === false).length, group.filter(r => r.is_active == null).length];
      })),
  ];
  for (const field of ['cat1', 'cat2', 'cat3'] as const) {
    const groups = distribution(rows, field);
    sections.push(`## 전체 ${field} 분포 (고유 등록 코드 ${groups.filter(([k]) => k !== missing).length}개)`,
      table([field, '건수'], groups.map(([key, group]) => [key, group.count])));
  }
  for (const type of ['25', '15', '39']) {
    const subset = byType.get(type) ?? [];
    for (const field of ['cat2', 'cat3'] as const) {
      const groups = distribution(subset, field);
      sections.push(`## ${labels[type]}(${type}) ${field} 분포 (고유 등록 코드 ${groups.filter(([k]) => k !== missing).length}개)`,
        '제목 샘플은 content_id 조회 순서의 서로 다른 제목 최대 5개입니다.',
        table([field, '건수', '제목 샘플'], groups.map(([key, group]) => [key, group.count, group.samples.join(' / ')])));
    }
    const pairs = new Map<string, number>();
    for (const row of subset) {
      const key = `${code(row.cat2)} / ${code(row.cat3)}`;
      pairs.set(key, (pairs.get(key) ?? 0) + 1);
    }
    sections.push(`### ${labels[type]} cat2 / cat3 조합`,
      table(['cat2 / cat3', '건수'], [...pairs].sort(([a, x], [b, y]) => y - x || a.localeCompare(b))));
  }
  const restaurants = byType.get('39') ?? [];
  const frequencies = keywords.map(([keyword, pattern]) => ({ keyword,
    count: restaurants.filter(row => pattern.test((row.title ?? '').normalize('NFKC').replace(/\s+/g, '').toLowerCase())).length,
  })).filter(r => r.count > 0).sort((a, b) => b.count - a.count || a.keyword.localeCompare(b.keyword)).slice(0, 20);
  sections.push('## 음식점 제목 주요 키워드 상위 20개',
    '정의된 업종·메뉴 키워드가 제목에 포함된 장소 수입니다. 한 장소는 여러 키워드에 집계될 수 있으며 자유 형태소 분석 결과가 아닙니다.',
    `키워드 사전: ${keywords.map(([word]) => word).join(', ')}`,
    table(['순위', '키워드', '장소 수'], frequencies.map((row, i) => [i + 1, row.keyword, row.count])));
  return sections.join('\n\n');
}

async function main() {
  const { values } = parseArgs({ options: { 'env-file': { type: 'string', default: '.env' } } });
  const loaded = config({ path: values['env-file'], quiet: true });
  if (loaded.error && values['env-file'] !== '.env') throw new BatchError('env_file_unavailable');
  const url = process.env.SUPABASE_URL ?? process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new BatchError('missing_server_configuration');
  const db = database(url, key);
  const rows: TaxonomyRow[] = [];
  let after: string | undefined;
  while (true) {
    let page: TaxonomyRow[] = [];
    for (let attempt = 0; ; attempt++) {
      let query = db.from('places').select('content_id,content_type_id,title,cat1,cat2,cat3,is_active')
        .order('content_id', { ascending: true }).limit(500);
      if (after !== undefined) query = query.gt('content_id', after);
      const { data, error, status } = await query;
      if (!error) { page = data as TaxonomyRow[]; break; }
      const retryable = ['57014', '40001', '40P01'].includes(error.code) || status === 0 || status >= 500;
      if (!retryable || attempt >= 3) throw new BatchError(`taxonomy_read_${/^[A-Z0-9]{5,12}$/.test(error.code) ? error.code : 'unavailable'}`);
      await new Promise(resolve => setTimeout(resolve, 500 * 2 ** attempt));
    }
    if (!page.length) break;
    const cursor = page.at(-1)!.content_id;
    if (cursor === after) throw new BatchError('taxonomy_cursor_not_advanced');
    rows.push(...page); after = cursor;
  }
  console.log(taxonomyReport(rows));
}

if (isMain(import.meta.url)) {
  await main().catch(error => {
    console.error(error instanceof BatchError ? error.code : 'taxonomy_analysis_failed');
    process.exitCode = 1;
  });
}
