"""Download and stage the pinned public speaker models for Windows packaging."""

import hashlib
import tarfile
from pathlib import Path
from urllib.request import urlretrieve

BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
CACHE = Path("build/diarization-models")
SEGMENTATION = "sherpa-onnx-pyannote-segmentation-3-0"
EMBEDDING = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
SEGMENTATION_HASH = "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"
EMBEDDING_HASH = "f1ee61be04000b50392a63442e5dd9feaea7e81dddd494d3647cf1c3d88a1498"
ARCHIVE_HASH = "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"


def verify(path: Path, digest: str) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == digest


def download(asset: str, path: Path, digest: str) -> None:
    if verify(path, digest):
        return
    pending = path.with_suffix(".download")
    try:
        urlretrieve(BASE + asset, pending)
        if not verify(pending, digest):
            raise RuntimeError(f"Контрольная сумма модели не совпадает: {path.name}")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    model = CACHE / "segmentation.onnx"
    license_file = CACHE / "segmentation-LICENSE.txt"
    if not verify(model, SEGMENTATION_HASH) or not license_file.is_file():
        archive = CACHE.parent / f"{SEGMENTATION}.tar.bz2"
        download(f"speaker-segmentation-models/{archive.name}", archive, ARCHIVE_HASH)
        with tarfile.open(archive, "r:bz2") as source:
            for member, target in (("model.onnx", model), ("LICENSE", license_file)):
                stream = source.extractfile(f"{SEGMENTATION}/{member}")
                if stream is None:
                    raise RuntimeError(f"Архив модели не содержит {member}.")
                with stream:
                    target.write_bytes(stream.read())
    if not verify(model, SEGMENTATION_HASH):
        raise RuntimeError("Контрольная сумма извлечённой модели не совпадает.")
    download(f"speaker-recongition-models/{EMBEDDING}", CACHE / "embedding.onnx", EMBEDDING_HASH)
    (CACHE / "Источники.txt").write_text(
        "Pyannote segmentation 3.0 (MIT), ONNX export from sherpa-onnx:\n"
        f"{BASE}speaker-segmentation-models/{SEGMENTATION}.tar.bz2\n"
        "3D-Speaker ERes2Net base (Apache-2.0), ONNX export from sherpa-onnx:\n"
        f"{BASE}speaker-recongition-models/{EMBEDDING}\n"
        "https://github.com/modelscope/3D-Speaker\n"
        "https://www.apache.org/licenses/LICENSE-2.0\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
