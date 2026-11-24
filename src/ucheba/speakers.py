from pathlib import Path
from tempfile import TemporaryDirectory

from .documents import academic_document, safe_text, timestamp, write_json
from .imports import load_transcript

ROLES = frozenset({"lecturer", "student", "ignore", "unknown"})


def save_review(result: Path, choices: dict[str, tuple[str, str]]) -> Path:
    segments = load_transcript(result / "transcript.json")
    speakers = {segment.speaker for segment in segments if segment.speaker}
    if not choices or not choices.keys() <= speakers:
        raise ValueError("Выберите говорящих из расшифровки.")
    if any(role not in ROLES or not name.strip() for name, role in choices.values()):
        raise ValueError("Укажите имя и допустимую роль для каждого выбранного говорящего.")
    if not any(role == "lecturer" for _, role in choices.values()):
        raise ValueError("Подтвердите хотя бы одного преподавателя.")

    document = academic_document()
    document.add_heading("Речь преподавателей и вопросы студентов", 0)
    document.add_paragraph(
        "Роли подтверждены вручную. Полная исходная расшифровка хранится отдельно. "
        "Реплики студентов сохранены для контекста."
    )
    reviewed = []
    for segment in segments:
        if segment.speaker is None or segment.speaker not in choices:
            reviewed.append(segment.model_dump(mode="json"))
            continue
        name, role = choices[segment.speaker]
        if role == "ignore":
            continue
        if role == "unknown":
            reviewed.append(segment.model_dump(mode="json"))
            continue
        time = f"[{timestamp(segment.start)}] " if segment.end > segment.start else ""
        label = "Преподаватель" if role == "lecturer" else "Студент"
        attributed = f"{label} ({name.strip()})"
        reviewed.append(segment.model_copy(update={"speaker": attributed}).model_dump(mode="json"))
        document.add_paragraph(safe_text(f"{time}{attributed} — {segment.text}"))

    target = result / "Речь_преподавателей.docx"
    with TemporaryDirectory(prefix=".speaker-review-", dir=result) as temporary:
        temp = Path(temporary)
        document.save(str(temp / target.name))
        write_json(
            temp / "speaker_review.json",
            {
                speaker: {"name": name.strip(), "role": role}
                for speaker, (name, role) in choices.items()
            },
        )
        reviewed_name = "Расшифровка_с_подтверждёнными_ролями.json"
        write_json(temp / reviewed_name, reviewed)
        (temp / reviewed_name).replace(result / reviewed_name)
        (temp / target.name).replace(target)
        (temp / "speaker_review.json").replace(result / "speaker_review.json")
    return target
