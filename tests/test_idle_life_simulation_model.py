from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.world.idle_life_simulation_model import (
    BudgetedModel,
    ModelBudgetExceeded,
    local_model,
)


def test_model_budget_caps_logical_calls_and_uses_actual_usage():
    client = Mock()
    client.chat_json.return_value = (
        '{"summary":"rested"}',
        SimpleNamespace(prompt_tokens=100, completion_tokens=30),
    )
    model = BudgetedModel(client, max_calls=1, max_tokens=500)
    model.chat_json(
        [{"role": "user", "content": "Choose an activity"}],
        model="fixture",
        options={"num_predict": 900},
    )
    assert model.calls == 1 and model.tokens >= 130
    call = client.chat_json.call_args.kwargs
    assert call["options"]["num_predict"] < 500 and call["think"] is False
    with pytest.raises(ModelBudgetExceeded):
        model.chat_json([])
    assert client.chat_json.call_count == 1


def test_model_budget_rejects_oversized_input_before_sending():
    client = Mock()
    model = BudgetedModel(client, max_calls=2, max_tokens=100)
    with pytest.raises(ModelBudgetExceeded):
        model.chat_json([{"role": "user", "content": "very long input " * 1000}])
    client.chat_json.assert_not_called()
    assert model.status()["exhausted"]


def test_model_experiments_cannot_use_external_endpoints():
    with pytest.raises(ValueError, match="loopback"):
        local_model(SimpleNamespace(ollama_url="https://example.test"))
