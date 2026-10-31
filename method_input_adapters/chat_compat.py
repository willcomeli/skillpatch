"""Adapt GPT-5.5 chat request parameters without editing upstream methods."""

from contextlib import contextmanager


def is_gpt55(model):
    return isinstance(model, str) and model.rsplit('/', 1)[-1].startswith('gpt-5.5')


def chat_parameters(kwargs):
    values = dict(kwargs)
    if is_gpt55(values.get('model')):
        if 'max_tokens' in values:
            limit = values.pop('max_tokens')
            if 'max_completion_tokens' in values and values['max_completion_tokens'] != limit:
                raise ValueError('Conflicting GPT completion token limits')
            values['max_completion_tokens'] = limit
        # Use the reasoning model's default sampling behavior across methods.
        values.pop('temperature', None)
        values.pop('top_p', None)
    return values


class _Proxy:
    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        return getattr(self._target, name)


class _Completions(_Proxy):
    def create(self, **kwargs):
        return self._target.create(**chat_parameters(kwargs))


class _Chat(_Proxy):
    def __init__(self, target):
        super().__init__(target)
        self.completions = _Completions(target.completions)


class ChatClient(_Proxy):
    def __init__(self, target):
        super().__init__(target)
        self.chat = _Chat(target.chat)


@contextmanager
def skillopt_chat_parameters(model):
    """Limit the upstream client factory adaptation to this training call."""
    if not is_gpt55(model):
        yield
        return
    from skillopt.model import openai_compatible_backend as backend
    original = backend._build_client
    backend._build_client = lambda config: ChatClient(original(config))
    try:
        yield
    finally:
        backend._build_client = original
