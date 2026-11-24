"""Собрать лицензионные файлы установленных зависимостей приложения."""

import re
import shutil
import sys
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement


def copy_notices(root: Path) -> None:
    pending = ["ucheba-ai"]
    seen: set[str] = set()
    while pending:
        package = distribution(pending.pop())
        name = package.metadata["Name"]
        if name in seen:
            continue
        seen.add(name)
        for raw_dependency in package.requires or ():
            dependency = Requirement(raw_dependency)
            if dependency.marker is None or dependency.marker.evaluate({"extra": ""}):
                pending.append(dependency.name)
        for item in package.files or ():
            filename = item.name.lower()
            in_metadata = any(part.endswith(".dist-info") for part in item.parts)
            license_name = re.match(r"^(license|licence|copying|copyright|notice)", filename)
            if in_metadata and (license_name or "licenses" in item.parts):
                destination = root / "licenses" / name / Path(*item.parts[1:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(package.locate_file(item), destination)


if __name__ == "__main__":
    copy_notices(Path(sys.argv[1]))
