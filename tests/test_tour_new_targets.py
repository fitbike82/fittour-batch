import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import sync_tour_enrichment_full as batch


class NewTargetTests(unittest.TestCase):
    def test_query_includes_partial_places_and_paginates_stably(self):
        db = batch.Database('https://example.test', 'key')
        places = [dict(content_id=f'{i:03}', content_type_id='12') for i in range(1, 10)]
        def request(method, table, *, params):
            self.assertEqual(table, 'places')
            self.assertNotIn('place_tour_payloads', params)
            start = params['offset']
            return places[start:start + min(params['limit'], 2)]
        db.request = Mock(side_effect=request)
        self.assertEqual([r['content_id'] for r in db.targets(limit=5)], ['001', '002', '003', '004', '005'])

    def test_report_counts_only_new_ids_written_by_this_execution(self):
        db = Mock()
        db.completed.return_value = {('old', 'KorService2', 'detailCommon2', ''),
                                     ('new', 'KorService2', 'detailCommon2', ''),
                                     ('other-job', 'KorService2', 'detailCommon2', '')}

        def run(argv, report, context):
            context.update(db=db, existing_ids={'old'}, saved_ids={'old', 'new'})
            report.update(processed_count=0, failures=0)
            return 0

        with tempfile.TemporaryDirectory() as directory, patch.object(batch, '_run_batch', side_effect=run), \
                patch.object(batch, 'send_telegram_report') as send, \
                patch.object(batch, 'Path', side_effect=lambda p: Path(directory) / p):
            batch.main([])
            report = send.call_args.args[0]
            self.assertEqual(report['actual_newly_inserted_count'], 1)
            self.assertEqual(report['processed_count'], 0)  # Distinct from fully enriched places.
