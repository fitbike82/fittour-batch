"""Python adapter for src/lib/taxonomy.ts; regenerate JSON with taxonomy:sync."""
import json
from pathlib import Path
import re
import unicodedata

CONFIG = json.loads(Path(__file__).with_suffix('.json').read_text(encoding='utf-8'))
FIELDS = ('category_l1', 'category_l2', 'filter_tags')


def normalize_tag(raw):
    if not isinstance(raw, str):
        return None
    token = re.sub(r'[()（）]', '', unicodedata.normalize('NFKC', raw).strip())
    token = re.sub(r'(?:전문점|전문|판매|도소매|소매)$', '', token).strip()
    if not (2 <= len(token) <= 50 or token in CONFIG['single_character_tags']) or token in CONFIG['stop_words']:
        return None
    return CONFIG['aliases'].get(token, token)


def extract_filter_tags(raw):
    tags = [normalize_tag(token) for token in re.split(r'[,/·&|\s()（）]+', raw or '')]
    return list(dict.fromkeys(tag for tag in tags if tag and tag not in CONFIG['reserved_tags']))


def resolve_food_category(raw):
    text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', raw or ''))
    for rule in CONFIG['food_rules']:
        if re.search(rule['pattern'], text):
            return {'l1': rule['l1'], 'l2': rule['l2']}
    return {'l1': '음식점', 'l2': None}


def classify(place, business=''):
    category = CONFIG['categories'].get(str(place.get('content_type_id')))
    if not category:
        return None
    # Business descriptions take precedence over possibly decorative store names.
    resolved = {'l1': category['l1'], 'l2': None}
    if str(place.get('content_type_id')) == '39':
        resolved = resolve_food_category(business)
        if resolved['l2'] is None:
            resolved = resolve_food_category(str(place.get('title') or ''))
    text = unicodedata.normalize('NFKC', business + ' ' + str(place.get('title') or ''))
    tags = set(extract_filter_tags(business)).intersection(CONFIG['tags'])
    for term in CONFIG['tags'] + list(CONFIG['aliases']):
        if len(term) >= 2 and term in text:
            tags.add(CONFIG['aliases'].get(term, term))
    return {'category_l1': resolved['l1'], 'category_l2': resolved['l2'],
            'filter_tags': sorted(tags.intersection(CONFIG['tags']))[:12]}


def validate_taxonomy(data, place):
    category = CONFIG['categories'].get(str(place.get('content_type_id')))
    if not category or not isinstance(data, dict):
        raise ValueError('Invalid taxonomy L1')
    l1, l2 = data.get('category_l1'), data.get('category_l2')
    if l2 is not None and l2 not in category['l2']:
        raise ValueError('Invalid taxonomy L2')
    expected = CONFIG['dessert_l1'] if str(place.get('content_type_id')) == '39' and l2 in CONFIG['dessert_l2'] else category['l1']
    if l1 != expected:
        raise ValueError('Invalid taxonomy L1')
    tags = data.get('filter_tags')
    if not isinstance(tags, list) or len(tags) > 12 or any(not isinstance(tag, str) for tag in tags):
        raise ValueError('Invalid taxonomy tags')
    tags = [CONFIG['aliases'].get(tag, tag) for tag in tags]
    if any(tag not in CONFIG['tags'] for tag in tags):
        raise ValueError('Invalid taxonomy tags')
    return {'category_l1': l1, 'category_l2': l2, 'filter_tags': sorted(set(tags))}


def model_schema(place):
    category = CONFIG['categories'].get(str(place.get('content_type_id')))
    if not category:
        return {}
    l1_values = [category['l1']] + ([CONFIG['dessert_l1']] if str(place.get('content_type_id')) == '39' else [])
    l2_schema = {'type': 'STRING', 'nullable': True}
    if category['l2']:
        l2_schema['enum'] = category['l2']
    else:
        l2_schema['description'] = '미정의 중분류이므로 반드시 null'
    return {'category_l1': {'type': 'STRING', 'enum': l1_values}, 'category_l2': l2_schema,
            'filter_tags': {'type': 'ARRAY', 'items': {'type': 'STRING', 'enum': CONFIG['tags']}, 'maxItems': 12}}


def model_instructions(place):
    category = CONFIG['categories'].get(str(place.get('content_type_id')))
    if not category:
        return ''
    return ('\nPM v2 분류 기준을 따른다. cat1~cat3는 사용하지 않는다. category_l1은 ' + category['l1']
            + '이다. 단, 음식점(39)의 베이커리/빵, 떡/전통명과, 카페/음료 L2는 L1을 카페/디저트로 쓴다. '
            'category_l2는 제공 enum에서 대표 하나만 선택하고 근거가 부족하거나 중분류 미정의이면 null로 쓴다. '
            '상호보다 제공된 주요사업과 메뉴를 우선한다. filter_tags는 허용 목록의 근거 있는 업종·메뉴만 '
            '최대 12개 선택한다. 백년가게 지정이나 시설·편의성을 추정하지 않는다.')
