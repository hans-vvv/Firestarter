from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.validation.service_definition import ServiceDefinitionDocument

# ---------------------------------------------------------------------------
# ServiceDefinitionDocument
# ---------------------------------------------------------------------------


class TestServiceDefinitionDocument:
    def _valid(self) -> dict:
        return {
            "service": "isis",
            "tenant": "lab",
            "variant": "default",
            "selectors": {"devices": {"isis": {"match": {}}}},
            "features": {"isis": {"process_id": "1"}},
            "interface_features": {"isis": {"process_id": "1"}},
            "parameters": {},
        }

    def test_valid_document_parses(self):
        ServiceDefinitionDocument(**self._valid())

    def test_missing_service_raises(self):
        data = self._valid()
        del data["service"]
        with pytest.raises(ValidationError):
            ServiceDefinitionDocument(**data)

    def test_missing_tenant_raises(self):
        data = self._valid()
        del data["tenant"]
        with pytest.raises(ValidationError):
            ServiceDefinitionDocument(**data)

    def test_missing_variant_raises(self):
        data = self._valid()
        del data["variant"]
        with pytest.raises(ValidationError):
            ServiceDefinitionDocument(**data)

    def test_missing_selectors_raises(self):
        data = self._valid()
        del data["selectors"]
        with pytest.raises(ValidationError):
            ServiceDefinitionDocument(**data)

    def test_missing_features_raises(self):
        data = self._valid()
        del data["features"]
        with pytest.raises(ValidationError):
            ServiceDefinitionDocument(**data)

    def test_missing_interface_features_raises(self):
        data = self._valid()
        del data["interface_features"]
        with pytest.raises(ValidationError):
            ServiceDefinitionDocument(**data)

    def test_missing_parameters_raises(self):
        data = self._valid()
        del data["parameters"]
        with pytest.raises(ValidationError):
            ServiceDefinitionDocument(**data)

    def test_extra_field_raises(self):
        data = self._valid()
        data["unexpected"] = "value"
        with pytest.raises(ValidationError):
            ServiceDefinitionDocument(**data)

    def test_features_accepts_arbitrary_dict(self):
        data = self._valid()
        data["features"] = {"any_key": {"nested": True}}
        ServiceDefinitionDocument(**data)

    def test_parameters_accepts_arbitrary_dict(self):
        data = self._valid()
        data["parameters"] = {"allocations": {"pool": "some_pool"}}
        ServiceDefinitionDocument(**data)
