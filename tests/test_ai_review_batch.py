import unittest
from unittest.mock import Mock, patch
from batch_ai_summary_cron import persist, resume_saved, search_reviews, run, pending_places


class BatchReviewTests(unittest.TestCase):
    def place(self):
        return {'content_id': '1', 'title': '장소', 'modified_time': '20261015000000'}

    def test_three_searches_only_when_fewer_than_two_valid_reviews(self):
        session = Mock()
        session.get.return_value.json.side_effect = [
            {'documents': []}, {'documents': [{'url': 'a'}]},
            {'documents': [{'url': 'a'}, {'url': 'b'}]}]
        with patch('batch_ai_summary_cron.read_article', return_value='본문'):
            reviews = search_reviews(session, 'key', '장소')
        self.assertEqual(len(reviews), 2)
        self.assertEqual([c.kwargs['params']['query'] for c in session.get.call_args_list],
                         ['장소 내돈내산', '장소 솔직 후기', '장소 방문 후기'])

    def test_priority_and_limit(self):
        db = Mock()
        db.rpc.return_value.execute.return_value.data = [{'content_id': 'tour', 'attempt_number': 1}]
        self.assertEqual(pending_places(db, 120)[0]['content_id'], 'tour')
        db.rpc.assert_called_once_with('claim_ai_review_work', {'batch_limit': 120})

    def test_insufficient_is_saved_before_revision_checked_queue_update(self):
        db = Mock()
        persist(db, self.place(), None, [], 'insufficient', 'model', 'insufficient_evidence')
        payload = db.table.return_value.upsert.call_args.args[0]
        self.assertEqual(payload['quality_status'], 'insufficient')
        self.assertEqual(payload['source_reviews']['input_modified_time'], self.place()['modified_time'])
        db.table.return_value.update.assert_called_once_with({'needs_ai_summary': False})

    def test_partial_commit_resumes_without_generation(self):
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {'quality_status': 'insufficient', 'source_reviews': {'input_modified_time': self.place()['modified_time']}}]
        with patch('batch_ai_summary_cron.finish_queue', return_value=True):
            self.assertEqual(resume_saved(db, self.place()), 'insufficient')

    def test_quality_exclusion_drains_queue_but_api_failure_stays_pending(self):
        db, generator, sleep = Mock(), Mock(), Mock()
        generator.summarize.side_effect = [ValueError('insufficient_evidence'), RuntimeError('API down')]
        with patch('batch_ai_summary_cron.pending_places', return_value=[self.place(), self.place()]), \
             patch('batch_ai_summary_cron.resume_saved', return_value=None), \
             patch('batch_ai_summary_cron.search_reviews', return_value=[]), \
             patch('batch_ai_summary_cron.persist', return_value=True) as save:
            counts = run(db, Mock(), generator, 'key', 2, sleep=sleep)
        self.assertEqual((counts['processed'], counts['excluded'], counts['errors']), (1, 1, 1))
        self.assertEqual(save.call_count, 1)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 5])

    def test_throttling_applies_to_fallback_and_retries(self):
        from pipeline_task2 import Gemini
        current = [0]
        waits = []
        def sleep(seconds):
            current[0] += seconds
            waits.append(seconds)
        model = Gemini(Mock(), 'key', sleep=sleep, clock=lambda: current[0], min_interval=5, max_rpm=12)
        for _ in range(13):
            model.wait_for_quota()
        self.assertEqual(current[0], 60)
        self.assertEqual(waits, [5]*12)


if __name__ == '__main__':
    unittest.main()
