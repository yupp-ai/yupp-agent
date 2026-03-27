import inspect
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

T = TypeVar("T")


def normalize_args[T](unwrapped_func: Callable[..., T], args: tuple, kwargs: dict) -> tuple[tuple, dict]:
    """
    Return normalized kwargs dict.
    """

    sig = inspect.signature(unwrapped_func)

    # Get the parameter names in order
    param_names = list(sig.parameters.keys())

    # Create a new kwargs dict with all parameters
    normalized_kwargs = {}

    # First handle positional arguments
    for i, arg in enumerate(args):
        normalized_kwargs[param_names[i]] = arg

    # Then handle keyword arguments, which override positional args
    normalized_kwargs.update(kwargs)

    # Add default values for any missing parameters
    for param_name, param in sig.parameters.items():
        if (
            param_name in normalized_kwargs
            and param.default is not inspect.Parameter.empty
            and normalized_kwargs[param_name] == param.default
        ):
            del normalized_kwargs[param_name]

    normalized_kwargs = dict(sorted(normalized_kwargs.items()))
    return (), normalized_kwargs


def normalize_params[T](func: Callable[..., T]) -> Callable[..., T]:
    """
    A decorator that normalizes function parameters to ensure consistent cache keys.
    This ensures that whether a parameter is passed as a positional argument or keyword argument,
    the cache key will be the same.

    Args:
        func: The function to decorate

    Example:
        @normalize_params
        @cache
        def my_func(a: int, b: int = 2) -> int:
            return a + b

        # These will use the same cache key:
        my_func(1)
        my_func(1, 2)
        my_func(1, b=2)
        my_func(a=1, b=2)
    """

    # Get the original signature before applying decorators
    unwrapped_func = inspect.unwrap(func)

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        normalized_args, normalized_kwargs = normalize_args(unwrapped_func, args, kwargs)

        # Call the original function with all parameters as keyword arguments
        return func(*normalized_args, **normalized_kwargs)

    def _normalize_args(args: tuple, kwargs: dict) -> tuple[tuple, dict]:
        return normalize_args(unwrapped_func, args, kwargs)

    wrapper.normalize_args = _normalize_args  # type: ignore
    return wrapper
