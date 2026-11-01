from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.validation.addressing import AddressingFeature, AddressingPolicyDocument

# ---------------------------------------------------------------------------
# AddressingFeature
# ---------------------------------------------------------------------------


class TestAddressingFeature:
    def _valid(self):
        return {
            "loopback0": {"by_roles": {"core": "core_lb0_pool"}},
            "loopback1": {"by_roles": {"core": "core_lb1_pool"}},
            "p2p": {"by_roles": {"core-core": "p2p_pool"}},
        }

    def test_accepts_valid_feature(self):
        obj = AddressingFeature(**self._valid())
        assert obj.loopback0 == {"by_roles": {"core": "core_lb0_pool"}}
        assert obj.loopback1 == {"by_roles": {"core": "core_lb1_pool"}}
        assert obj.p2p == {"by_roles": {"core-core": "p2p_pool"}}

    def test_rejects_missing_loopback0(self):
        data = self._valid()
        del data["loopback0"]
        with pytest.raises(ValidationError):
            AddressingFeature(**data)

    def test_rejects_missing_loopback1(self):
        data = self._valid()
        del data["loopback1"]
        with pytest.raises(ValidationError):
            AddressingFeature(**data)

    def test_rejects_missing_p2p(self):
        data = self._valid()
        del data["p2p"]
        with pytest.raises(ValidationError):
            AddressingFeature(**data)

    def test_rejects_extra_field(self):
        data = self._valid()
        data["loopback2"] = {"by_roles": {}}
        with pytest.raises(ValidationError):
            AddressingFeature(**data)

    def test_accepts_empty_dicts_for_all_fields(self):
        obj = AddressingFeature(loopback0={}, loopback1={}, p2p={})
        assert obj.loopback0 == {}
        assert obj.p2p == {}

    def test_arbitrary_dict_content_is_accepted(self):
        obj = AddressingFeature(
            loopback0={"nested": {"deep": [1, 2, 3]}},
            loopback1={"flag": True},
            p2p={"count": 42},
        )
        assert obj.loopback0["nested"]["deep"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# AddressingPolicyDocument
# ---------------------------------------------------------------------------


class TestAddressingPolicyDocument:
    def _valid(self):
        return {
            "name": "addressing_lab_def",
            "selectors": {"devices": {"addressing": {"match": {"labels": {"tenant": "lab"}}}}},
            "features": {
                "addressing": {
                    "loopback0": {"by_roles": {"core": "core_lb0"}},
                    "loopback1": {"by_roles": {"core": "core_lb1"}},
                    "p2p": {"by_roles": {"core-core": "p2p_pool"}},
                }
            },
        }

    def test_accepts_valid_document(self):
        obj = AddressingPolicyDocument(**self._valid())
        assert obj.name == "addressing_lab_def"

    def test_name_is_plain_string(self):
        data = self._valid()
        data["name"] = "any_name_works"
        obj = AddressingPolicyDocument(**data)
        assert obj.name == "any_name_works"

    def test_selectors_is_arbitrary_dict(self):
        data = self._valid()
        data["selectors"] = {"custom_key": [1, 2, 3]}
        obj = AddressingPolicyDocument(**data)
        assert obj.selectors["custom_key"] == [1, 2, 3]

    def test_features_must_contain_addressingfeature_values(self):
        data = self._valid()
        # Provide a feature that is missing required keys — should fail
        data["features"] = {
            "addressing": {"loopback0": {}}  # missing loopback1 and p2p
        }
        with pytest.raises(ValidationError):
            AddressingPolicyDocument(**data)

    def test_accepts_multiple_feature_entries(self):
        data = self._valid()
        data["features"]["other"] = {
            "loopback0": {},
            "loopback1": {},
            "p2p": {},
        }
        obj = AddressingPolicyDocument(**data)
        assert "other" in obj.features

    def test_rejects_missing_name(self):
        data = self._valid()
        del data["name"]
        with pytest.raises(ValidationError):
            AddressingPolicyDocument(**data)

    def test_rejects_missing_selectors(self):
        data = self._valid()
        del data["selectors"]
        with pytest.raises(ValidationError):
            AddressingPolicyDocument(**data)

    def test_rejects_missing_features(self):
        data = self._valid()
        del data["features"]
        with pytest.raises(ValidationError):
            AddressingPolicyDocument(**data)

    def test_rejects_extra_top_level_field(self):
        data = self._valid()
        data["unexpected"] = "value"
        with pytest.raises(ValidationError):
            AddressingPolicyDocument(**data)
