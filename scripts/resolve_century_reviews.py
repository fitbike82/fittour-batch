"""Audited relaxed re-review of historical review rows, never of unmatched rows."""
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import json
import re

try:
    from import_century_stores import compact, name_key, phones, locality, address_key, update_payload, conditional_update
    from place_taxonomy import classify
except ModuleNotFoundError:
    from scripts.import_century_stores import compact, name_key, phones, locality, address_key, update_payload, conditional_update
    from scripts.place_taxonomy import classify


def clean_title(value):
    return name_key(re.sub(r'본점|직영점', '', str(value or '')))


def numbers(address):
    # Preserve lot suffixes: 873-1 and 873 are different buildings/lots.
    return {re.sub(r'\s+', '', match.group(1)) for match in re.finditer(
        r'(?:로|길|동|리|가|읍|면)\s*(산?\s*\d+(?:-\d+)?)(?!\d|번길)',
        re.sub(r'\([^)]*\)', '', str(address or '')))}


def rule4_pass(similarity, margin):
    return similarity >= .80 and (margin is None or margin >= 5)


def candidate_evidence(record, place):
    source, target = clean_title(record['업체명']), clean_title(place.get('title'))
    region = locality(record, place.get('address') or place.get('addr1'))
    same_name = bool(source and source == target)
    substring = bool(source and target and (source in target or target in source))
    source_phones, target_phones = phones(record['연락처']), phones(place.get('tel'))
    suffix = bool({phone[-4:] for phone in source_phones}.intersection(phone[-4:] for phone in target_phones))
    exact_phone = bool(source_phones.intersection(target_phones))
    similarity = SequenceMatcher(None, source, target).ratio()
    overlap = SequenceMatcher(None, source, target).find_longest_match().size
    shared_numbers = numbers(record['기본주소']).intersection(numbers(place.get('address') or place.get('addr1')))
    same_address = bool(address_key(record['기본주소']) and
                        address_key(record['기본주소']) == address_key(place.get('address') or place.get('addr1')))
    score = (40 if same_name else int(similarity * 20)) + (20 if region else 0) + (20 if exact_phone else 0) + (20 if same_address else 0)
    rules = []
    if region and same_name:
        rules.append('rule_1_core_name_region')
    if region and substring and suffix:
        rules.append('rule_2_substring_phone_suffix')
    if region and overlap >= 3 and shared_numbers:
        rules.append('rule_3_name_building_number')
    return {'content_id': place['content_id'], 'title': place.get('title'),
            'address': place.get('address') or place.get('addr1'), 'score': score,
            'similarity': round(similarity, 6), 'region_match': region,
            'phone_suffix_match': suffix, 'core_overlap_length': overlap,
            'shared_building_numbers': sorted(shared_numbers), 'rules': rules}


def resolve_records(original, records, places):
    by_id = {str(place['content_id']): place for place in places if place.get('is_active') is True}
    records_by_id = {record['연번']: record for record in records}
    by_core, by_trigram = defaultdict(set), defaultdict(set)
    for content_id, place in by_id.items():
        key = clean_title(place.get('title'))
        by_core[key].add(content_id)
        for index in range(len(key) - 2):
            by_trigram[key[index:index + 3]].add(content_id)
    reserved = {str(row['content_id']) for row in original['events'] if row['status'] == 'matched' and row.get('content_id')}
    events = []
    for previous in original['events']:
        if previous['status'] != 'review':
            continue
        record = records_by_id[previous['csv_id']]
        key = clean_title(record['업체명'])
        pool = {str(row['content_id']) for row in previous.get('candidates', [])}
        pool.update(by_core.get(key, ()))
        related = set()
        for index in range(len(key) - 2):
            related.update(by_trigram.get(key[index:index + 3], ()))
        pool.update(content_id for content_id in related
                    if locality(record, by_id[content_id].get('address') or by_id[content_id].get('addr1')))
        candidates = [candidate_evidence(record, by_id[content_id]) for content_id in sorted(pool) if content_id in by_id]
        candidates.sort(key=lambda row: (-row['score'], str(row['content_id'])))
        if candidates:
            best = candidates[0]
            gap = best['score'] - candidates[1]['score'] if len(candidates) > 1 else None
            if rule4_pass(best['similarity'], gap):
                best['rules'].append('rule_4_similarity_margin')
            acceptable = [candidate for candidate in candidates if candidate['rules']]
        else:
            acceptable, gap = [], None
        selected = acceptable[0] if acceptable else None
        reason = 'no_relaxed_rule'
        if selected and len(acceptable) > 1 and selected['score'] == acceptable[1]['score']:
            selected, reason = None, 'ambiguous_equal_candidates'
        if selected and selected['content_id'] in reserved:
            selected, reason = None, 'already_matched_to_other_csv'
        if selected:
            current = by_id[selected['content_id']]
            info = current.get('century_store_info') or {}
            if current.get('is_century_store') and str(info.get('csv_id', '')) != record['연번']:
                selected, reason = None, 'already_certified_for_other_source'
        if selected and str(by_id[selected['content_id']].get('content_type_id')) != '39':
            selected, reason = None, 'non_food_category_requires_review'
        if selected:
            source = by_id[selected['content_id']].get('taxonomy_source') or ''
            if source and not source.startswith(('century_', 'gemini:')):
                selected, reason = None, 'curated_taxonomy_requires_review'
        events.append({'csv_id': record['연번'], 'csv_name': record['업체명'],
                       'status': 'accepted' if selected else 'rejected',
                       'content_id': selected['content_id'] if selected else None,
                       'reason': selected['rules'][0] if selected else reason,
                       'matched_rules': selected['rules'] if selected else [],
                       'region_match': selected['region_match'] if selected else None,
                       'score_margin': gap, 'candidates': candidates[:5]})
    counts = Counter(event['content_id'] for event in events if event['status'] == 'accepted')
    for event in events:
        if event['status'] == 'accepted' and counts[event['content_id']] > 1:
            event.update(status='rejected', content_id=None, reason='multiple_csv_rows_for_place', matched_rules=[])
    return events


def merge_unmatched(path, original, events, records):
    records_by_id = {record['연번']: record for record in records}
    if path.exists():
        existing = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(existing, dict):
            existing = existing.get('records', [])
        if not isinstance(existing, list):
            raise ValueError('Unsupported unmatched queue format')
    else:
        existing = []
    queue = {}
    for row in existing:
        identity = str(row.get('csv_id') or row.get('연번') or '')
        if not identity:
            raise ValueError('Unmatched queue row lacks CSV identity')
        queue[identity] = row
    for event in original['events']:
        if event['status'] == 'unmatched':
            queue[event['csv_id']] = {'csv_id': event['csv_id'], 'source_record': records_by_id[event['csv_id']],
                                      'reason': 'initial_unmatched', 'status': 'pending_seed'}
    for event in events:
        if (event.get('committed') and event.get('verified') is not False) or event.get('already_applied'):
            queue.pop(event['csv_id'], None)
        else:
            queue[event['csv_id']] = {'csv_id': event['csv_id'], 'source_record': records_by_id[event['csv_id']],
                                      'reason': event.get('write_error') or event['reason'],
                                      'status': 'pending_match' if event['status'] == 'rejected' else 'pending_apply',
                                      'candidates': event['candidates']}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps({'source_date': '2022-07-13', 'records': list(queue.values())},
                                     ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    return len(queue)


def resolve_run(args, db, records, digest, places):
    original = json.loads(args.review_input.read_text(encoding='utf-8'))
    if original.get('source_sha256') != digest:
        raise ValueError('Review report and CSV source hashes differ')
    events = resolve_records(original, records, places)
    by_id = {str(place['content_id']): place for place in places}
    by_csv = {record['연번']: record for record in records}
    baseline = db.table('places').select('content_id', count='exact', head=True).eq('is_century_store', True).execute().count
    if baseline is None:
        raise ValueError('Century count unavailable')
    report = {'mode': 'apply' if args.apply else 'dry_run', 'source_sha256': digest,
              'review_total': len(events), 'accepted': sum(event['status'] == 'accepted' for event in events),
              'rejected': sum(event['status'] == 'rejected' for event in events),
              'baseline_db_total': baseline, 'applied': 0, 'already_applied': 0, 'conflicts': 0, 'errors': 0,
              'rule_counts': dict(Counter(event['reason'] for event in events if event['status'] == 'accepted')),
              'accepted_without_region_match': sum(event['status'] == 'accepted' and not event['region_match'] for event in events),
              'events': events}
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    if args.apply:
        # Validate the existing queue before writing any DB record.
        if args.unmatched.exists():
            queue = json.loads(args.unmatched.read_text(encoding='utf-8-sig'))
            if not isinstance(queue, list) and not (isinstance(queue, dict) and isinstance(queue.get('records'), list)):
                raise ValueError('Unsupported unmatched queue format')
        for event in events:
            if event['status'] != 'accepted':
                continue
            place, record = by_id[event['content_id']], by_csv[event['csv_id']]
            info = place.get('century_store_info') or {}
            if place.get('is_century_store') and info.get('match_rule') == 'relaxed_review_pass' and str(info.get('csv_id')) == event['csv_id']:
                event['already_applied'] = True
                report['already_applied'] += 1
                continue
            payload = update_payload(record, place, digest, event)
            payload.update(classify(place, record['주요사업']), taxonomy_source='century_seed')
            payload['filter_tags'] = sorted(set(place.get('filter_tags') or []).union(payload['filter_tags'], ['백년가게']))
            payload['century_store_info'].update(match_rule='relaxed_review_pass',
                matched_rules=event['matched_rules'], region_match=event['region_match'])
            event['before'] = {field: place.get(field) for field in payload}
            event['patch'] = payload
            try:
                event['committed'] = conditional_update(db, place, payload)
                report['applied' if event['committed'] else 'conflicts'] += 1
            except Exception as exc:
                event['write_error'] = str(getattr(exc, 'code', '') or type(exc).__name__)
                report['errors'] += 1
            save()
        committed = [event for event in events if event.get('committed')]
        report['verified'] = 0
        for offset in range(0, len(committed), 100):
            batch = committed[offset:offset + 100]
            rows = db.table('places').select('content_id,is_century_store,century_store_info,category_l1,category_l2,filter_tags,taxonomy_source') \
                .in_('content_id', [event['content_id'] for event in batch]).execute().data or []
            actual = {str(row['content_id']): row for row in rows}
            for event in batch:
                row = actual.get(event['content_id'], {})
                event['verified'] = all(row.get(field) == value for field, value in event['patch'].items())
                if event['verified']:
                    report['verified'] += 1
                else:
                    report['errors'] += 1
                    event['write_error'] = 'read_back_mismatch'
        report['unmatched_queue_count'] = merge_unmatched(args.unmatched, original, events, records)
    report['db_total_expected'] = baseline + report['applied']
    if args.apply:
        report['db_total_actual'] = db.table('places').select('content_id', count='exact', head=True).eq('is_century_store', True).execute().count
        if report['db_total_actual'] != report['db_total_expected']:
            report['errors'] += 1
            report['verification_error'] = 'century_total_mismatch'
    save()
    print(f"검토 대상 {len(events)}건 중 추가 승인: {report['accepted']}건 (목표: 100건 이상)")
    print(f"최종 기각(미매칭 이관): {report['rejected']}건")
    print(f"DB 누적 백년가게 반영 총합: 기존 {baseline}건 + 신규 {report['applied']}건 = 총 {report['db_total_expected']}건")
    print(json.dumps({key: value for key, value in report.items() if key != 'events'}, ensure_ascii=False, indent=2))
    return 1 if report['conflicts'] or report['errors'] else 0
