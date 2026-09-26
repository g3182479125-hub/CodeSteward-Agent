"""Narrow exception for public authentication page templates."""


_PUBLIC_PASSWORD_PAGES = frozenset({"forgot_password.html", "reset_password.html"})


def is_public_password_template(parts: tuple[str, ...], index: int) -> bool:
    """Only HTML page templates may override a password-name path rejection.

    The remaining path components still go through their caller's usual
    generated-file, secret-name, suffix, and symlink checks.
    """

    if index != len(parts) - 1 or "templates" not in (
        part.casefold() for part in parts[:-1]
    ):
        return False
    return parts[-1].casefold() in _PUBLIC_PASSWORD_PAGES
