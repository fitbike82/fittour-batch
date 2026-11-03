"""Bounded API contract probe: no DB, notifications, or billing changes."""
import json
import time
from pathlib import Path
import requests
from dotenv import load_dotenv
from gemini_key_pool import discover_keys
from pipeline_task2 import MODEL, SCHEMA, json_response_schema, api_error_details, setup_masking
from place_taxonomy import model_schema

def main():
    load_dotenv()
    setup_masking()
    key = discover_keys()[0][1]
    base = {'contents': [{'role': 'user', 'parts': [{'text': 'Return a JSON object with ok=true.'}]}]}
    schema = json_response_schema(SCHEMA)
    schema['properties'].update(json_response_schema(model_schema({'content_type_id': '12'})))
    schema['required'] += list(model_schema({'content_type_id': '12'}))
    schema['properties']['filter_tags']['items'].pop('enum', None)
    variants = [('review_schema_only', {**base, 'generationConfig': {'responseMimeType': 'application/json',
                    'responseJsonSchema': json_response_schema(SCHEMA)}}),
                ('taxonomy_without_tag_enum', {**base, 'generationConfig': {'responseMimeType': 'application/json', 'responseJsonSchema': schema}})]
    results = []
    for name, payload in variants:
        if results:
            time.sleep(6)
        try:
            r = requests.post(f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent',
                              headers={'x-goog-api-key': key}, json=payload, timeout=45)
            results.append({'variant': name, 'status': r.status_code, 'error': api_error_details(r)})
            if r.status_code == 200:
                results[-1]['model'] = r.json().get('modelVersion')
            print(json.dumps(results[-1]))
            if r.status_code == 429:
                break
        except requests.RequestException as exc:
            results.append({'variant': name, 'error_type': type(exc).__name__})
    path = Path('artifacts/ai-review-recovery/contract-isolation.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2), encoding='utf-8')

if __name__ == '__main__':
    main()
