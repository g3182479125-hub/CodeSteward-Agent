"""Narrow exception for public authentication page templates."""


_OTHER_SECRET_MARKERS = (
    "secret",
    "credential",
    "api_key",
    "api-key",
    "apikey",
    "access_token",
    "access-token",
    "auth_token",
    "auth-token",
)


def is_public_password_template(parts: tuple[str, ...], index: int) -> bool:
    """Only HTML page templates may override a password-name path rejection.

    The remaining path components still go through their caller's usual
    generated-file, secret-name, suffix, and symlink checks.
    """

    if index != len(parts) - 1 or "templates" not in (
        part.casefold() for part in parts[:-1]
    ):
        return False
    name = parts[-1].casefold()
    return (
        name.endswith(".html")
        and not name.startswith(".env")
        and "password" in name
        and not any(marker in name for marker in _OTHER_SECRET_MARKERS)
    )
