import json
import tomllib
from pathlib import Path


def test_supabase_config_and_cli_belong_to_backend_and_disable_email_sending():
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "supabase/config.toml").read_text(encoding="utf-8"))
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert package["devDependencies"]["supabase"] == "3.1.0"
    assert config["project_id"] == "cashly-backend-local"
    assert config["auth"]["email"]["enable_confirmations"] is False
    assert config["local_smtp"]["enabled"] is False
    assert "cashly" not in config["api"]["schemas"]
    assert config["db"]["migrations"]["enabled"] is False
    assert "external" not in config["auth"]
