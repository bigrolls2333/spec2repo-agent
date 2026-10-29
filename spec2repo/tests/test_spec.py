import json

import pytest
from pydantic import ValidationError

from spec2repo.domain.spec import CoreSpec


def test_sample_has_traceable_acceptance_and_is_ready(spec_payload):
    spec = CoreSpec.model_validate(spec_payload)
    assert spec.ready_for_planning
    assert len(spec.requirements) == 5
    assert len(spec.acceptance_criteria) == 8


@pytest.mark.parametrize("group", ["requirements", "operations", "acceptance_criteria"])
def test_duplicate_ids_are_rejected(spec_payload, group):
    spec_payload[group].append(spec_payload[group][0])
    with pytest.raises(ValidationError, match="Duplicate IDs"):
        CoreSpec.model_validate(spec_payload)


@pytest.mark.parametrize("group", ["operations", "acceptance_criteria"])
def test_unknown_requirement_reference_is_rejected(spec_payload, group):
    spec_payload[group][0]["requirement_ids"] = ["REQ-NOT-DEFINED"]
    with pytest.raises(ValidationError, match="unknown requirements"):
        CoreSpec.model_validate(spec_payload)


def test_requirement_without_acceptance_is_rejected(spec_payload):
    spec_payload["requirements"].append({"id": "REQ-NEW", "description": "新增行为"})
    with pytest.raises(ValidationError, match="without acceptance criteria"):
        CoreSpec.model_validate(spec_payload)


def test_duplicate_route_is_rejected(spec_payload):
    duplicate = {**spec_payload["operations"][0], "id": "OP-DUPLICATE"}
    spec_payload["operations"].append(duplicate)
    with pytest.raises(ValidationError, match="method/path"):
        CoreSpec.model_validate(spec_payload)


def test_unknown_fields_are_not_silently_ignored(spec_payload):
    spec_payload["requrements"] = []
    with pytest.raises(ValidationError, match="Extra inputs"):
        CoreSpec.model_validate(spec_payload)


def test_blocking_question_must_have_nonempty_answer(spec_payload):
    spec_payload["open_questions"] = [{"id": "Q-STATUS", "question": "允许逆向更新吗？"}]
    blocked = CoreSpec.model_validate(spec_payload)
    assert not blocked.ready_for_planning
    assert blocked.blocking_question_ids == ("Q-STATUS",)
    spec_payload["open_questions"][0]["answer"] = "不允许"
    answered = CoreSpec.model_validate(spec_payload)
    assert answered.ready_for_planning
    assert answered.fingerprint() != blocked.fingerprint()
    spec_payload["open_questions"][0]["answer"] = "  "
    with pytest.raises(ValidationError):
        CoreSpec.model_validate(spec_payload)


def test_optional_question_does_not_block(spec_payload):
    spec_payload["open_questions"] = [
        {"id": "Q-STYLE", "question": "偏好的内部模块命名？", "blocking": False}
    ]
    assert CoreSpec.model_validate(spec_payload).ready_for_planning


def test_fingerprint_ignores_json_formatting_but_tracks_business_changes(spec_payload):
    original = CoreSpec.model_validate(spec_payload)
    reordered = json.dumps(spec_payload, sort_keys=True, indent=4)
    assert CoreSpec.model_validate_json(reordered).fingerprint() == original.fingerprint()
    spec_payload["requirements"][0]["description"] += " 新的业务约束。"
    assert CoreSpec.model_validate(spec_payload).fingerprint() != original.fingerprint()


@pytest.mark.parametrize("version", [True, "1", 0, -1])
def test_version_is_a_positive_integer(spec_payload, version):
    spec_payload["version"] = version
    with pytest.raises(ValidationError):
        CoreSpec.model_validate(spec_payload)
