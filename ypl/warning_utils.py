import warnings
from typing import TextIO

# Suppress third-party deprecation warnings. We override showwarning rather than using
# filterwarnings because libraries like pandas, langchain, fastmcp, and authlib reset
# the warning filters during import (via simplefilter("always", DeprecationWarning)),
# which overrides any "ignore" entries we set.
_original_showwarning = warnings.showwarning


def _suppressed_showwarning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: TextIO | None = None,
    line: str | None = None,
) -> None:
    if not issubclass(category, DeprecationWarning):
        _original_showwarning(message, category, filename, lineno, file, line)
        return
    # Only suppress third-party warnings (site-packages, frozen C modules, interpreter).
    # Preserve deprecation warnings from our own code.
    if "site-packages" in filename or "dist-packages" in filename or filename.startswith("<frozen"):
        return
    if filename == "sys" or filename.startswith("sys:"):
        return
    _original_showwarning(message, category, filename, lineno, file, line)


warnings.showwarning = _suppressed_showwarning

# Targeted filter for the swigvarlink warning emitted from C extensions during
# interpreter shutdown (sys:1). This bypasses our showwarning override because
# Python's cleanup has already started resetting modules.
warnings.filterwarnings("ignore", category=DeprecationWarning, message=r".*builtin type.*has no __module__.*")
