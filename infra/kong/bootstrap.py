"""Render reproducible Kong OSS configuration with Keycloak's local RSA public key."""
import json
import time
import urllib.request
from pathlib import Path

for attempt in range(90):
    try:
        with urllib.request.urlopen("http://keycloak:8080/realms/learning", timeout=5) as response:
            realm = json.load(response)
        break
    except (OSError, ValueError):
        time.sleep(2)
else:
    raise SystemExit("Keycloak realm unavailable")

public_key = "-----BEGIN PUBLIC KEY-----\n" + realm["public_key"] + "\n-----END PUBLIC KEY-----"
config = {
    "_format_version": "3.0",
    "services": [{"name": name, "url": f"http://{name}:8080/v1", "connect_timeout": 5000, "read_timeout": 20000, "write_timeout": 20000, "routes": [{"name": name, "paths": [f"/api/{name}"], "strip_path": True}]} for name in ["identity", "curriculum", "content", "assessment", "grading", "analytics"]],
    "consumers": [{"username": "keycloak", "jwt_secrets": [{"key": "http://localhost:8081/realms/learning", "algorithm": "RS256", "rsa_public_key": public_key}]}],
    "plugins": [
        {"name": "jwt", "config": {"claims_to_verify": ["exp"], "header_names": ["authorization"], "uri_param_names": [], "cookie_names": []}},
        {"name": "correlation-id", "config": {"header_name": "X-Correlation-ID", "generator": "uuid", "echo_downstream": True}},
        {"name": "rate-limiting", "config": {"minute": 120, "policy": "local"}},
        {"name": "request-size-limiting", "config": {"allowed_payload_size": 6}},
        {"name": "cors", "config": {"origins": ["http://localhost:4200"], "methods": ["GET", "POST", "PATCH", "DELETE", "OPTIONS"], "headers": ["Authorization", "Content-Type", "X-Correlation-ID"], "exposed_headers": ["X-Correlation-ID", "X-Request-ID"], "preflight_continue": False}},
    ],
}
Path("/output/kong.json").write_text(json.dumps(config), encoding="utf-8")
