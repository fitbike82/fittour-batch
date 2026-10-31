"""Deterministic evidence references and bounded generation diagnostics."""
import re
from datetime import datetime, timezone


def evidence_catalog(reviews):
    catalog = {}
    for review in reviews:
        # Preserve exact source substrings; punctuation splits do not rewrite quotes.
        chunks = re.findall(r'[^.!?。！？\n]+[.!?。！？]?', review['text'])
        for index, chunk in enumerate(chunks):
            chunk = chunk.strip()
            for offset in range(0, len(chunk), 160):
                quote = chunk[offset:offset + 160]
                if len(quote) >= 10:
                    catalog[f"s{review['source_id']}-p{index}-{offset}"] = {
                        'source_id': review['source_id'], 'evidence': quote}
    return catalog


def resolve_points(data, catalog, diagnostics):
    if not isinstance(data, dict):
        raise ValueError('Invalid response object')
    for field in ('pros', 'cons'):
        if not isinstance(data.get(field), list):
            raise ValueError('Invalid ' + field)
        valid = []
        for index, point in enumerate(data[field]):
            if not isinstance(point, dict):
                diagnostics.append({'field': field, 'index': index, 'code': 'invalid_point'})
                continue
            evidence_id = point.get('evidence_id')
            if evidence_id is not None:
                if not isinstance(evidence_id, str) or evidence_id not in catalog:
                    diagnostics.append({'field': field, 'index': index, 'code': 'unknown_evidence_id',
                                        'evidence_id': str(evidence_id)[:80]})
                    continue
                point = {**point, **catalog[evidence_id]}
            if not isinstance(point.get('text'), str) or not point['text'].strip():
                diagnostics.append({'field': field, 'index': index, 'code': 'empty_point_text'})
                continue
            valid.append(point)
        data[field] = valid
    return data


def basic_summary(place, reason):
    """Coverage without invented visitor claims; distinguish from review analysis."""
    address = place.get('address') or place.get('addr1')
    return {'quality_status': 'ready', 'summary_lines': [
        f"장소명: {place.get('title') or '장소명 확인 필요'}",
        f"위치: {address}" if address else '위치 정보는 추가 확인이 필요합니다.',
        '신뢰할 수 있는 방문 후기 분석이 부족하여 기본 장소 정보만 제공합니다.'],
        'pros': [], 'cons': [], 'visit_tips': '방문 전 운영시간과 시설 정보를 확인해 주세요.',
        'analysis_basis': 'basic_info', 'coverage_reason': reason,
        'analyzed_at': datetime.now(timezone.utc).isoformat()}


def quote_diagnostics(data, reviews):
    details=[]
    sources={review['source_id']: review['text'] for review in reviews}
    for field in ('pros', 'cons'):
        for index, point in enumerate(data.get(field, []) if isinstance(data.get(field), list) else []):
            if not isinstance(point, dict):
                continue
            quote=point.get('evidence')
            if not isinstance(quote, str) or len(quote)<10:
                code='missing_quote' if not quote else 'short_quote'
                matches=[]
            else:
                pattern=r'\s+'.join(re.escape(part) for part in quote.split())
                matches=[source_id for source_id, text in sources.items() if re.search(pattern, text)]
                if point.get('source_id') in matches or len(matches)==1:
                    continue
                code='quote_not_found' if not matches else 'ambiguous_source'
            details.append({'field':field,'index':index,'code':code,
                            'source_id':point.get('source_id'), 'evidence':str(quote or '')[:160],
                            'matching_source_ids':matches})
    return details
