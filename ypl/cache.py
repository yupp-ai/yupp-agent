import hashlib
import shutil
from functools import cache
from pathlib import Path
from typing import Any

from ypl.backend.config import settings


class CacheDir:
    def __init__(self, cache_dir: str):
        self.root = Path(cache_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, key: Any, *, create_folder_if_not_exists: bool = True) -> tuple[Path, bool]:
        path = self.root / hashlib.md5(str(key).encode()).hexdigest()

        if create_folder_if_not_exists and not path.exists():
            created = True
            path.mkdir(parents=True, exist_ok=True)
        else:
            created = False

        return path, created

    def delete(self, key: Any) -> None:
        path = self.root / hashlib.md5(str(key).encode()).hexdigest()

        if path.exists():
            shutil.rmtree(path)


@cache
def get_cache_dir() -> CacheDir:
    return CacheDir(settings.CACHE_DIR)
