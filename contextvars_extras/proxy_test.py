from pytest import raises
from contextvars_extras.proxy import ContextVarsProxy, ContextVarDescriptor, ContextVar


def test__ContextVarsProxy__must_be_subclassed__and_cannot_be_instanciated_directly():
    with raises(NotImplementedError):
        ContextVarsProxy()

    class Subclass(ContextVarsProxy):
        pass
    Subclass()


def test__class_members__with_type_hints__are_automatically_converted_to_context_var_descriptors():
    class MyVars(ContextVarsProxy):
        # magically becomes ContextVarDescriptor()
        hinted: str = "hinted"

        # no type hint, so not affected
        not_hinted = "not hinted"

    my_vars = MyVars()

    # at class level, type-hinted attributes are converted to ContextVarDescriptor objects
    assert isinstance(MyVars.hinted, ContextVarDescriptor)
    assert isinstance(MyVars.not_hinted, str)

    # at instance level, descriptors do proxy calls to ContextVar.get()/ContextVar.set() methods
    assert my_vars.hinted == "hinted"
    my_vars.hinted = 42
    assert my_vars.hinted == 42

    # At class level, descriptors can be used to call .get()/.set()/.reset() on a ContextVar.
    assert MyVars.hinted.get() == 42

    token = MyVars.hinted.set(43)
    assert my_vars.hinted == MyVars.hinted.get() == 43

    MyVars.hinted.reset(token)
    assert my_vars.hinted == MyVars.hinted.get() == 42

    # underlying ContextVar() objects are available via ContextVarDescriptor.context_var
    assert isinstance(MyVars.hinted.context_var, ContextVar)

    # also, ContextVar() automatically get verbose name, useful for debugging
    assert (
        "contextvars_extras.proxy_test.MyVars.hinted"
        == MyVars.hinted.name
        == MyVars.hinted.context_var.name
    )


def test__class_member_values__become__context_var_defaults():
    class MyVars(ContextVarsProxy):
        has_default: str = "has default value"
        has_none_as_default: int = None
        no_default: str

    my_vars = MyVars()

    assert my_vars.has_default == "has default value"
    assert my_vars.has_none_as_default is None

    # an attempt to get value of an unitialized variable raises an exception
    # It is not a bug. It is a feature: "default=None" and "no default at all" are 2 separate cases.
    with raises(LookupError):
        my_vars.no_default

    # after we initialize it, the error is not raised anymore
    my_vars.no_default = None
    assert my_vars.no_default is None


def test__missing_vars__are_automatically_created__on_setattr():
    class CurrentVars(ContextVarsProxy):
        pass

    current = CurrentVars()

    with raises(AttributeError):
        current.timezone
    current.timezone = 'Europe/Moscow'
    assert CurrentVars.timezone.get() == 'Europe/Moscow'

    # ...but this feature may be disabled by setting `_var_init_on_setattr = False`
    # Let's test that:

    class CurrentVars(ContextVarsProxy):
        _var_init_on_setattr = False

    current = CurrentVars()

    with raises(AttributeError):
        current.timezone = 'Europe/Moscow'


def test__var_prefix__is_reserved__and_cannot_be_used_for_context_variables():
    class CurrentVars(ContextVarsProxy):
        _var_foo: str = 'foo'

    current = CurrentVars()

    # _var_* attributes cannot be set on instance level
    with raises(AttributeError):
        current._var_foo = 'bar'

    # and they don't become ContextVar() objects,
    # even though they were declared with type hints on the class level
    assert isinstance(CurrentVars._var_foo, str)   # normally you expect ContextVarDescriptor here
