import base64
import json
import os
import tempfile

import pytest
import yaml

from rdeploy.utils import (
    strtobool,
    get_path,
    format_yaml,
    get_settings,
    get_helm_bin,
    decode_data_value,
    decode_data_fields,
    json_decode_data_fields,
    yaml_decode_data_fields,
)


class TestStrtobool:
    @pytest.mark.parametrize("val", ["y", "yes", "t", "true", "on", "1", "YES", "True", "ON"])
    def test_truthy_values(self, val):
        assert strtobool(val) is True

    @pytest.mark.parametrize("val", ["n", "no", "f", "false", "off", "0", "NO", "False", "OFF"])
    def test_falsy_values(self, val):
        assert strtobool(val) is False

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="invalid truth value"):
            strtobool("maybe")

    def test_whitespace_stripped(self):
        assert strtobool("  yes  ") is True


class TestGetPath:
    def test_returns_string(self):
        result = get_path()
        assert isinstance(result, str)
        assert os.path.isabs(result)


class TestFormatYaml:
    def test_replaces_env_vars(self):
        template = "host: ${HOST}, port: ${PORT}"
        config = {"HOST": "localhost", "PORT": "8080"}
        result = format_yaml(template, config)
        assert result == "host: localhost, port: 8080"

    def test_no_replacement_when_no_match(self):
        template = "host: ${HOST}"
        config = {"PORT": "8080"}
        result = format_yaml(template, config)
        assert result == "host: ${HOST}"


class TestGetSettings:
    def test_loads_yaml_file(self, tmp_path):
        settings = {
            "version": "3",
            "configs": {
                "staging": {
                    "namespace": "test-ns",
                    "project_name": "test-project",
                }
            }
        }
        config_file = tmp_path / "rdeploy.yaml"
        config_file.write_text(yaml.dump(settings))
        result = get_settings(str(config_file))
        assert result == settings

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            get_settings("/nonexistent/rdeploy.yaml")


class TestGetHelmBin:
    def test_system_helm(self):
        config = {"use_system_helm": True}
        assert get_helm_bin(config) == "helm"

    def test_system_helm_default(self):
        config = {}
        assert get_helm_bin(config) == "helm"

    def test_local_helm(self):
        config = {"use_system_helm": False, "helm_version": "3.12.0"}
        result = get_helm_bin(config)
        assert "3.12.0" in result
        assert "helm" in result


class TestDecodeDataValue:
    def test_decodes_plain_string(self):
        encoded = base64.b64encode(b"hello").decode()
        assert decode_data_value(encoded) == "hello"

    def test_decodes_json_value(self):
        data = {"key": "value"}
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        assert decode_data_value(encoded) == data

    def test_decodes_json_array(self):
        data = [1, 2, 3]
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        assert decode_data_value(encoded) == data


class TestDecodeDataFields:
    def test_decodes_all_data_fields(self):
        secret = {
            "metadata": {"name": "test"},
            "data": {
                "DB_HOST": base64.b64encode(b"localhost").decode(),
                "DB_PORT": base64.b64encode(b"5432").decode(),
            }
        }
        result = decode_data_fields(secret)
        assert result["data"]["DB_HOST"] == "localhost"
        assert result["data"]["DB_PORT"] == 5432  # numeric strings get JSON-parsed
        assert result["metadata"]["name"] == "test"


class TestJsonDecodeDataFields:
    def test_returns_json_string(self):
        secret = {
            "data": {
                "key": base64.b64encode(b"value").decode(),
            }
        }
        result = json_decode_data_fields(json.dumps(secret))
        parsed = json.loads(result)
        assert parsed["data"]["key"] == "value"


class TestYamlDecodeDataFields:
    def test_returns_yaml_string(self):
        secret = {
            "data": {
                "key": base64.b64encode(b"value").decode(),
            }
        }
        result = yaml_decode_data_fields(yaml.dump(secret))
        parsed = yaml.safe_load(result)
        assert parsed["data"]["key"] == "value"
