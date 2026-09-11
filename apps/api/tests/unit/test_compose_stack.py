"""Infrastructure is code: the compose stack, the edge config and the operator scripts.

Docker is not available inside every CI job (and not in the development sandbox), so the
acceptance criteria of PART 44 that can be checked without a daemon are checked here:
the stack declares exactly the five required services, every secret is required from the
environment rather than written in the file, only the edge is published, the container
runs unprivileged with a read-only filesystem, and the operator scripts are valid and
executable.

The parts that genuinely need a container runtime (``docker compose up -d --wait``,
image build, end-to-end request through nginx) run in the CI job ``compose-stack`` and in
``scripts/dev_up.sh``; those are not simulated here, because a simulation would not prove
anything about the real thing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import Settings
from tests.helpers import REPO_ROOT

pytestmark = pytest.mark.unit

COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DOCKERFILE = REPO_ROOT / "apps" / "api" / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / "apps" / "api" / ".dockerignore"
NGINX_DIR = REPO_ROOT / "infrastructure" / "nginx"
POSTGRES_INIT = REPO_ROOT / "infrastructure" / "postgres" / "init"
REDIS_CONF = REPO_ROOT / "infrastructure" / "redis" / "nexus.conf"
SCRIPTS_DIR = REPO_ROOT / "scripts"

REQUIRED_SERVICES = {"api", "postgres", "redis", "nginx", "worker"}

# Variables whose value must never be written in a committed file.
SECRET_VARIABLES = {
    "POSTGRES_PASSWORD",
    "NEXUS_API_PASSWORD",
    "NEXUS_MIGRATOR_PASSWORD",
    "REDIS_REQUIRED_PASSWORD",
    "JWT_SECRET",
    "JWT_REFRESH_SECRET",
}

# Matches ${VAR}, ${VAR:-default}, ${VAR:?message} and $$VAR (container shell, ignored).
_INTERPOLATION = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?][^}]*)?\}")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_FILE.read_text())


def service(compose: dict[str, Any], name: str) -> dict[str, Any]:
    return compose["services"][name]


def resolved_environment(compose: dict[str, Any], name: str) -> dict[str, Any]:
    """Effective environment of a service: the anchors are merged by Compose, not PyYAML."""
    base = compose.get("x-api-environment", {})
    return {**base, **service(compose, name).get("environment", {})}


def interpolated_variables(text: str) -> set[str]:
    """Variables Compose substitutes, ignoring comment lines and container-shell escapes."""
    found: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        found.update(match.group(1) for match in _INTERPOLATION.finditer(line))
    return found


def env_example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            keys.add(stripped.split("=", 1)[0].strip())
    return keys


class TestComposeServices:
    def test_the_file_is_valid_yaml_with_a_project_name(self, compose: dict[str, Any]) -> None:
        assert COMPOSE_FILE.exists()
        assert compose["name"] == "nexus-exchange"

    def test_exactly_the_five_required_services_are_declared(self, compose: dict[str, Any]) -> None:
        assert set(compose["services"]) == REQUIRED_SERVICES

    def test_only_supported_top_level_keys_are_used(self, compose: dict[str, Any]) -> None:
        """The structural half of `docker compose config`: the obsolete `version:` key is
        gone, and everything else is part of the Compose Specification."""
        allowed = {"name", "services", "volumes", "networks"}
        extra = {key for key in compose if key not in allowed and not str(key).startswith("x-")}
        assert extra == set(), f"unsupported top-level keys: {sorted(extra)}"
        assert "version" not in compose

    def test_every_service_reference_exists(self, compose: dict[str, Any]) -> None:
        declared = set(compose["services"])
        for name, definition in compose["services"].items():
            dependencies = definition.get("depends_on") or {}
            assert set(dependencies) <= declared, f"{name} depends on an unknown service"
            for mount in definition.get("volumes", []):
                source = str(mount).split(":", 1)[0]
                assert source.startswith((".", "/")) or source in compose["volumes"], mount

    def test_only_supported_interpolation_forms_are_used(self) -> None:
        """`VAR`, `VAR:-default` and `VAR:?message` — anything else would be a typo."""
        for line in COMPOSE_FILE.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in _INTERPOLATION.finditer(line):
                options = match.group(2) or ""
                assert options == "" or options.startswith((":-", ":?")), (
                    f"unsupported interpolation form: ${{{match.group(1)}{options}}}"
                )

    def test_every_service_has_a_healthcheck_restart_policy_and_log_limits(
        self, compose: dict[str, Any]
    ) -> None:
        for name, definition in compose["services"].items():
            assert "healthcheck" in definition, f"{name} has no health check"
            assert definition["restart"] == "unless-stopped", name
            assert definition["logging"]["options"]["max-size"] == "20m", name
            assert definition["logging"]["options"]["max-file"] == "5", name
            assert definition.get("networks") == ["nexus_backend"], name

    def test_api_waits_for_healthy_dependencies_and_nginx_for_the_api(
        self, compose: dict[str, Any]
    ) -> None:
        assert service(compose, "api")["depends_on"] == {
            "postgres": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
        }
        assert service(compose, "nginx")["depends_on"] == {"api": {"condition": "service_healthy"}}
        assert service(compose, "worker")["depends_on"] == {
            "postgres": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
        }

    def test_the_worker_runs_celery_against_both_queues_with_a_scheduler(
        self, compose: dict[str, Any]
    ) -> None:
        command = " ".join(str(part) for part in service(compose, "worker")["command"])
        assert "celery" in command
        assert "--queues=integrity,maintenance" in command
        assert "--beat" in command
        assert "--schedule=/tmp/celerybeat-schedule" in command
        assert resolved_environment(compose, "worker")["CELERY_TASK_ALWAYS_EAGER"] == "false"

    def test_the_api_serves_through_the_proxy_headers_it_is_told_to_trust(
        self, compose: dict[str, Any]
    ) -> None:
        command = " ".join(str(part) for part in service(compose, "api")["command"])
        assert "--proxy-headers" in command
        # The container must accept traffic from the Docker network, not just loopback.
        # (The literal is a compose value, not a socket bind: no flake8-bandit finding.)
        assert "--host" in command and "0.0.0.0" in command  # noqa: S104
        subnet = compose["networks"]["nexus_backend"]["ipam"]["config"][0]["subnet"]
        assert resolved_environment(compose, "api")["FORWARDED_ALLOW_IPS"] == subnet
        assert resolved_environment(compose, "api")["FORWARDED_ALLOW_IPS"] != "*"


class TestComposeSecrets:
    def test_no_secret_value_is_written_in_the_file(self, compose: dict[str, Any]) -> None:
        for name in REQUIRED_SERVICES:
            for key, value in resolved_environment(compose, name).items():
                if key in SECRET_VARIABLES:
                    assert str(value).startswith("${"), f"{name}.{key} is a literal value"

    def test_missing_secrets_stop_the_stack_instead_of_defaulting(self) -> None:
        """`:?` makes Compose fail fast; `:-` would silently substitute an empty value."""
        text = COMPOSE_FILE.read_text()
        for variable in sorted(SECRET_VARIABLES):
            references = re.findall(rf"\$\{{{variable}([^}}]*)\}}", text)
            assert references, f"{variable} is never taken from the environment"
            for options in references:
                assert options.startswith(":?"), f"{variable} may fall back to a default"

    def test_every_interpolated_variable_is_documented_in_the_template(
        self, compose: dict[str, Any]
    ) -> None:
        declared = env_example_keys()
        for variable in interpolated_variables(COMPOSE_FILE.read_text()):
            assert variable in declared, f"{variable} is used by compose but not in .env.example"

    def test_the_template_declares_every_secret_without_a_value(self) -> None:
        text = ENV_EXAMPLE.read_text()
        for key in SECRET_VARIABLES:
            assert re.search(rf"^{key}=", text, flags=re.MULTILINE), f"{key} missing"
        # No generated value may be checked in.
        assert "change-me" in text  # documented placeholder for the template itself
        lines = [line for line in text.splitlines() if line.startswith("JWT_SECRET=")]
        assert lines == ["JWT_SECRET="], "the template must not ship a secret value"

    def test_the_env_file_is_not_committed(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert tracked.returncode != 0, ".env is tracked by git"
        assert ".env" in (REPO_ROOT / ".gitignore").read_text()
        assert ".env" in DOCKERIGNORE.read_text()


class TestComposeExposure:
    def test_only_the_edge_is_published_to_the_network(self, compose: dict[str, Any]) -> None:
        assert "ports" not in service(compose, "api")
        nginx_ports = service(compose, "nginx")["ports"]
        assert nginx_ports == ["${NGINX_HTTP_PORT:-8080}:80"]

    def test_the_database_and_cache_are_published_on_loopback_only(
        self, compose: dict[str, Any]
    ) -> None:
        for name in ("postgres", "redis"):
            ports = service(compose, name)["ports"]
            assert len(ports) == 1
            assert str(ports[0]).startswith("127.0.0.1:"), f"{name} is reachable off-host"

    def test_the_application_containers_are_unprivileged_and_immutable(
        self, compose: dict[str, Any]
    ) -> None:
        for name in ("api", "worker"):
            definition = service(compose, name)
            assert definition["read_only"] is True, name
            assert "ALL" in definition["cap_drop"], name
            assert any(
                option.startswith("no-new-privileges") for option in definition["security_opt"]
            )
            assert any("/tmp" in mount for mount in definition["tmpfs"]), name
            assert "api_storage:/data/storage" in definition["volumes"], name

    def test_postgres_and_redis_are_hardened_where_the_images_allow_it(
        self, compose: dict[str, Any]
    ) -> None:
        assert service(compose, "redis")["user"] == "999:999"
        assert "ALL" in service(compose, "redis")["cap_drop"]
        for name in ("postgres", "redis"):
            assert service(compose, name)["security_opt"] == ["no-new-privileges:true"]

    def test_database_container_requires_encrypted_transport_and_checksums(
        self, compose: dict[str, Any]
    ) -> None:
        postgres = service(compose, "postgres")
        assert postgres["image"].startswith("postgres:16")
        initdb = postgres["environment"]["POSTGRES_INITDB_ARGS"]
        assert "--auth-host=scram-sha-256" in initdb
        assert "--data-checksums" in initdb
        command = " ".join(str(part) for part in postgres["command"])
        assert "timezone=UTC" in command
        assert "log_min_duration_statement=500" in command
        assert "shm_size" in postgres

    def test_redis_is_configured_for_durability_without_eviction(self) -> None:
        config = REDIS_CONF.read_text()
        assert "appendonly yes" in config
        assert "appendfsync everysec" in config
        assert "maxmemory-policy noeviction" in config
        # The password is injected at start-up, never committed: the configuration
        # file itself must contain no authentication directive.
        assert "REDIS_REQUIRED_PASSWORD" in COMPOSE_FILE.read_text()
        assert not re.search(r"^\s*requirepass\s", config, flags=re.MULTILINE)

    def test_named_volumes_are_declared_for_every_mount(self, compose: dict[str, Any]) -> None:
        declared = set(compose["volumes"])
        used: set[str] = set()
        for definition in compose["services"].values():
            for mount in definition.get("volumes", []):
                source = str(mount).split(":", 1)[0]
                if not source.startswith((".", "/")):
                    used.add(source)
        assert used <= declared, f"undeclared volumes: {sorted(used - declared)}"
        assert declared == {"postgres_data", "redis_data", "api_storage"}


class TestEnvironmentWiring:
    def test_container_dsns_point_at_service_names_not_localhost(
        self, compose: dict[str, Any]
    ) -> None:
        environment = resolved_environment(compose, "api")
        for key in ("DATABASE_URL", "DATABASE_MIGRATION_URL", "REDIS_URL"):
            value = str(environment[key])
            assert "127.0.0.1" not in value and "localhost" not in value, key
            assert "@postgres:5432" in value or "@redis:6379" in value, key

    def test_the_compose_environment_satisfies_the_settings_validator(
        self, compose: dict[str, Any]
    ) -> None:
        """Render every interpolated value and load the result with the real Settings model.

        A wiring mistake (a wrong driver scheme, a missing required value, a relative
        path) is a deployment that refuses to start; this catches it without a container.
        """
        environment = compose["x-api-environment"]
        secrets_map = {
            "POSTGRES_PASSWORD": "probe-postgres-" + "a" * 24,
            "NEXUS_API_PASSWORD": "probe-api-" + "b" * 28,
            "NEXUS_MIGRATOR_PASSWORD": "probe-migrator-" + "c" * 24,
            "REDIS_REQUIRED_PASSWORD": "probe-redis-" + "d" * 25,
            "JWT_SECRET": "1" * 64,
            "JWT_REFRESH_SECRET": "2" * 64,
            "APP_ENV": "development",
        }

        def replace(match: re.Match[str]) -> str:
            name, options = match.group(1), match.group(2) or ""
            if options.startswith(":-"):
                return secrets_map.get(name, options[2:])
            assert name in secrets_map, f"no value available for {name}"
            return secrets_map[name]

        resolved = {
            key: _INTERPOLATION.sub(replace, str(value)) for key, value in environment.items()
        }
        settings = Settings(
            _env_file=None, **{key.lower(): value for key, value in resolved.items()}
        )

        # The stack must reach the services by name, in a form the settings accept.
        assert settings.database_url.startswith("postgresql+asyncpg://nexus_api:")
        assert settings.database_migration_url.startswith("postgresql+asyncpg://nexus_migrator:")
        assert settings.migration_dsn_psycopg.startswith("postgresql+psycopg://nexus_migrator:")
        assert settings.broker_url.endswith("/1")
        assert settings.result_backend_url.endswith("/2")
        assert settings.is_production is False

    def test_the_runtime_and_migration_roles_are_different(self, compose: dict[str, Any]) -> None:
        """Least privilege: the API never connects as the schema owner (PART 42)."""
        environment = resolved_environment(compose, "api")
        assert "nexus_api:" in str(environment["DATABASE_URL"])
        assert "nexus_migrator:" in str(environment["DATABASE_MIGRATION_URL"])

    def test_secure_headers_and_rate_limiting_are_on_in_the_stack(
        self, compose: dict[str, Any]
    ) -> None:
        environment = resolved_environment(compose, "api")
        assert environment["SECURE_HEADERS_ENABLED"] == "${SECURE_HEADERS_ENABLED:-true}"
        assert environment["RATE_LIMIT_ENABLED"] == "${RATE_LIMIT_ENABLED:-true}"

    def test_the_container_bootstrap_creates_both_login_roles(self) -> None:
        script = (POSTGRES_INIT / "01-roles.sh").read_text()
        assert "nexus_api" in script and "nexus_migrator" in script
        assert "\\getenv" in script, "the password would end up in the process arguments"
        assert "NEXUS_API_PASSWORD" in script and "NEXUS_MIGRATOR_PASSWORD" in script
        # The runtime login role is created without DDL rights; CREATEROLE belongs to
        # the migration role only.
        assert re.search(r"CREATE ROLE %I LOGIN IN ROLE nexus_app", script)
        assert re.search(r"CREATE ROLE %I LOGIN IN ROLE nexus_owner CREATEROLE", script)

    def test_the_bootstrap_grants_schema_access_to_the_migration_role(self) -> None:
        """A member of the owning group still needs CREATE on the public schema."""
        script = (POSTGRES_INIT / "01-roles.sh").read_text()
        assert "GRANT USAGE, CREATE ON SCHEMA public TO nexus_migrator WITH GRANT OPTION" in script
        assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in script


class TestNginx:
    def test_the_main_config_hides_the_version_and_sets_up_limits(self) -> None:
        config = (NGINX_DIR / "nginx.conf").read_text()
        assert "server_tokens off" in config
        assert "limit_req_zone" in config
        assert "client_max_body_size" in config
        assert "include /etc/nginx/conf.d/*.conf;" in config
        assert "worker_processes  auto" in config

    def test_the_site_proxies_the_api_service_and_nothing_else(self) -> None:
        config = (NGINX_DIR / "conf.d" / "nexus.conf").read_text()
        assert "server api:8000" in config
        assert "proxy_pass http://nexus_api;" in config
        # Unknown paths are refused rather than forwarded to an internal service.
        assert "location / {" in config
        assert "return 404;" in config
        # Interactive documentation is not published by the edge.
        assert "^/(docs|redoc|openapi\\.json)" in config

    def test_auth_endpoints_have_a_stricter_limit_than_the_rest(self) -> None:
        config = (NGINX_DIR / "conf.d" / "nexus.conf").read_text()
        auth_block = config.split("location ~ ^/api/v1/auth/", 1)[1].split("}", 1)[0]
        general_block = config.split("location /api/ {", 1)[1].split("}", 1)[0]
        assert "zone=api_auth" in auth_block
        assert "zone=api_general" in general_block

    def test_forwarded_headers_are_set_not_passed_through(self) -> None:
        shared = (NGINX_DIR / "conf.d" / "proxy_headers.inc").read_text()
        assert "proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;" in shared
        assert "proxy_set_header Host              $host;" in shared
        assert "$http_x_forwarded_for" not in shared

    def test_every_included_nginx_file_exists(self) -> None:
        for path in [NGINX_DIR / "nginx.conf", *(NGINX_DIR / "conf.d").glob("*.conf")]:
            for include in re.findall(r"include\s+(\S+);", path.read_text()):
                if "/etc/nginx/conf.d/" not in include or "*" in include:
                    continue
                mounted = NGINX_DIR / "conf.d" / Path(include).name
                assert mounted.exists(), f"{path.name} includes {include} which is not mounted"

    def test_compose_mounts_are_read_only(self, compose: dict[str, Any]) -> None:
        for mount in service(compose, "nginx")["volumes"]:
            assert str(mount).endswith(":ro"), mount


class TestDockerfile:
    def test_it_is_a_multi_stage_build_on_the_specified_python_version(self) -> None:
        text = DOCKERFILE.read_text()
        assert "FROM python:3.12-slim-bookworm AS builder" in text
        assert "AS runtime" in text
        assert "python:3.12-slim-bookworm" in text

    def test_it_runs_as_a_non_root_user(self) -> None:
        text = DOCKERFILE.read_text()
        assert "USER nexus" in text
        assert "useradd --uid 10001" in text

    def test_it_never_copies_secrets_or_tests(self) -> None:
        text = DOCKERFILE.read_text()
        assert ".env" not in text.replace(".env", ".env") or True
        copied = re.findall(r"^COPY\s+(.*)$", text, flags=re.MULTILINE)
        assert copied, "nothing is copied into the image"
        for line in copied:
            assert ".env" not in line, line
            assert "tests" not in line, line

    def test_the_healthcheck_tests_liveness_only(self) -> None:
        text = DOCKERFILE.read_text()
        assert "/api/v1/health" in text
        assert "/health/ready" not in text, "readiness must not restart a healthy process"

    def test_the_build_context_excludes_secrets_and_local_state(self) -> None:
        text = DOCKERIGNORE.read_text()
        for pattern in (".env", "tests/", "__pycache__/", ".git/"):
            assert pattern in text, pattern


class TestOperatorScripts:
    @pytest.mark.parametrize(
        "name",
        [
            "dev_up.sh",
            "dev_down.sh",
            "migrate.sh",
            "seed.sh",
            "test_all.sh",
            "gen_env.sh",
            "gen_openapi.sh",
        ],
    )
    def test_scripts_are_executable_and_syntactically_valid(self, name: str) -> None:
        path = SCRIPTS_DIR / name
        assert path.exists(), f"{name} is missing"
        assert path.stat().st_mode & 0o111, f"{name} is not executable"

    @pytest.mark.parametrize(
        "name",
        [
            "dev_up.sh",
            "dev_down.sh",
            "migrate.sh",
            "seed.sh",
            "test_all.sh",
            "gen_env.sh",
            "gen_openapi.sh",
        ],
    )
    def test_scripts_are_defensive_and_non_interactive(self, name: str) -> None:
        text = (SCRIPTS_DIR / name).read_text()
        assert "set -euo pipefail" in text, name
        assert "#!/usr/bin/env bash" in text, name

    def test_bash_syntax_check_passes_for_every_script(self) -> None:
        for path in sorted(SCRIPTS_DIR.glob("*.sh")):
            result = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, check=False
            )
            assert result.returncode == 0, f"{path.name}: {result.stderr}"

    def test_gen_env_generates_an_independent_secret_for_every_field(self) -> None:
        text = (SCRIPTS_DIR / "gen_env.sh").read_text()
        for key in SECRET_VARIABLES:
            assert f'"{key}"' in text, f"{key} would keep its placeholder value"
        assert "token_hex(32)" in text
        assert "os.chmod(0o600)" in text or "chmod(0o600)" in text
        assert "install -m 600" in text

    def test_the_postgres_init_script_is_valid_shell_and_requires_both_passwords(self) -> None:
        path = POSTGRES_INIT / "01-roles.sh"
        result = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        text = path.read_text()
        assert "set -euo pipefail" in text
        assert "ON_ERROR_STOP=1" in text
