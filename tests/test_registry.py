from pytest import raises

from contextvars_extras.registry import ContextVar, ContextVarDescriptor, ContextVarsRegistry

# pylint: disable=attribute-defined-outside-init,protected-access,pointless-statement
# pylint: disable=function-redefined


def test__ContextVarsRegistry__must_be_subclassed__and_cannot_be_instanciated_directly():
    with raises(NotImplementedError):
        ContextVarsRegistry()

    class Subclass(ContextVarsRegistry):
        pass

    Subclass()


def test__class_members__with_type_hints__are_automatically_converted_to_context_var_descriptors():
    class MyVars(ContextVarsRegistry):
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
        "tests.test_registry.MyVars.hinted" == MyVars.hinted.name == MyVars.hinted.context_var.name
    )


def test__class_member_values__become__context_var_defaults():
    class MyVars(ContextVarsRegistry):
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
    class CurrentVars(ContextVarsRegistry):
        pass

    current = CurrentVars()

    with raises(AttributeError):
        current.timezone
    current.timezone = "Europe/Moscow"
    assert CurrentVars.timezone.get() == "Europe/Moscow"

    # ...but this feature may be disabled by setting `_var_init_on_setattr = False`
    # Let's test that:

    class CurrentVars(ContextVarsRegistry):
        _var_init_on_setattr = False

    current = CurrentVars()

    with raises(AttributeError):
        current.timezone = "Europe/Moscow"


def test__var_prefix__is_reserved__and_cannot_be_used_for_context_variables():
    class CurrentVars(ContextVarsRegistry):
        _var_foo: str = "foo"

    current = CurrentVars()

    # _var_* attributes cannot be set on instance level
    with raises(AttributeError):
        current._var_foo = "bar"

    # and they don't become ContextVar() objects,
    # even though they were declared with type hints on the class level
    assert isinstance(CurrentVars._var_foo, str)  # normally you expect ContextVarDescriptor here


def test__with_context_manager__sets_variables__temporarily():
    class CurrentVars(ContextVarsRegistry):
        timezone: str = "UTC"
        locale: str

    current = CurrentVars()

    with current(timezone="Europe/London", locale="en"):
        with current(locale="en_GB", user_id=1):
            assert current.timezone == "Europe/London"
            assert current.locale == "en_GB"
            assert current.user_id == 1
        assert current.timezone == "Europe/London"
        assert current.locale == "en"
        assert CurrentVars.user_id.get("FALLBACK_VALUE") == "FALLBACK_VALUE"

        # ``user_id`` wasn't set above using the ``with()`` block,
        # so it will NOT be restored afterrwards
        current.user_id = 2

    # not restored, because not present in the ``with (...)`` parenthesis
    assert current.user_id == 2

    # these two were set using ``with (...)``, so they are restored to their initial states
    assert current.timezone == "UTC"
    assert CurrentVars.locale.get("FALLBACK_VALUE") == "FALLBACK_VALUE"


def test__with_context_manager__throws_error__when_setting_reserved_var_attribute():
    class CurrentVars(ContextVarsRegistry):
        _var_foo: str = "not a ContextVar because of special _var_ prefix"

    current = CurrentVars()

    with raises(AttributeError):
        with current(_var_foo="foo"):
            pass

    with raises(AttributeError):
        with current(_var_bar="bar"):
            pass


def test__with_context_manager__throws_error__when_init_on_setattr_is_disabled():
    class CurrentVars(ContextVarsRegistry):
        _var_init_on_setattr = False
        locale: str = "en"

    current = CurrentVars()

    with current(locale="en_US"):
        assert current.locale == "en_US"

    # an attempt to set current.timezone will raise AttributeError
    # Because the variable wasn't declared in the class definition
    # (and dynamic creation of variables is disabled by ``_var_init_on_setattr = False``)
    with raises(AttributeError):
        with current(locale="en_US", timezone="America/New_York"):
            pass


def test__with_context_manager__restores_attrs__even_if_exception_is_raised():
    class CurrentVars(ContextVarsRegistry):
        locale: str = "en"

    current = CurrentVars()

    # Try to set a couple of attributes using the ``with`` statement.
    #
    # Upon exit from the ``with`` block, the attribute states must be restored,
    # even though ValueError was raised inside.
    with raises(ValueError):
        with current(locale="en_US", user_id=42):
            raise ValueError

    # current.locale is restored to the default value
    assert current.locale == "en"

    # current.user_id is also restored to its initial state:(no value, getattr raises LookupError)
    with raises(LookupError):
        current.user_id
