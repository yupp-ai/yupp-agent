import inspect


def is_primitive_type(typ: type) -> bool:
    """Check if a type is a primitive type (int, float, str, bool, None)."""
    return typ in [int, float, str, bool, type(None)]


def has_classmethod(cls: type, method_name: str) -> bool:
    """Check if a class has a classmethod with the given name."""
    try:
        for base in cls.__mro__:
            if method_name in base.__dict__:
                attr = base.__dict__[method_name]
                return isinstance(attr, classmethod)
        return False
    except AttributeError:
        # Certain types like UnionType don't have __mro__ attribute
        return False


def has_instance_method(cls: type, method_name: str) -> bool:
    """Check if a class has an instance method with the given name."""
    try:
        for base in cls.__mro__:
            if method_name in base.__dict__:
                attr = base.__dict__[method_name]
                return inspect.isfunction(attr)
        return False
    except AttributeError:
        # Certain types like UnionType don't have __mro__ attribute
        return False
