import subprocess
import unittest
from unittest.mock import Mock, patch

from scripts.skillsbench.hf_request_retries import wrap_snapshot_download, bootstrap_command


class HuggingFacePreparationRetryTests(unittest.TestCase):
    def test_transport_retry_preserves_snapshot_arguments_and_cache_and_has_no_attempt_cap(self):
        original = Mock(side_effect=[RuntimeError('Network error: Request middleware error: error sending request for url')] * 7 + ['snapshot'])
        call = wrap_snapshot_download(original)
        with patch('scripts.skillsbench.hf_request_retries.time.sleep'):
            self.assertEqual(call(repo_id='public/repo', revision='fixed-commit', local_files_only=False), 'snapshot')
        self.assertEqual(original.call_count, 8)
        self.assertTrue(all(c.kwargs == original.call_args.kwargs for c in original.call_args_list))

    def test_authentication_and_certificate_failures_are_not_retried(self):
        for message in ('403 Forbidden', 'certificate verify failed'):
            original = Mock(side_effect=RuntimeError(message))
            with self.assertRaisesRegex(RuntimeError, message):
                wrap_snapshot_download(original)('public/repo', revision='fixed')
            self.assertEqual(original.call_count, 1)

    def test_bootstrap_is_valid_shell_and_scoped_to_preparation(self):
        command = bootstrap_command()
        subprocess.run(['sh', '-n', '-c', command], check=True)
        self.assertIn('HF_HUB_DISABLE_XET=1', command)
        self.assertIn('/tmp/skillpatch-hf-preparation-network', command)
        self.assertNotIn('/usr/local/lib', command)
