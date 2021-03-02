from __future__ import annotations
import threading
from contextvars import ContextVar, Token
from typing import get_type_hints, Dict, List, Tuple
from contextlib import contextmanager
from contextvars_extras.util import dedent_strip


MISSING = Token.MISSING


class ContextVarsRegistry:
    """A collection of ContextVar() objects, with nice @property-like way to access them.

    The idea is simple: you create a sub-class, and declare your variables using type annotations:

        >>> class CurrentVars(ContextVarsRegistry):
        ...    locale: str = 'en_GB'
        ...    timezone: str = 'Europe/London'
        ...    user_id: int = None
        ...    db_session: object

        >>> current = CurrentVars()

    When you create a sub-class, all type-hinted members become ContextVar() objects,
    and you can work with them by just getting/setting instance attributes:

        >>> current.locale
        'en_GB'

        >>> current.timezone
        'Europe/London'

        >>> current.timezone = 'UTC'
        >>> current.timezone
        'UTC'

    Getting/setting attributes is automatically mapped to ContextVar.get()/ContextVar.set() calls.

    The underlying ContextVar() objects can be managed via class attributes:

        >>> CurrentVars.timezone.get()
        'UTC'

        >>> token = CurrentVars.timezone.set('GMT')
        >>> current.timezone
        'GMT'
        >>> CurrentVars.timezone.reset(token)
        >>> current.timezone
        'UTC'

    Well, actually, the above is a little lie: the class members are actially instances of
    ContextVarDescriptor (not ContextVar). It has all the same get()/set()/reset() methods, but it
    is not a subclass (just because ContextVar can't be subclassed, this is a technical limitation).

    So class members are ContextVarDescriptor objects:

        >>> CurrentVars.timezone
        <ContextVarDescriptor name='contextvars_extras.registry.CurrentVars.timezone'...>

    and its underlying ContextVar can be reached via the `.context_var` attribute:

        >>> CurrentVars.timezone.context_var
        <ContextVar name='contextvars_extras.registry.CurrentVars.timezone'...>

    But in practice, you normally shouldn't need that.
    ContextVarDescriptor should implement all same attributes and methods as ContextVar,
    and thus it can be used instead of ContextVar() object in all cases except isinstance() checks.
    """

    _var_init_on_setattr: bool = True
    """Automatically initialize missing ContextVar() objects when setting attributes?

    If set to True (default), missing ContextVar() objects are automatically created when
    setting attributes. That is, you can define an empty class, and then set arbitrary attributes:

        >>> class CurrentVars(ContextVarsRegistry):
        ...    pass

        >>> current = CurrentVars()
        >>> current.locale = 'en'
        >>> current.timezone = 'UTC'

    However, if you find this behavior weak, you may disable it, like this:

        >>> class CurrentVars(ContextVarsRegistry):
        ...     _var_init_on_setattr = False
        ...     locale: str = 'en'

        >>> current = CurrentVars()
        >>> current.timezone = 'UTC'
        AttributeError: ...
    """

    _var_init_done_descriptors: Dict[str, ContextVarDescriptor]
    """A dictionary that tracks which attributes were initialized as ContextVarDescriptor objects.

    Keys are attribute names, and values are instances of :class:`ContextVarDescriptor`.
    The dictionary can be mutated at run time, because new context variables can be created
    on the fly, lazily, on 1st set of an attribute.

    Actually, this dictionary isn't strictly required.
    You can derive this mapping by iterating over all class attributes and calling isinstance().
    It is just kept for the convenience, and maybe small performance improvements.
    """

    _var_init_lock: threading.RLock
    """A lock that protects initialization of the class attributes as context variables.

    ContextVar() objects are lazily created on 1st attempt to set an attribute.
    So a race condition is possible: multiple concurrent threads (or co-routines)
    set an attribute for the 1st time, and thus create multiple ContextVar() objects.
    The last created ContextVar() object wins, and others are lost, causing a memory leak.

    So this lock is needed to ensure that ContextVar() object is created only once.
    """

    @contextmanager
    def __call__(self, **attr_names_and_values):
        """Set attributes temporarily, using context manager (the ``with`` statement in Python).

        Example of usage:

            >>> class CurrentVars(ContextVarsRegistry):
            ...     timezone: str = 'UTC'
            ...     locale: str = 'en'
            >>> current = CurrentVars()

            >>> with current(timezone='Europe/London', locale='en_GB'):
            ...    print(current.timezone)
            ...    print(current.locale)
            Europe/London
            en_GB

        On exiting form the ``with`` block, the values are restored:

            >>> current.timezone
            'UTC'
            >>> current.locale
            'en'

        .. caution::
            Only attributes that are present inside ``with (...)`` parenthesis are restored:

                >>> with current(timezone='Europe/London'):
                ...   current.locale = 'en_GB'
                ...   current.user_id = 42
                >>> current.timezone  # restored
                'UTC'
                >>> current.locale  # not restored
                'en_GB'
                >>> current.user_id  # not restored
                42

            That is, the ``with current(...)`` doesn't make a full copy of all context variables.
            It is NOT a scope isolation mechanism that protects all attributes.

            It is a more primitive tool, roughly, a syntax sugar for this:

                >>> try:
                ...     saved_timezone = current.timezone
                ...     current.timezone = 'Europe/London'
                ...
                ...     # do_something_useful_with_current_timezone()
                ... finally:
                ...     current.timezone = saved_timezone
        """
        saved_state: List[Tuple[ContextVarDescriptor, Token]] = []
        try:
            # Set context variables, saving their state.
            for attr_name, value in attr_names_and_values.items():
                # This is almost like __setattr__(), except that it also saves a Token() object,
                # that allows to restore the previous value later (via ContextVar.reset() method).
                descriptor = self.__before_set__ensure_initialized(attr_name, value)
                reset_token = descriptor.set(value)  # calls ContextVar.set() method
                saved_state.append((descriptor, reset_token))

            # execute code inside the ``with: ...`` block
            yield
        finally:
            # Restore context variables.
            for (descriptor, reset_token) in saved_state:
                descriptor.reset(reset_token)  # calls ContextVar.reset() method

    @classmethod
    def __init_subclass__(cls):
        cls._var_init_done_descriptors = dict()
        cls._var_init_lock = threading.RLock()
        cls.__init_type_hinted_class_attrs_as_descriptors()

    @classmethod
    def __init_type_hinted_class_attrs_as_descriptors(cls):
        hinted_attrs = get_type_hints(cls)
        for attr_name in hinted_attrs:
            cls.__init_class_attr_as_descriptor(attr_name)

    @classmethod
    def __init_class_attr_as_descriptor(cls, attr_name):
        with cls._var_init_lock:
            if attr_name in cls._var_init_done_descriptors:
                return

            if attr_name.startswith('_var_'):
                return

            value = getattr(cls, attr_name, MISSING)
            assert not isinstance(value, (ContextVar, ContextVarDescriptor))

            var_name = f"{cls.__module__}.{cls.__name__}.{attr_name}"
            new_var_descriptor = ContextVarDescriptor(var_name, default=value)
            setattr(cls, attr_name, new_var_descriptor)

            cls._var_init_done_descriptors[attr_name] = new_var_descriptor

    def __init__(self):
        cls = self.__class__
        if cls == ContextVarsRegistry:
            raise MustBeSubclassedError.format()

    def __setattr__(self, attr_name, value):
        self.__before_set__ensure_initialized(attr_name, value)
        super().__setattr__(attr_name, value)

    @classmethod
    def __before_set__ensure_initialized(self, attr_name, value) -> ContextVarDescriptor:
        try:
            return self._var_init_done_descriptors[attr_name]
        except KeyError:
            self.__before_set__ensure_not_starts_with_special_var_prefix(attr_name, value)
            self.__before_set__initialize_attr_as_context_var_descriptor(attr_name, value)

            return self._var_init_done_descriptors[attr_name]

    @classmethod
    def __before_set__ensure_not_starts_with_special_var_prefix(cls, attr_name, value):
        if attr_name.startswith('_var_'):
            raise ReservedAttributeError.format(cls, attr_name, value)

    @classmethod
    def __before_set__initialize_attr_as_context_var_descriptor(cls, attr_name, value):
        assert attr_name not in cls._var_init_done_descriptors, \
            "This method should not be called when attribute is already initialized as ContextVar"

        if not cls._var_init_on_setattr:
            raise UndeclaredAttributeError.format(cls, attr_name, value)

        cls.__init_class_attr_as_descriptor(attr_name)


class ContextVarDescriptor:
    context_var: ContextVar

    def __init__(self, name, default=MISSING):
        if default is MISSING:
            self.context_var = ContextVar(name)
        else:
            self.context_var = ContextVar(name, default=default)

        self.name = self.context_var.name
        self.get = self.context_var.get
        self.set = self.context_var.set
        self.reset = self.context_var.reset

        self.default = default

    def __get__(self, instance, _unused_owner_cls):
        if instance is None:
            return self
        return self.context_var.get()

    def __set__(self, instance, value):
        assert instance is not None
        self.context_var.set(value)

    def __repr__(self):
        if self.default is MISSING:
            return f"<{self.__class__.__name__} name={self.name}>"
        else:
            return f"<{self.__class__.__name__} name={self.name!r} default={self.default!r}>"


class MustBeSubclassedError(NotImplementedError):
    @staticmethod
    def format():
        return MustBeSubclassedError(
            dedent_strip(
                """
                class ContextVarsRegistry cannot be instanciated directly without sub-classing.

                You have to create a sub-class before using it:

                    class CurrentVars(ContextVarsRegistry):
                        var1: str = "default_value"

                    current = CurrentVars()
                    current.var1   # => "default_value"
                """
            )
        )


class ReservedAttributeError(AttributeError):
    @staticmethod
    def format(context_vars_registry_subclass, attr_name, attr_value):
        cls_name = context_vars_registry_subclass.__name__
        attr_type_name = type(attr_value).__name__

        return ReservedAttributeError(
            dedent_strip(
                f"""
                Can't set attribute '{attr_name}' because of special '_var_' prefix.

                '_var_' prefix is reserved for ContextVarProxy class settings.
                You can't set such attribute on the instance level.

                If you want to configure the class, you should do it on the class level:
                like this:

                class {cls_name}(ContextVarsRegistry):
                    {attr_name}: {attr_type_name}
                """
            )
        )


class UndeclaredAttributeError(AttributeError):
    @staticmethod
    def format(context_var_proxy_subclass, attr_name, attr_value):
        cls_name = context_var_proxy_subclass.__name__
        attr_type_name = type(attr_value).__name__

        return UndeclaredAttributeError(
            dedent_strip(
                f"""
                Can't set undeclared attribute: {cls_name}.{attr_name}

                Maybe there is a typo in the attribute name?

                If this is a new attribute, then you have to first declare it in the class,
                with a type hint, like this:

                class {cls_name}(...):
                    {attr_name}: {attr_type_name}
                """
            )
        )
