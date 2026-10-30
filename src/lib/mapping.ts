import { BatchError } from './budget.js';
import type { Item } from './tour-api.js';
import type { Details } from './store.js';
export const CONTENT_TYPES = [12, 14, 15, 25, 28, 32, 38, 39] as const;
export const text = (value: unknown, max = 100_000): string | null => {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  return Array.from(String(value).replaceAll('\0', '').trim()).slice(0, max).join('') || null;
};
export function date8(value: unknown): string | null {
  const date = text(value);
  if (!date || !/^[0-9]{8}$/.test(date)) return null;
  const iso = `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}`;
  const parsed = new Date(`${iso}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === iso ? date : null;
}
export function sanitizeIntro(raw: Item): Item {
  if (Object.keys(raw).length > 128) throw new BatchError('too_many_intro_fields');
  for (const [key, value] of Object.entries(raw)) {
    if (key.length > 128 || (value !== null && !['string', 'number', 'boolean'].includes(typeof value)) ||
        (typeof value === 'number' && !Number.isFinite(value))) throw new BatchError('invalid_intro_field');
  }
  return Object.fromEntries(Object.entries(raw).map(([key, value]) => [key,
    ['eventstartdate', 'eventenddate'].includes(key) ? date8(value) : typeof value === 'string' ? text(value, 10_000) : value]));
}

// Bound text before serialization; reject oversized structures rather than dropping course stops.
export function sanitizeDetails(details: Details): Details {
  if (details.stops.length > 500) throw new BatchError('too_many_course_stops');
  const clean: Details = {
    overview: text(details.overview, 20_000), intro: sanitizeIntro(details.intro),
    course: details.course ? { title: text(details.course.title, 255),
      distance: text(details.course.distance, 1000), taketime: text(details.course.taketime, 1000),
      schedule: text(details.course.schedule, 10_000) } : null,
    stops: details.stops.map(stop => ({ subnum: stop.subnum, subcontentid: text(stop.subcontentid, 50),
      subname: text(stop.subname, 255), subdetailoverview: text(stop.subdetailoverview, 10_000),
      subdetailimg: text(stop.subdetailimg, 2048) })),
  };
  if (Buffer.byteLength(JSON.stringify(clean), 'utf8') > 512 * 1024) throw new BatchError('detail_payload_too_large');
  return clean;
}
export function mapMaster(raw: Item, expectedCategory: number): Item {
  const id = text(raw.contentid);
  const category = Number(raw.contenttypeid);
  const title = text(raw.title, 255);
  const modified = text(raw.modifiedtime);
  if (!id || !/^[0-9]{1,50}$/.test(id) || !title || category !== expectedCategory ||
      !(CONTENT_TYPES as readonly number[]).includes(category)) throw new BatchError('invalid_master_identity');
  if (!modified || !/^[0-9]{14}$/.test(modified) || !date8(modified.slice(0, 8)) ||
      Number(modified.slice(8, 10)) > 23 || Number(modified.slice(10, 12)) > 59 || Number(modified.slice(12)) > 59) {
    throw new BatchError('invalid_modified_time');
  }
  if (![raw.mapx, raw.mapy].every(value => typeof value === 'string' || typeof value === 'number')) {
    throw new BatchError('invalid_coordinates');
  }
  const x = Number(raw.mapx), y = Number(raw.mapy);
  if (!Number.isFinite(x) || !Number.isFinite(y) || x < 124 || x > 132 || y < 33 || y > 39) {
    throw new BatchError('invalid_coordinates');
  }
  return {
    content_id: id, content_type_id: String(category), title, modified_time: modified,
    addr1: text(raw.addr1, 120), addr2: text(raw.addr2, 120), zipcode: text(raw.zipcode, 20),
    tel: text(raw.tel, 100), first_image: text(raw.firstimage, 2048), first_image2: text(raw.firstimage2, 2048),
    area_code: text(raw.areacode, 10), sigungu_code: text(raw.sigungucode, 10),
    ldong_regn_cd: text(raw.lDongRegnCd, 10), mlevel: text(raw.mlevel, 10),
    mapx: x, mapy: y, is_active: true,
  };
}
export type CourseStop = { subnum: number; subcontentid: string | null; subname: string;
  subdetailoverview: string | null; subdetailimg: string | null };
export function courseStops(raw: Item[]): CourseStop[] {
  const seen = new Set<number>();
  const rows = raw.map(item => {
    const order = text(item.subnum);
    const name = text(item.subname);
    if (!order || !/^[0-9]+$/.test(order) || !Number.isSafeInteger(Number(order)) || !name || seen.has(Number(order))) {
      throw new BatchError('invalid_course_stop');
    }
    seen.add(Number(order));
    const subId = text(item.subcontentid, 50);
    return { subnum: Number(order), subcontentid: subId && /^[0-9]+$/.test(subId) ? subId : null,
      subname: name, subdetailoverview: text(item.subdetailoverview), subdetailimg: text(item.subdetailimg, 2048) };
  });
  // TourAPI may number from zero. Store a contiguous, explicit 1-based order.
  return rows.sort((a, b) => a.subnum - b.subnum).map((row, index) => ({ ...row, subnum: index + 1 }));
}
