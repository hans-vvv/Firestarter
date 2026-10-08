import pytest

from app.models.dataclass_models import DeviceSelectorView, RoleView
from app.services.selectors.selector_engine import SelectorEngine


@pytest.fixture
def devices():
    """
    Minimal in-memory device set for SelectorEngine unit tests.
    """
    return [
        DeviceSelectorView(
            hostname="Core1.Site1",
            labels={"fabric": "core", "os_name": "iosxr"},
            role=RoleView("CORE"),
        ),
        DeviceSelectorView(
            hostname="PE1.Site11",
            labels={"fabric": "core", "os_name": "iosxr"},
            role=RoleView("PE"),
        ),
        DeviceSelectorView(
            hostname="PE2.Site11",
            labels={"fabric": "core", "os_name": "iosxr"},
            role=RoleView("PE"),
        ),
        DeviceSelectorView(
            hostname="RR1.Site11",
            labels={"fabric": "core", "os_name": "iosxr"},
            role=RoleView("RR"),
        ),
        DeviceSelectorView(
            hostname="CE1.Site11",
            labels={"fabric": "access"},
            role=RoleView("CE"),
        ),
    ]


@pytest.fixture
def selector_engine():
    # session is unused for pure selector tests
    return SelectorEngine(session=None)  # type: ignore


# --------------------------------------------------------------
# BASIC MATCH
# --------------------------------------------------------------


def test_match_role_include(selector_engine, devices):
    cfg = {"match": {"role": {"include": ["PE"]}}}

    result = selector_engine.select(devices, cfg)

    assert {d.hostname for d in result} == {"PE1.Site11", "PE2.Site11"}


def test_match_role_exclude(selector_engine, devices):
    cfg = {"match": {"role": {"exclude": ["CE"]}}}

    result = selector_engine.select(devices, cfg)

    assert "CE1.Site11" not in {d.hostname for d in result}


# --------------------------------------------------------------
# LABEL MATCHING
# --------------------------------------------------------------


def test_match_labels_scalar(selector_engine, devices):
    cfg = {"match": {"labels": {"fabric": "core"}}}

    result = selector_engine.select(devices, cfg)

    assert {d.hostname for d in result} == {"PE1.Site11", "PE2.Site11", "RR1.Site11", "Core1.Site1"}


def test_match_labels_include_exclude(selector_engine, devices):
    cfg = {
        "match": {
            "labels": {
                "fabric": {
                    "include": ["core"],
                    "exclude": ["access"],
                }
            }
        }
    }

    result = selector_engine.select(devices, cfg)

    assert "CE1.Site11" not in {d.hostname for d in result}


# --------------------------------------------------------------
# HOSTNAME GLOB
# --------------------------------------------------------------


def test_match_hostname_glob(selector_engine, devices):
    cfg = {"match": {"hostname": {"include": ["PE*"]}}}

    result = selector_engine.select(devices, cfg)

    assert {d.hostname for d in result} == {"PE1.Site11", "PE2.Site11"}


# --------------------------------------------------------------
# OR LOGIC
# --------------------------------------------------------------


def test_any_rules(selector_engine, devices):
    cfg = {
        "any": [
            {"role": {"include": ["RR"]}},
            {"hostname": {"include": ["CE*"]}},
        ]
    }

    result = selector_engine.select(devices, cfg)

    assert {d.hostname for d in result} == {"RR1.Site11", "CE1.Site11"}


# --------------------------------------------------------------
# EXCLUDE LOGIC
# --------------------------------------------------------------


def test_exclude_after_match(selector_engine, devices):
    cfg = {"match": {"labels": {"fabric": "core"}}, "exclude": {"role": {"include": ["RR"]}}}

    result = selector_engine.select(devices, cfg)

    assert {d.hostname for d in result} == {"PE1.Site11", "PE2.Site11", "Core1.Site1"}


# --------------------------------------------------------------
# ERROR CASES
# --------------------------------------------------------------


def test_unknown_match_key_raises(selector_engine, devices):
    cfg = {"match": {"unknown": {"foo": "bar"}}}

    with pytest.raises(RuntimeError, match="Unknown selector rule"):
        selector_engine.select(devices, cfg)


def test_unknown_or_rule_raises(selector_engine, devices):
    cfg = {"any": [{"unknown.field": "x"}]}

    with pytest.raises(RuntimeError, match="Unknown selector OR rule"):
        selector_engine.select(devices, cfg)


def test_labels_list_shorthand_not_supported(selector_engine, devices):
    cfg = {"match": {"labels": {"fabric": ["core"]}}}

    with pytest.raises(RuntimeError, match=r"List shorthand not supported"):
        selector_engine.select(devices, cfg)
