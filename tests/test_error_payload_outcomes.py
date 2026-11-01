"""Expose only fixed safe outcome classifications in API errors."""
import unittest

from api_v1 import error_payload
from common import GuideError, RunCancelled


class ErrorPayloadOutcomeTests(unittest.TestCase):
    def test_cancelled_after_remote_success_reports_no_retry(self):
        error = RunCancelled()
        error.remote_outcome = 'completed'
        error.retry_safe = False
        payload = error_payload(error)['error']
        self.assertEqual(payload['code'], 'user_cancelled')
        self.assertEqual(payload['remote_outcome'], 'completed')
        self.assertIs(payload['retry_safe'], False)
        self.assertIs(payload['retryable'], False)

    def test_committed_database_cleanup_failure_reports_no_retry(self):
        error = GuideError('cleanup failed')
        error.code = 'source_store_cleanup_failed'
        error.transaction_outcome = 'committed'
        payload = error_payload(error, status=503)['error']
        self.assertEqual(payload['transaction_outcome'], 'committed')
        self.assertIs(payload['retryable'], False)

    def test_untrusted_outcome_attributes_are_not_reflected(self):
        error = GuideError('failed')
        error.remote_outcome = 'synthetic-private'
        error.transaction_outcome = 'synthetic-private'
        payload = error_payload(error)
        self.assertNotIn('synthetic-private', repr(payload))


if __name__ == '__main__':
    unittest.main()
