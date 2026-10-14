"""Write-only configuration credentials at the HTTP boundary.

Internal loaders still receive the real configuration. Browser round trips carry
empty values and boolean status only; empty/omitted credentials keep the saved
value. Explicit removal uses ``_clear_secrets`` JSON pointers, never a mask that
could accidentally become the password.
"""
from copy import deepcopy
from urllib.parse import urlsplit


class ConfigSecretError(ValueError):
    pass


def _secret(path: tuple[str, ...], key: str, value=None) -> bool:
    name = key.lower().replace("-", "_")
    if name in {"password", "passwd", "basic_pass", "cookie", "cookies", "token",
                "secret", "authorization", "proxy_authorization", "api_key",
                "access_key", "client_secret", "headers", "params"}:
        return True
    if name.endswith(("_password", "_passwd", "_token", "_secret", "_api_key")):
        return True
    if name == "value" and "auth" in path:
        return True
    if name == "body" and ("remote" in path or "feed" in path):
        return True
    # A supplier/export URL can authenticate through an opaque path, not only
    # an obvious ?token=. Treat the entire configured endpoint as a credential.
    if "export_url" in name or name == "feed_url":
        return True
    if name in {"url", "urls"} and ("feeds" in path or "feed" in path or "remote" in path):
        return True
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        try:
            parsed = urlsplit(value)
            return bool(parsed.username or parsed.password or parsed.query or parsed.fragment)
        except ValueError:
            return True
    return False


def _empty(value):
    return {} if isinstance(value, dict) else [] if isinstance(value, list) else ""


def public_config(config: dict) -> dict:
    """Return a detached JSON value with no reusable configured credentials."""
    def visit(value, path):
        if isinstance(value, list):
            return [visit(item, path + (str(index),)) for index, item in enumerate(value)]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for key, item in value.items():
            if key == "_clear_secrets" or key.endswith("_configured"):
                continue
            if _secret(path, key, item):
                result[key] = _empty(item)
                result[key + "_configured"] = bool(item)
            else:
                result[key] = visit(item, path + (key,))
        return result
    return visit(config, ())


def config_patch(current: dict, payload: dict) -> dict:
    """Prepare an HTTP patch, preserving hidden credentials inside replaced maps.

    Non-secret replacement/deletion semantics remain those of config_io. Removing
    a whole feed source still removes it; an existing source's omitted auth map
    retains its credentials. ``_clear_secrets`` is consumed, never persisted.
    """
    clear = payload.get("_clear_secrets", [])
    if not isinstance(clear, list) or any(not isinstance(path, str) for path in clear):
        raise ConfigSecretError("_clear_secrets must contain JSON pointer strings")

    def has_secrets(value, path):
        if not isinstance(value, dict):
            return False
        for key, item in value.items():
            if (_secret(path, key, item) and bool(item)) or has_secrets(item, path + (key,)):
                return True
        return False

    def visit(old, new, path):
        result = {}
        old = old if isinstance(old, dict) else {}
        for key, item in new.items():
            if key == "_clear_secrets" or key.endswith("_configured"):
                continue
            previous = old.get(key)
            if _secret(path, key, item) or _secret(path, key, previous):
                if item in (None, "", {}, []):
                    result[key] = deepcopy(previous) if key in old else deepcopy(item)
                elif isinstance(item, str) and item in {"********", "[REDACTED]", "••••••••"}:
                    raise ConfigSecretError("Enter a new credential, not a masked placeholder")
                else:
                    result[key] = deepcopy(item)
            elif isinstance(item, dict):
                result[key] = visit(previous, item, path + (key,))
            else:
                result[key] = deepcopy(item)
        # Preserve credentials omitted by a form's normalized auth object, but
        # do not resurrect removed feed definitions from a replacement sources.
        if path and path[-1] != "sources":
            for key, item in old.items():
                if key not in new and not key.endswith("_configured"):
                    if _secret(path, key, item) and item:
                        result[key] = deepcopy(item)
                    elif has_secrets(item, path + (key,)):
                        result[key] = deepcopy(item)
        if "feeds" in path and path[-1:] == ("remote",):
            # Changing endpoint representation must not leave a hidden previous
            # list taking precedence over the newly entered single URL.
            if new.get("url") and not new.get("urls") and old.get("urls"):
                result["urls"] = []
            elif new.get("urls") and not new.get("url") and old.get("url"):
                result["url"] = ""
        return result

    result = visit(current, payload, ())
    for pointer in clear:
        if not pointer.startswith("/") or len(pointer) > 1000:
            raise ConfigSecretError("Invalid secret field pointer")
        parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
        if any(not part or part in {"__proto__", "_clear_secrets"} for part in parts):
            raise ConfigSecretError("Invalid secret field pointer")
        old = current
        for part in parts[:-1]:
            old = old.get(part, {}) if isinstance(old, dict) else {}
        key = parts[-1]
        value = old.get(key) if isinstance(old, dict) else None
        if not _secret(tuple(parts[:-1]), key, value):
            raise ConfigSecretError("Only credential fields can be explicitly cleared")
        target = result
        for index, part in enumerate(parts[:-1]):
            if part not in target:
                # config_io replaces nested maps: start from the current branch
                # when clearing a secret without resending the rest of that map.
                branch = current
                for parent in parts[:index + 1]:
                    branch = branch.get(parent, {}) if isinstance(branch, dict) else {}
                target[part] = deepcopy(branch) if isinstance(branch, dict) else {}
            if not isinstance(target[part], dict):
                raise ConfigSecretError("Invalid secret field pointer")
            target = target[part]
        target[key] = _empty(value)
    return result
