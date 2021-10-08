"""ContextVarDescriptor - extension for the built-in ContextVar that behaves like @property."""

from contextvars import ContextVar
from typing import Any, Optional

from contextvars_extras.context_var_ext import ContextVarExt, DeferredDefaultFn, Missing
from contextvars_extras.util import ExceptionDocstringMixin


class ContextVarDescriptor(ContextVarExt):
    def __init__(
        self,
        name: Optional[str] = None,
        default: Optional[Any] = Missing,
        deferred_default: Optional[DeferredDefaultFn] = None,
        context_var: Optional[ContextVar] = None,
        owner_cls: Optional[type] = None,
        owner_attr: Optional[str] = None,
    ):
        """Initialize ContextVarDescriptor object.

        :param name: Name for the underlying ``ContextVar`` object.
                     Needed for introspection and debugging purposes.
                     Ususlly you don't want to set it manually, because it is automatically
                     formatted from owner class/attribute names.

        :param default: The default value for the  underlying ``ContextVar`` object.
                        Returned by the ``get()`` method if the variable is not bound to a value.
                        If default is missing, then ``get()`` may raise ``LookupError``.

        :param deferred_default: A function that produces a default value.
                                 Called by ``get()`` method, once per context.
                                 That is, if you spawn 10 threads, then ``deferred_default()``
                                 is called 10 times, and you get 10 thread-local values.

        :param context_var: A reference to an existing ``ContextVar`` object.
                            You need it only if you want to re-use an existing object.
                            If missing, a new ``ContextVar`` object is created automatically.

        :param owner_cls: Reference to a class, where the descritor is placed.
                          Usually it is captured automatically by the ``__set_name__`` method,
                          however, you need to pass it manually if you're adding a new descriptor
                          after the class is already created.

        :param owner_attr: Name of the attribute, where the descriptor is placed.
                           Usually it is captured automatically by the ``__set_name__`` method,
                           however, you need to pass it manually if you're adding a new descriptor
                           after the class is already creted.
        """
        if name or context_var:
            super().__init__(
                name=name,
                default=default,
                deferred_default=deferred_default,
                context_var=context_var,
            )
        else:
            assert not ((default is not Missing) and (deferred_default is not None))
            self._default = default
            self._deferred_default = deferred_default

            if owner_cls and owner_attr:
                context_var = self._new_context_var_for_owner(owner_cls, owner_attr, default)
                self._set_context_var(context_var)

    def __set_name__(self, owner_cls: type, owner_attr: str):
        if hasattr(self, "context_var"):
            return

        context_var = self._new_context_var_for_owner(owner_cls, owner_attr, self._default)
        self._set_context_var(context_var)

    @classmethod
    def _new_context_var_for_owner(cls, owner_cls: type, owner_attr: str, default) -> ContextVar:
        name = f"{owner_cls.__module__}.{owner_cls.__name__}.{owner_attr}"
        return cls._new_context_var(name, default)

    def __get__(self, instance, _unused_owner_cls):
        if instance is None:
            return self
        try:
            return self.get()
        except LookupError as err:
            raise ContextVarNotSetError.format(context_var_name=self.name) from err

    def __set__(self, instance, value):
        assert instance is not None
        self.set(value)

    def __delete__(self, instance):
        self.__get__(instance, None)  # needed to raise AttributeError if already deleted
        self.delete()


class ContextVarNotSetError(ExceptionDocstringMixin, AttributeError, LookupError):
    """Context variable is not set: '{context_var_name}'.

    This exception is usually raised when you declare a context variable without a default value,
    like this:

        >>> from contextvars_extras.registry import ContextVarsRegistry
        >>> class Current(ContextVarsRegistry):
        ...     timezone: str
        >>> current = Current()

    In this case, the variable remains unitialized (as if the attribute was never set),
    so an attempt to read the attribute will raise an exception::

        >>> current.timezone
        Traceback (most recent call last):
        ...
        contextvars_extras.context_var_descriptor.ContextVarNotSetError: ...

    So you have 2 options to solve the problem:

    1. Execute your code with setting the missing attribute, like this:

        >>> with current(timezone='UTC'):
        ...     # put your code here
        ...     print(current.timezone)  # now it doesn't raise the error
        UTC

    2. Add a default value to your registry class, like this::

        >>> class Current(ContextVarsRegistry):
        ...     timezone: str = 'UTC'
        >>> current = Current()
        >>> current.timezone
        'UTC'

    .. Note::

      This exception is a subclass of **both** ``AttributeError`` and ``LookupError``.

      - ``AttributeError`` comes from Python's descriptor protocol.
        We have to throw it, otherwise ``hasattr()`` and ``geattr()`` will not work nicely.

      - ``LookupError`` is thrown by the standard ``contextvars.ContextVar.get()`` method
        when the variable is not set. So we do the same for consistency with the standard library.

      So, to fit both cases, this exception uses both ``AttributeErrror`` and ``LookupError``
      as base classes.
    """