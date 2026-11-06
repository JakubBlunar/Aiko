from datetime import datetime, timezone
import json
from unittest.mock import patch

from app.llm.tools.builtins import GetTimeTool
from app.llm.tools.world import LookAroundTool


def test_explicit_zone_and_default_read_the_same_narrative_instant():
    shifted = datetime(2031, 2, 3, 12, tzinfo=timezone.utc)
    with patch("app.core.infra.timephrase.now", return_value=shifted):
        local = json.loads(GetTimeTool().run({}))
        tokyo = json.loads(GetTimeTool().run({"timezone": "Asia/Tokyo"}))
    assert datetime.fromisoformat(local["iso"]) == datetime.fromisoformat(tokyo["iso"])
    assert tokyo["iso"].endswith("+09:00") and "21:00" in tokyo["human"]


def test_world_schema_does_not_direct_the_model_to_a_retired_tool():
    description = LookAroundTool(None).schema().description
    assert "list_file_roots" not in description
    assert "start_workflow" in description and "when available" in description
