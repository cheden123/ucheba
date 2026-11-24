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
    from ucheba.analysis import _chunks, _parse

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
    assert assignments[0].source_kind == "speech"


def test_slide_marked_as_source_and_ungrounded_date_removed():
    slide = "Домашнее задание\nСдать отчёт  до пятницы."
    assignment = {
        "id": 2,
        "title": "Отчёт",
        "quote": "Домашнее задание Сдать отчёт до пятницы.",
        "requirements": "Сдать отчёт",
        "deadline": "до 18 декабря",
        "plan": [],
    }

    def mock(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {}})
        if request.method == "GET":
            return httpx.Response(200, json={"data": [catalog_row()]})
        sent = json.loads(request.content)
        fragments = [json.loads(line) for line in sent["messages"][1]["content"].splitlines()]
        assert [row["source_kind"] for row in fragments] == ["speech", "slide", "speech"]
        assert fragments[1]["text"] == slide
        return completion([assignment, {**assignment, "id": 3}])

    result = analyze(
        [
            Segment(start=10, end=20, text="Перейдём к домашнему заданию."),
            Segment(start=70, end=75, text="На сегодня всё."),
        ],
        "fake-key",
        slides=[(35.5, slide)],
        transport=httpx.MockTransport(mock),
    )
    assert len(result.assignments) == 1
    extracted = result.assignments[0]
    assert (extracted.start, extracted.end, extracted.source_kind) == (35.5, 35.5, "slide")
    assert extracted.quote == slide
    assert extracted.deadline is None


def test_summary_reduce_preserves_order_and_formula_case():
    candidates = [f"Понятие номер {index}." for index in range(50)]
    summary = analysis._brief_points(candidates)
    assert len(summary) == analysis.SUMMARY_LIMIT
    assert (summary[0], summary[-1]) == (candidates[0], candidates[-1])
    assert [candidates.index(point) for point in summary] == sorted(
        candidates.index(point) for point in summary
    )
    assert analysis._unique_points(["a  = I", "a = I", "a = i"]) == ["a  = I", "a = i"]


def test_check_key_performs_only_get_requests_and_never_returns_secrets() -> None:
    from ucheba.analysis import check_openrouter_key
    import httpx

    class Requests:
        paths: list[str] = []

        def __call__(self, request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            self.paths.append(request.url.path)
            if request.url.path.endswith("/key"):
                return httpx.Response(
                    200,
                    json={
                        "data": {"label": "PRIVATE", "free_model_daily_requests": {"remaining": 5}}
                    },
                )
            return httpx.Response(200, json={"data": []})

    requests = Requests()
    message = check_openrouter_key("sk-not-a-real-key", transport=httpx.MockTransport(requests))
    assert requests.paths == ["/api/v1/key", "/api/v1/models"]
    assert "принял ключ" in message and "5" in message
    assert "PRIVATE" not in message and "sk-not-a-real-key" not in message


def test_check_key_does_not_echo_remote_401() -> None:
    import httpx
    import pytest
    from ucheba.analysis import OpenRouterError, check_openrouter_key

    transport = httpx.MockTransport(lambda _: httpx.Response(401, text="private server details"))
    with pytest.raises(OpenRouterError, match="отклонил ключ") as error:
        check_openrouter_key("fake-key", transport=transport)
    assert "private server" not in str(error.value)
