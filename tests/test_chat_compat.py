"""Validate optimizer request schemas without making API calls."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from method_input_adapters.chat_compat import ChatClient, chat_parameters, skillopt_chat_parameters


class ChatParameterTests(unittest.TestCase):
    def test_gpt55_rewrites_token_limit_and_omits_sampling_without_mutating_input(self):
        original = {'model': 'gpt-5.5', 'messages': [{'role': 'user', 'content': 'test'}],
                    'max_tokens': 8192, 'temperature': 0, 'top_p': 1, 'timeout': 45}
        value = chat_parameters(original)
        self.assertEqual(value['max_completion_tokens'], 8192)
        self.assertNotIn('max_tokens', value)
        self.assertNotIn('temperature', value)
        self.assertNotIn('top_p', value)
        self.assertEqual(value['messages'], original['messages'])
        self.assertEqual(original['temperature'], 0)
        self.assertEqual(value['timeout'], 45)

    def test_ds_requests_are_preserved(self):
        original = {'model': 'deepseek-v4.1-flash', 'max_tokens': 4096, 'temperature': 0}
        self.assertEqual(chat_parameters(original), original)

    def test_conflicting_limits_are_rejected(self):
        with self.assertRaises(ValueError):
            chat_parameters({'model': 'gpt-5.5', 'max_tokens': 10, 'max_completion_tokens': 20})

    def test_wrapped_sdk_client_forwards_response_and_close(self):
        create = Mock(return_value=object())
        client = Mock(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        wrapped = ChatClient(client)
        result = wrapped.chat.completions.create(model='gpt-5.5', max_tokens=20, temperature=0)
        self.assertIs(result, create.return_value)
        create.assert_called_once_with(model='gpt-5.5', max_completion_tokens=20)
        wrapped.close()
        client.close.assert_called_once()

    def test_skillopt_factory_is_adapted_and_restored_after_failure(self):
        from skillopt.model import openai_compatible_backend as backend
        sdk = Mock(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock())))
        with patch.object(backend, '_build_client', return_value=sdk) as factory:
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                with skillopt_chat_parameters('gpt-5.5'):
                    wrapped = backend._build_client(object())
                    wrapped.chat.completions.create(model='gpt-5.5', max_tokens=50, temperature=0)
                    sdk.chat.completions.create.assert_called_once_with(
                        model='gpt-5.5', max_completion_tokens=50)
                    raise RuntimeError('test failure')
            self.assertIs(backend._build_client, factory)
