from contextvars import ContextVar, Token
from typing import Any, Callable, Generic, Optional, TypeVar, Union

from contextvars_extras.context_management import bind_to_empty_context
from contextvars_extras.sentinel import MISSING, Missing, Sentinel


class ContextVarDeletionMark(Sentinel):
    """A special placeholder object written into ContextVar when its value is deleted.

    Problem: in Python, it is not possible to delete value of a ContextVar object.
    Once the variable is set, it cannot be unset.
    But, we (or at least I, the author) need to support deletion in :class:`ContextVarExt`.

    So, here is a workaround:

    1. Put a special deletion mark to the context variable.

    2. Add extra logic to :meth:``ContextVarExt.get()`` that recognizes the deletion mark
       and acts as if the value is not set (returns a default value or throws LookupError).

    Also, this :class:`ContextVarDeletionMark` has 2 instances (while you might expect a singleton),
    since there are 2 slightly different ways to erase the context variable:

    1. :data:`CONTEXT_VAR_VALUE_DELETED`
         Indicates that context variable is just erased.
         Used by the :meth:`~ContextVarExt.delete` method.

    2. :data:`CONTEXT_VAR_RESET_TO_DEFAULT`
         Indicates that context variable is reset to default value (as if it was never set).
         Used by :meth:`~ContextVarExt.reset_to_default` and the `Deferred Defaults`_ feature.

    Normally you shouldn't see these marker objects.
    They're an implementation detail, that shouldn't leak outside, unless you work with
    the `Underlying ContextVar object`_ directly, or use the :meth:`ContextVarExt.get_raw` method.
    """


CONTEXT_VAR_VALUE_DELETED = ContextVarDeletionMark(__name__, "CONTEXT_VAR_VALUE_DELETED")
"""Special placeholder object that marks context variable as deleted."""

CONTEXT_VAR_RESET_TO_DEFAULT = ContextVarDeletionMark(__name__, "CONTEXT_VAR_RESET_TO_DEFAULT")
"""Special placeholder object that resets variable to a default value (as if it was never set)."""


VarValueT = TypeVar("VarValueT")  # value, stored in the ContextVar object
FallbackT = TypeVar("FallbackT")  # an object, returned by .get() when ContextVar has no value


class ContextVarExt(Generic[VarValueT]):
    context_var: ContextVar[Union[VarValueT, ContextVarDeletionMark]]
    name: str
    default: Union[VarValueT, Missing]
    _deferred_default: Optional[Callable[[], VarValueT]]

    def __init__(
        self,
        name: Optional[str] = None,
        default: Union[VarValueT, Missing] = MISSING,
        deferred_default: Optional[Callable[[], VarValueT]] = None,
        context_var: Optional[ContextVar[VarValueT]] = None,
    ):
        """Initialize ContextVarExt object.

        :param name: Name for the underlying ``ContextVar`` object.
                     Needed for introspection and debugging purposes.

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
        """
        assert name or context_var
        assert not (name and context_var)

        if name:
            self._init_with_creating_new_context_var(name, default, deferred_default)

        if context_var:
            assert default is MISSING
            self._init_from_existing_context_var(context_var, deferred_default)

    def _init_from_existing_context_var(self, context_var, deferred_default):
        assert context_var

        self.context_var = context_var
        self.name = context_var.name
        self.default = get_context_var_default(context_var)
        self._deferred_default = deferred_default

        assert not ((self.default is not MISSING) and (self._deferred_default is not None))

        self._init_fast_methods()
        self._init_deferred_default()

    def _init_with_creating_new_context_var(self, name, default, deferred_default):
        assert name
        assert not ((default is not MISSING) and (deferred_default is not None))

        context_var = self._new_context_var(name, default)

        self.context_var = context_var
        self.name = name
        self.default = default
        self._deferred_default = deferred_default

        self._init_fast_methods()
        self._init_deferred_default()

    @classmethod
    def _new_context_var(cls, name: str, default):
        context_var: ContextVar[Union[VarValueT, ContextVarDeletionMark]]

        if default is MISSING:
            context_var = ContextVar(name)
        else:
            context_var = ContextVar(name, default=default)

        return context_var

    def _init_fast_methods(self):
        # Problem: basic ContextVar.get()/.set()/etc() must have good performance.
        #
        # So, I decided to do some evil premature optimization: instead of regular methods,
        # I define them as functions (closures) here, and then write them as methods to
        # the ContextVarExt() instance.
        #
        # Closures, are faster than methods, because they can:
        #  - take less arguments (each function argument adds some overhead)
        #  - avoid `self` (because Python's attribute resolution mechanism has some performance hit)
        #  - avoid `.` - the dot operator (because, again, attribute access is slow)
        #  - avoid globals (because they're slower than local variables)
        #
        # Of course, all these overheads are minor, but they add up.
        # For example .get() call became 2x faster after these optimizations.
        # So I decided to keep them.

        # The `.` (the dot operator that resoles attributes) has some overhead.
        # So, do it in advance to avoid dots in closures below.
        context_var = self.context_var
        context_var_get = context_var.get
        context_var_set = context_var.set
        context_var_ext_default = self.default
        context_var_ext_deferred_default = self._deferred_default

        # Local variables are faster than globals.
        # So, copy all needed globals and thus make them locals.
        _MISSING = MISSING
        _CONTEXT_VAR_VALUE_DELETED = CONTEXT_VAR_VALUE_DELETED
        _CONTEXT_VAR_RESET_TO_DEFAULT = CONTEXT_VAR_RESET_TO_DEFAULT
        _LookupError = LookupError

        # Ok, now define closures that use all the variables prepared above.

        # NOTE: function name is chosen such that it looks good in stack traces.
        # When an exception is thrown, just "get" looks cryptic, while "_method_ContextVarExt_get"
        # at least gives you a hint that the ContextVarExt.get method is the source of exception.
        def _method_ContextVarExt_get(default=_MISSING):
            if default is _MISSING:
                value = context_var_get()
            else:
                value = context_var_get(default)

            # special marker, left by ContextVarExt.reset_to_default()
            if value is _CONTEXT_VAR_RESET_TO_DEFAULT:
                if default is not _MISSING:
                    return default
                if context_var_ext_default is not _MISSING:
                    return context_var_ext_default
                if context_var_ext_deferred_default is not None:
                    value = context_var_ext_deferred_default()
                    context_var_set(value)
                    return value
                raise _LookupError(context_var)

            # special marker, left by ContextVarExt.delete()
            if value is _CONTEXT_VAR_VALUE_DELETED:
                if default is not _MISSING:
                    return default
                raise _LookupError(context_var)

            return value

        self.get = _method_ContextVarExt_get  # type: ignore[assignment]

        def _method_ContextVarExt_is_set() -> bool:
            return context_var_get(_MISSING) not in (  # type: ignore[arg-type]
                _MISSING,
                _CONTEXT_VAR_VALUE_DELETED,
                _CONTEXT_VAR_RESET_TO_DEFAULT,
            )

        self.is_set = _method_ContextVarExt_is_set  # type: ignore[assignment]

        # Copy some methods from ContextVar.
        # These are even better than closures above, because they are C functions.
        # So by calling, for example ``ContextVarRegistry.set()``, you're *actually* calling
        # tje low-level C function ``ContextVar.set`` directly, without any Python-level wrappers.
        self.get_raw = self.context_var.get  # type: ignore[assignment]
        self.set = self.context_var.set  # type: ignore[assignment]
        self.reset = self.context_var.reset  # type: ignore[assignment]

    def _init_deferred_default(self):
        # In case ``deferred_default`` is used, put a special marker object to the variable
        # (otherwise ContextVar.get() method will not find any value and raise a LookupError)
        if self._deferred_default and not self.is_set():
            self.reset_to_default()

    def get(self, default=MISSING):
        """Return a value for the context variable for the current context.

        If there is no value for the variable in the current context,
        the method will:

          * return the value of the ``default`` argument of the method, if provided; or
          * return the default value for the context variable, if it was created with one; or
          * raise a :exc:`LookupError`.

        Example usage::

            >>> locale_var = ContextVarExt('locale_var', default='UTC')

            >>> locale_var.get()
            'UTC'

            >>> locale_var.set('Europe/London')
            <Token ...>

            >>> locale_var.get()
            'Europe/London'


        Note that if that if there is no ``default`` value, it may raise ``LookupError``::

            >>> locale_var = ContextVarExt('locale_var')

            >>> try:
            ...     locale_var.get()
            ... except LookupError:
            ...     print('LookupError was raised')
            LookupError was raised

            # The exception can be prevented by supplying the `.get(default)` argument.
            >>> locale_var.get(default='en')
            'en'

            >>> locale_var.set('en_GB')
            <Token ...>

            # The `.get(default=...)` argument is ignored since the value was set above.
            >>> locale_var.get(default='en')
            'en_GB'
        """
        # pylint: disable=no-self-use,method-hidden
        # This code is never actually called, see ``_init_fast_methods``.
        # It exists only for auto-generated documentation and static code analysis tools.
        raise AssertionError

    def get_raw(self, default=MISSING):
        """Return a value for the context variable, without overhead added by :meth:`get` method.

        This is a more lightweight version of :meth:`get` method.
        It is faster, but doesn't support some features (like deletion).

        In fact, it is a direct reference to the standard :meth:`contextvars.ContextVar.get` method,
        which is a built-in method (written in C), check this out::

            >>> timezone_var = ContextVarExt('timezone_var')

            >>> timezone_var.get_raw
            <built-in method get of ContextVar object ...>

            >>> timezone_var.get_raw == timezone_var.context_var.get
            True

        So here is absolutely no overhead on top of the standard ``ContextVar.get()`` method,
        and you can safely use ``.get_raw()`` when you need performance.

        See also, documentation for this method in the standard library:
        :meth:`contextvars.ContextVar.get`.
        """
        # pylint: disable=no-self-use,method-hidden
        # This code is never actually called, see ``_init_fast_methods``.
        # It exists only for auto-generated documentation and static code analysis tools.
        raise AssertionError

    def is_set(self) -> bool:
        """Ceck if the variable has a value.

        Examples::

            >>> timezone_var = ContextVarExt('timezone_var')
            >>> timezone_var.is_set()
            False

            >>> timezone_var = ContextVarExt('timezone_var', default='UTC')
            >>> timezone_var.is_set()
            False

            >>> timezone_var.set('GMT')
            <Token ...>
            >>> timezone_var.is_set()
            True

            >>> timezone_var.reset_to_default()
            >>> timezone_var.is_set()
            False

            >>> timezone_var.set(None)
            <Token ...>
            >>> timezone_var.is_set()
            True

            >>> timezone_var.delete()
            >>> timezone_var.is_set()
            False
        """
        # pylint: disable=no-self-use,method-hidden
        # This code is never actually called, see ``_init_fast_methods``.
        # It exists only for auto-generated documentation and static code analysis tools.
        raise AssertionError

    def set(self, value) -> Token:
        """Call to set a new value for the context variable in the current context.

        The required *value* argument is the new value for the context variable.

        Returns a :class:`~contextvars.contextvars.Token` object that can be used to restore
        the variable to its previous value via the :meth:`~ContextVarExt.reset` method.

        .. Note::

          This method is a shortcut to method of the standard ``ContextVar`` class,
          please check out its documentation: :meth:`contextvars.ContextVar.set`.
        """
        # pylint: disable=no-self-use,method-hidden
        # This code is never actually called, see ``_init_fast_methods``.
        # It exists only for auto-generated documentation and static code analysis tools.
        raise AssertionError

    def set_if_not_set(self, value) -> Any:
        """Set value if not yet set.

        Examples::

            >>> locale_var = ContextVarExt('locale_var', default='en')

            # The context variable has no value set yet (the `default='en'` above isn't
            # treated as if value was set), so the call to .set_if_not_set() has effect.
            >>> locale_var.set_if_not_set('en_US')
            'en_US'

            # The 2nd call to .set_if_not_set() has no effect.
            >>> locale_var.set_if_not_set('en_GB')
            'en_US'

            >>> locale_var.get(default='en')
            'en_US'

            # .delete() method reverts context variable into "not set" state.
            >>> locale_var.delete()
            >>> locale_var.set_if_not_set('en_GB')
            'en_GB'

            # .reset_to_default() also means that variable becomes "not set".
            >>> locale_var.reset_to_default()
            >>> locale_var.set_if_not_set('en_AU')
            'en_AU'
        """
        existing_value = self.get(_NotSet)

        if existing_value is _NotSet:
            self.set(value)
            return value

        return existing_value

    def reset(self, token: Token):
        """Reset the context variable to a previous value.

        Reset the context variable to the value it had before the
        :meth:`ContextVarExt.set` that created the *token* was used.

        For example::

            >>> var = ContextVar('var')

            >>> token = var.set('new value')
            >>> var.get()
            'new value'

            # After the reset call the var has no value again,
            # so var.get() would raise a LookupError.
            >>> var.reset(token)
            >>> var.get()
            Traceback (most recent call last):
            ...
            LookupError: ...

        .. Note::

          This method is a shortcut to method of the standard ``ContextVar`` class,
          please check out its documentation: :meth:`contextvars.ContextVar.reset`.
        """
        # pylint: disable=no-self-use,method-hidden
        # This code is never actually called, see ``_init_fast_methods``.
        # It exists only for auto-generated documentation and static code analysis tools.
        raise AssertionError

    def reset_to_default(self):
        """Reset context variable to the default value.

        Example::

            >>> timezone_var = ContextVarExt('timezone_var', default='UTC')

            >>> timezone_var.set('Antarctica/Troll')
            <Token ...>

            >>> timezone_var.reset_to_default()

            >>> timezone_var.get()
            'UTC'

            >>> timezone_var.get(default='GMT')
            'GMT'

        When there is no default value, the value is erased, so ``get()`` raises ``LookupError``::

            >>> timezone_var = ContextVarExt('timezone_var')

            >>> timezone_var.set('Antarctica/Troll')
            <Token ...>

            >>> timezone_var.reset_to_default()

            # ContextVar has no default value, so .get() call raises LookupError.
            >>> try:
            ...     timezone_var.get()
            ... except LookupError:
            ...     print('LookupError was raised')
            LookupError was raised

            # The exception can be avoided by passing a `default=...` value.
            timezone_var.get(default='UTC')
            'UTC'
        """
        self.set(CONTEXT_VAR_RESET_TO_DEFAULT)

    def delete(self):
        """Delete value stored in the context variable.

        Example::

            # Create a context variable, and set a value.
            >>> timezone_var = ContextVarExt('timezone_var')
            >>> timezone_var.set('Europe/London')
            <Token ...>

            # ...so .get() call doesn't raise an exception and returns the value that was set
            >>> timezone_var.get()
            'Europe/London'

            # Call .delete() to erase the value.
            >>> timezone_var.delete()

            # Once value is deleted, the .get() method raises LookupError.
            >>> try:
            ...     timezone_var.get()
            ... except LookupError:
            ...     print('LookupError was raised')
            LookupError was raised

            # The exception can be avoided by passing a `default=...` value.
            >>> timezone_var.get(default='GMT')
            'GMT'
        """
        self.set(CONTEXT_VAR_VALUE_DELETED)

    def __repr__(self):
        return f"<{self.__class__.__name__} name={self.name!r}>"


# A special sentinel object, used only by the ContextVarExt.set_if_not_set() method.
_NotSet = Sentinel(__name__, "_NotSet")


@bind_to_empty_context
def get_context_var_default(context_var: ContextVar, missing=MISSING):
    """Get a default value from :class:`contextvars.ContextVar` object.

    Example::

      >>> from contextvars import ContextVar
      >>> from contextvars_extras.context_var_ext import get_context_var_default

      >>> timezone_var = ContextVar('timezone_var', default='UTC')

      >>> timezone_var.set('GMT')
      <Token ...>

      >>> get_context_var_default(timezone_var)
      'UTC'

    In case the default value is missing, the :func:`get_context_var_default`
    returns a special sentinel object called ``MISSING``::

      >>> timezone_var = ContextVar('timezone_var')  # no default value

      >>> timezone_var.set('UTC')
      <Token ...>

      >>> get_context_var_default(timezone_var)
      contextvars_extras.sentinel.MISSING

    You can also use a custom missing marker (instead of ``MISSING``), like this::

      >>> get_context_var_default(timezone_var, '[NO DEFAULT TIMEZONE]')
      '[NO DEFAULT TIMEZONE]'
    """
    try:
        return context_var.get()
    except LookupError:
        return missing
