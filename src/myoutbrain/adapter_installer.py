from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Literal, cast

from myoutbrain.core_types import ConfigurationConflict
from myoutbrain.domain_protocol import execute_domain_request
from myoutbrain.library import KnowledgeWorkflow

AdapterClient = Literal["codex", "opencode", "claude-code"]
ADAPTER_CLIENTS: tuple[AdapterClient, ...] = (
    "codex",
    "opencode",
    "claude-code",
)
ADAPTER_MINIMUM_PROTOCOL_VERSION = {"major": 2, "minor": 0}
ADAPTER_MAXIMUM_PROTOCOL_VERSION = {"major": 2, "minor": 1}
ADAPTER_CAPABILITIES = (
    "instance_status.v1",
    "review_list.v1",
    "review_payload.v1",
    "review_decision.v1",
    "review_effect.create_derived_memory.v1",
    "review_effect.create_canonical_memory.v1",
    "review_effect.create_source_backed_canonical_memory.v1",
    "review_effect.revise_canonical_memory.v1",
    "review_effect.create_human_archive.v1",
    "review_effect.create_research_thread.v1",
)
_CODEX_START = "# BEGIN MYOUTBRAIN MANAGED ADAPTER"
_CODEX_END = "# END MYOUTBRAIN MANAGED ADAPTER"
_CODEX_BLOCK = re.compile(
    rf"(?:\r?\n)?{re.escape(_CODEX_START)}.*?{re.escape(_CODEX_END)}(?:\r?\n)?",
    re.DOTALL,
)
_MANAGED_MARKER = "MYOUTBRAIN_ADAPTER_MANAGED_V1"
_MISSING = object()
_SKILL = f"""---
name: myoutbrain
description: Use the shared MyOutBrain private instance through its negotiated MCP domain protocol.
---

<!-- {_MANAGED_MARKER} -->

# MyOutBrain entrance

Use `myoutbrain_gateway` for all private-instance operations. Declare the
adapter protocol range and only the capabilities this client actually
understands. Never read SQLite, the object store, Vault, or generated views
directly. Before approving a proposal, inspect its complete `approval_effect`
and declare the matching `review_effect.<type>.v1` capability. Every semantic
write must carry a stable idempotency key and the observed `expected_version`.
"""


@dataclass(frozen=True)
class AdapterPaths:
    config: Path
    skills: Path


class AdapterInstaller:
    def __init__(
        self,
        client: AdapterClient,
        instance_root: Path | None,
        *,
        config_path: Path | None = None,
        skills_dir: Path | None = None,
        registry_path: Path | None = None,
    ) -> None:
        self._client = client
        self._registry_path = (
            registry_path or Path.home() / ".myoutbrain" / "instances.json"
        ).resolve()
        self._instance_root = (
            instance_root.resolve()
            if instance_root is not None
            else _read_primary_instance(self._registry_path)
        )
        defaults = _default_paths(client)
        self._paths = AdapterPaths(
            config=(config_path or defaults.config).resolve(),
            skills=(skills_dir or defaults.skills).resolve(),
        )

    def install(self) -> dict[str, object]:
        KnowledgeWorkflow(self._instance_root).instance_status()
        self._assert_config_installable()
        self._assert_skill_installable()
        _claim_primary_instance(self._registry_path, self._instance_root)
        self._write_config(self._installed_config())
        _atomic_write_text(self._skill_path, _SKILL)
        return self._result("installed")

    def check(self) -> tuple[dict[str, object], bool]:
        config_matches = self._config_matches()
        skill_matches = (
            self._skill_path.is_file()
            and self._skill_path.read_text(encoding="utf-8") == _SKILL
        )
        response, _ = execute_domain_request(
            self._instance_root,
            {
                "protocol": {
                    "minimum": dict(ADAPTER_MINIMUM_PROTOCOL_VERSION),
                    "maximum": dict(ADAPTER_MAXIMUM_PROTOCOL_VERSION),
                },
                "client": {
                    "name": self._client,
                    "capabilities": list(ADAPTER_CAPABILITIES),
                },
                "operation": "instance.status",
                "parameters": {},
            },
        )
        protocol_compatible = response.get("ok") is True
        negotiated = response.get("protocol_version")
        installed = config_matches and skill_matches and protocol_compatible
        return (
            {
                **self._result("installed" if installed else "not-installed"),
                "config_matches": config_matches,
                "skill_matches": skill_matches,
                "protocol": {
                    "compatible": protocol_compatible,
                    "client": {
                        "minimum": dict(ADAPTER_MINIMUM_PROTOCOL_VERSION),
                        "maximum": dict(ADAPTER_MAXIMUM_PROTOCOL_VERSION),
                    },
                    "negotiated": negotiated,
                    "server": response.get("server_protocol_version"),
                },
                "capabilities": {
                    "client": list(ADAPTER_CAPABILITIES),
                    "server": response.get("server_capabilities", []),
                    "common": sorted(
                        set(ADAPTER_CAPABILITIES).intersection(
                            cast(list[str], response.get("server_capabilities", []))
                        )
                    ),
                },
            },
            installed,
        )

    def uninstall(self) -> dict[str, object]:
        self._assert_config_uninstallable()
        self._assert_skill_uninstallable()
        self._write_config(self._uninstalled_config())
        if self._skill_path.is_file():
            self._skill_path.unlink()
        _remove_empty_directory(self._skill_path.parent)
        return self._result("uninstalled")

    @property
    def _skill_path(self) -> Path:
        return self._paths.skills / "myoutbrain" / "SKILL.md"

    def _result(self, status: str) -> dict[str, object]:
        return {
            "client": self._client,
            "status": status,
            "config": str(self._paths.config),
            "skill": str(self._skill_path),
            "instance": str(self._instance_root),
            "registry": str(self._registry_path),
        }

    def _assert_config_installable(self) -> None:
        if self._client == "codex":
            self._installed_config()
            return
        entry = self._json_entry_if_present()
        if entry is not _MISSING and not _is_managed_json_entry(entry):
            raise ConfigurationConflict(
                f"{self._client} already has an unmanaged myoutbrain MCP server"
            )

    def _assert_config_uninstallable(self) -> None:
        if self._client == "codex":
            content = _read_text_if_present(self._paths.config)
            unmanaged = _CODEX_BLOCK.sub("\n", content)
            if "[mcp_servers.myoutbrain]" in unmanaged:
                raise ConfigurationConflict(
                    "Codex has an unmanaged myoutbrain MCP server"
                )
            return
        entry = self._json_entry_if_present()
        if entry is not _MISSING and not _is_managed_json_entry(entry):
            raise ConfigurationConflict(
                f"{self._client} has an unmanaged myoutbrain MCP server"
            )

    def _assert_skill_installable(self) -> None:
        if not self._skill_path.is_file():
            return
        content = _read_text_if_present(self._skill_path)
        if _MANAGED_MARKER not in content:
            raise ConfigurationConflict(
                f"an unmanaged myoutbrain skill already exists: {self._skill_path}"
            )

    def _assert_skill_uninstallable(self) -> None:
        if not self._skill_path.is_file():
            return
        if _MANAGED_MARKER not in _read_text_if_present(self._skill_path):
            raise ConfigurationConflict(
                f"refusing to remove unmanaged myoutbrain skill: {self._skill_path}"
            )

    def _json_entry_if_present(self) -> object:
        data = _read_json_object(self._paths.config)
        container_name = _json_container_name(self._client)
        raw_container = data.get(container_name)
        if raw_container is None:
            return _MISSING
        if not isinstance(raw_container, dict) or not all(
            isinstance(key, str) for key in raw_container
        ):
            raise ConfigurationConflict(
                f"{self._client} {container_name} configuration is invalid"
            )
        return raw_container.get("myoutbrain", _MISSING)

    def _installed_config(self) -> str:
        if self._client == "codex":
            existing = _read_text_if_present(self._paths.config)
            unmanaged = _CODEX_BLOCK.sub("\n", existing).rstrip()
            if "[mcp_servers.myoutbrain]" in unmanaged:
                raise ConfigurationConflict(
                    "Codex already has an unmanaged myoutbrain MCP server"
                )
            block = _codex_block(self._instance_root)
            return f"{unmanaged}\n\n{block}".lstrip("\n")
        data = _read_json_object(self._paths.config)
        container_name = _json_container_name(self._client)
        raw_container = data.get(container_name, {})
        if not isinstance(raw_container, dict) or not all(
            isinstance(key, str) for key in raw_container
        ):
            raise ConfigurationConflict(
                f"{self._client} {container_name} configuration is invalid"
            )
        container = cast(dict[str, object], raw_container)
        container["myoutbrain"] = self._json_mcp_entry()
        data[container_name] = container
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def _uninstalled_config(self) -> str:
        if self._client == "codex":
            return _CODEX_BLOCK.sub("\n", _read_text_if_present(self._paths.config)).lstrip("\n")
        data = _read_json_object(self._paths.config)
        container_name = _json_container_name(self._client)
        raw_container = data.get(container_name)
        if isinstance(raw_container, dict):
            container = cast(dict[object, object], raw_container)
            container.pop("myoutbrain", None)
            if container:
                data[container_name] = container
            else:
                data.pop(container_name, None)
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def _config_matches(self) -> bool:
        if not self._paths.config.is_file():
            return False
        if self._client == "codex":
            content = _read_text_if_present(self._paths.config)
            matches = _CODEX_BLOCK.findall(content)
            return len(matches) == 1 and _codex_block(self._instance_root).strip() in matches[0]
        data = _read_json_object(self._paths.config)
        container_name = _json_container_name(self._client)
        container = data.get(container_name)
        return isinstance(container, dict) and container.get("myoutbrain") == self._json_mcp_entry()

    def _json_mcp_entry(self) -> dict[str, object]:
        arguments = _mcp_arguments(self._instance_root)
        if self._client == "opencode":
            return {
                "type": "local",
                "command": [sys.executable, *arguments],
                "enabled": True,
                "environment": {_MANAGED_MARKER: "1"},
            }
        return {
            "type": "stdio",
            "command": sys.executable,
            "args": arguments,
            "env": {_MANAGED_MARKER: "1"},
        }

    def _write_config(self, content: str) -> None:
        _atomic_write_text(self._paths.config, content)


def _default_paths(client: AdapterClient) -> AdapterPaths:
    home = Path.home()
    if client == "codex":
        root = Path(os.environ.get("CODEX_HOME", home / ".codex"))
        return AdapterPaths(root / "config.toml", root / "skills")
    if client == "opencode":
        explicit = os.environ.get("OPENCODE_CONFIG")
        config = Path(explicit) if explicit else home / ".config" / "opencode" / "opencode.json"
        return AdapterPaths(config, config.parent / "skills")
    return AdapterPaths(home / ".claude.json", home / ".claude" / "skills")


def _json_container_name(client: AdapterClient) -> str:
    if client == "opencode":
        return "mcp"
    if client == "claude-code":
        return "mcpServers"
    raise AssertionError("Codex does not use a JSON adapter container")


def _is_managed_json_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    values = cast(dict[object, object], entry)
    environment = values.get("environment", values.get("env"))
    return (
        isinstance(environment, dict)
        and cast(dict[object, object], environment).get(_MANAGED_MARKER) == "1"
    )


def _mcp_arguments(instance_root: Path) -> list[str]:
    return ["-m", "myoutbrain", "mcp", "--root", str(instance_root)]


def _codex_block(instance_root: Path) -> str:
    command = json.dumps(sys.executable)
    arguments = ", ".join(json.dumps(value) for value in _mcp_arguments(instance_root))
    return (
        f"{_CODEX_START}\n"
        "[mcp_servers.myoutbrain]\n"
        f"command = {command}\n"
        f"args = [{arguments}]\n"
        "enabled = true\n"
        f"{_CODEX_END}\n"
    )


def _read_text_if_present(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as error:
        raise ConfigurationConflict(f"cannot read adapter configuration: {path}") from error


def _read_json_object(path: Path) -> dict[str, object]:
    content = _read_text_if_present(path)
    if not content.strip():
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise ConfigurationConflict(f"invalid adapter configuration: {path}") from error
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        raise ConfigurationConflict(f"invalid adapter configuration: {path}")
    return cast(dict[str, object], data)


def _read_primary_instance(registry_path: Path) -> Path:
    data = _read_json_object(registry_path)
    primary = data.get("primary_instance")
    if data.get("schema_version") != 1 or not isinstance(primary, str) or not primary:
        raise ConfigurationConflict(
            "no primary MyOutBrain instance is registered; install once with --root"
        )
    return Path(primary).resolve()


def _claim_primary_instance(registry_path: Path, instance_root: Path) -> None:
    if registry_path.is_file():
        registered = _read_primary_instance(registry_path)
        if registered != instance_root:
            raise ConfigurationConflict(
                "a different primary MyOutBrain instance is already registered"
            )
        return
    _atomic_write_text(
        registry_path,
        json.dumps(
            {"primary_instance": str(instance_root), "schema_version": 1},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except (FileNotFoundError, OSError):
        pass
