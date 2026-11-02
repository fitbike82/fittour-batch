/** Exact PM dictionaries. Overrides take priority; do not infer new industries. */
export const NON_FOOD_KEYWORDS = [
  '이·미용', '미용', '헤어', '이용원', '이발', '피부', '네일', '가발', '목욕', '대중탕',
  '안경', '콘택트', '렌즈', '보청기', '의료', '약방', '한약',
  '한복', '양복', '정장', '의류', '남성복', '주단', '포목', '침구', '단추', '신발', '제화', '구두',
  '학원', '교육', '서적', '서점', '문구', '인쇄', '출판', '사진관', '스튜디오', '앨범', '사무기기',
  '자동차', '카센타', '정비', '철물', '건재', '방수', '도배', '장판', '타일', '인테리어',
  '농약', '비료', '종묘', '농자재', '모터', '펌프', '전기', '배관', '가전', '음향', '악기',
  '열쇠', '도장', '인장', '가구', '금고', '유리', '목공', '공예', '스포츠', '운동',
  '식육', '정육', '기름', '참기름', '들기름', '방앗간', '곡물', '쌀', '양곡',
  '건어물', '젓갈', '수산물', '생화', '화훼', '꽃집', '슈퍼', '마트', '잡화',
];
export const FOOD_OVERRIDE_KEYWORDS = ['제과', '제빵', '베이커리', '빵', '떡', '카페', '찻집', '커피', '식당', '음식점', '분식'];
export function isFoodBusiness(rawBusiness: string): boolean {
  if (!rawBusiness) return false;
  if (FOOD_OVERRIDE_KEYWORDS.some(keyword => rawBusiness.includes(keyword))) return true;
  if (NON_FOOD_KEYWORDS.some(keyword => rawBusiness.includes(keyword))) return false;
  return true;
}
