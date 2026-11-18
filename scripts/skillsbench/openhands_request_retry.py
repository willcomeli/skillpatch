"""Installed only into the pinned OpenHands tool environment by bench.py."""

from functools import wraps
from importlib.metadata import version
import logging
import os
import sys

try:
    from .retry_policy import transient_network_failure
except ImportError:
    from skillpatch_network_retry_policy import transient_network_failure

logger = logging.getLogger('skillpatch.request_retry')


class IncompleteStreamError(ConnectionError):
    pass


def extend_retry_decorator(original):
    @wraps(original)
    def retry_decorator(self, *args, **kwargs):
        native = original(self, *args, **kwargs)

        def decorate(function):
            wrapped = native(function)
            policy = wrapped.retry
            native_stop, native_retry, native_before_sleep = policy.stop, policy.retry, policy.before_sleep

            def exception(state):
                return state.outcome.exception() if state.outcome is not None else None

            policy.stop = lambda state: False if transient_network_failure(exception(state)) else native_stop(state)
            policy.retry = lambda state: transient_network_failure(exception(state)) or native_retry(state)

            def before_sleep(state):
                native_before_sleep(state)
                if transient_network_failure(exception(state)):
                    logger.warning('Retrying the same model request after transport failure; attempt=%s; network retry limit=unlimited',
                                   state.attempt_number)

            policy.before_sleep = before_sleep
            return wrapped

        return decorate

    return retry_decorator


def check_stream_transport(original):
    @wraps(original)
    def call(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if kwargs.get('enable_streaming'):
            choices = result.get('choices') or []
            if not choices or any(choice.get('finish_reason') is None for choice in choices):
                raise IncompleteStreamError('Model stream ended without a completed response')
        return result
    return call


def install():
    if version('openhands-sdk') != '1.22.1':
        raise RuntimeError('Request retry hook requires pinned openhands-sdk 1.22.1')
    from openhands.sdk.llm.utils.retry_mixin import RetryMixin
    from openhands.sdk.llm.llm import LLM
    if getattr(RetryMixin, '_skillpatch_step_retries', False):
        return
    RetryMixin.retry_decorator = extend_retry_decorator(RetryMixin.retry_decorator)
    LLM._transport_call = check_stream_transport(LLM._transport_call)
    RetryMixin._skillpatch_step_retries = True


def install_or_exit():
    try:
        install()
    except Exception:
        # Python normally ignores .pth startup failures. Do not silently run
        # an experiment without its requested recovery policy.
        print('OpenHands request retry hook failed to initialize; refusing unpatched execution.', file=sys.stderr, flush=True)
        os._exit(78)
