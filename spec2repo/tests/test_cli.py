import json

from spec2repo.cli import main


def test_cli_reports_ready_contract(tmp_path, spec_payload, capsys):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec_payload), encoding="utf-8")
    assert main(["validate-spec", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["ready_for_planning"] is True


def test_cli_distinguishes_clarification_from_invalid_input(tmp_path, spec_payload, capsys):
    spec_payload["open_questions"] = [{"id": "Q-1", "question": "谁能更新？"}]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec_payload), encoding="utf-8")
    assert main(["validate-spec", str(path)]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is True
    assert output["blocking_question_ids"] == ["Q-1"]


def test_invalid_input_does_not_echo_secret(tmp_path, capsys):
    path = tmp_path / "invalid.json"
    path.write_text('{"private_secret": "do-not-echo-this"}', encoding="utf-8")
    assert main(["validate-spec", str(path)]) == 1
    output = capsys.readouterr()
    assert "do-not-echo-this" not in output.err
    assert json.loads(output.err)["valid"] is False


def test_missing_file_returns_structured_error(tmp_path, capsys):
    assert main(["validate-spec", str(tmp_path / "missing.json")]) == 1
    assert json.loads(capsys.readouterr().err)["valid"] is False
