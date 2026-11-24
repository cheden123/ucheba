import json

import httpx
import pytest

from ucheba import analysis
from ucheba.analysis import OpenRouterError, analyze
from ucheba.models import Segment


def catalog_row(identifier="example/model:free", **prices):
    return {
        "id": identifier,
        "pricing": {"prompt": "0", "completion": "0", **prices},
        "context_length": 16000,
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "supported_parameters": ["structured_outputs", "response_format"],
    }


def completion(assignments=None):
    payload = {"summary": ["Гидродинамика"], "details": [], "assignments": assignments or []}
    return httpx.Response(
        200,
        json={
            "model": "example/model:free",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}],
            "usage": {"cost": 0, "prompt_tokens": 100, "completion_tokens": 20},
        },
    )


def test_free_only_and_source_verification(tmp_path):
    quote = "Решите задачу номер три до пятницы."
    task = {
        "id": 1,
        "title": "Задача номер три",
        "quote": quote,
        "requirements": "Решите задачу номер три",
        "deadline": "до пятницы",
        "plan": ["Выписать исходные данные."],
    }
    segments = [Segment(start=19, end=25, text=quote)]
    calls = []

    def mock(request):
        assert request.url.host == "openrouter.ai"
        calls.append(request.url.path)
        if request.url.path.endswith("/key"):
            return httpx.Response(
                200, json={"data": {"free_model_daily_requests": {"remaining": 8}}}
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        catalog_row("example/not-confirmed"),
                        catalog_row("example/hidden-cost:free", input_cache_read="0.000001"),
                        catalog_row(),
                    ]
                },
            )
        sent = json.loads(request.content)
        assert sent["model"] == "example/model:free"
        assert sent["provider"]["max_price"] == dict.fromkeys(
            ("prompt", "completion", "request", "image"), 0
        )
        assert sent["response_format"]["json_schema"]["strict"]
        assert "fake-key" not in request.content.decode()
        return completion([task, {**task, "quote": "Придуманная цитата."}, {**task, "id": 100}])

    result = analyze(segments, "fake-key", cache_dir=tmp_path, transport=httpx.MockTransport(mock))
    expected = ["/api/v1/key", "/api/v1/models", "/api/v1/chat/completions"]
    assert calls == expected
    assert result.requests == 1
    assert (result.input_tokens, result.output_tokens) == (100, 20)
    assert len(result.assignments) == 1
    assert result.assignments[0].deadline == "до пятницы"
    assert result.assignments[0].start == 19

    result = analyze(segments, "fake-key", cache_dir=tmp_path, transport=httpx.MockTransport(mock))
    assert calls == expected
    assert result.requests == 0
    assert len(result.assignments) == 1


def test_pricing_missing_or_paid_never_posts():
    def mock(request):
        assert request.method == "GET"
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(
            200,
            json={
                "data": [
                    catalog_row("example/fee:free", request="0.01"),
                    catalog_row("example/unknown:free", completion=None),
                ]
            },
        )

    with pytest.raises(OpenRouterError, match="Нет доступной"):
        analyze(
            [Segment(start=0, end=1, text="Лекция.")],
            "fake-key",
            transport=httpx.MockTransport(mock),
        )


def test_retry_after_and_catalog_recheck(monkeypatch):
    waits = []
    monkeypatch.setattr(analysis, "_wait", lambda seconds, _cancelled: waits.append(seconds))
    calls = []

    def mock(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {}})
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [catalog_row()]})
        if calls.count("POST") == 1:
            return httpx.Response(429, headers={"Retry-After": "9"})
        return completion()

    result = analyze(
        [Segment(start=0, end=1, text="Лекция.")],
        "fake-key",
        transport=httpx.MockTransport(mock),
    )
    assert calls == ["GET", "POST", "GET", "POST"]
    assert waits[0] == 9
    assert result.requests == 2
    assert not result.assignments


def test_cancelled_does_not_contact_api():
    def mock(_request):
        pytest.fail("Выполнен сетевой запрос после отмены.")

    with pytest.raises(OpenRouterError, match="отменён"):
        analyze(
            [Segment(start=0, end=1, text="Лекция.")],
            "fake-key",
            cancelled=lambda: True,
            transport=httpx.MockTransport(mock),
        )


def test_daily_quota_exhausted_never_posts():
    def mock(request):
        assert request.method == "GET"
        assert request.url.path.endswith("/key")
        return httpx.Response(200, json={"data": {"free_model_daily_requests": {"remaining": 0}}})

    with pytest.raises(OpenRouterError, match="Дневной лимит"):
        analyze(
            [Segment(start=0, end=1, text="Лекция.")],
            "fake-key",
            transport=httpx.MockTransport(mock),
        )


def test_student_text_is_not_accepted_and_title_is_sourced() -> None:
    import json

    from ucheba.analysis import _chunks, _parse
    from ucheba.models import Segment

    text = "Прочитайте третью главу до пятницы."
    student = Segment(text=text, start=0, end=1, speaker="Студент 2")
    teacher = Segment(text=text, start=1, end=2, speaker="Преподаватель")
    body, evidence = next(_chunks([student, teacher]))
    assert json.loads(body.splitlines()[0])["speaker"] == "Студент 2"
    rows = [
        {
            "id": index,
            "title": "Выдуманный заголовок",
            "quote": text,
            "requirements": "Прочитайте третью главу",
            "deadline": "до пятницы",
            "plan": [],
        }
        for index in (1, 2)
    ]
    _, _, assignments = _parse({"summary": [], "details": [], "assignments": rows}, evidence)
    assert [item.title for item in assignments] == ["Прочитайте третью главу"]
    assert assignments[0].speaker == "Преподаватель"
