"""Curated OpenAI OAuth model catalogue.

Subscription tokens cannot list models (`GET /v1/models` is not available to
ChatGPT plan credentials), so the product ships a known-good list instead:
a human label for the picker and the real slug used on the wire.

Adding a model here is the only place the two names are defined; the UI never
derives one from the other.
"""

# Order is the order shown in the picker.
_CURATED = (
    {'id': 'gpt-6-luna', 'label': 'GPT-6 Luna', 'hint': '速度优先'},
    {'id': 'gpt-6.1-sol', 'label': 'GPT-6.1 Sol', 'hint': '通用'},
    {'id': 'gpt-6.1-terra', 'label': 'GPT-6.1 Terra', 'hint': '长上下文'},
    {'id': 'gpt-6.2-sol', 'label': 'GPT-6.2 Sol', 'hint': '最强推理'},
)


def catalogue():
    """Return a fresh copy so callers cannot mutate the table."""
    return [dict(item) for item in _CURATED]


def label_for(model_id):
    for item in _CURATED:
        if item['id'] == model_id:
            return item['label']
    return model_id


def is_known(model_id):
    return any(item['id'] == model_id for item in _CURATED)
