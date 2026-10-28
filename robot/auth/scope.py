"""Resolve the single-user data scope from authenticated identity, not client input."""


def resolve_websocket_group(user_id: str, requested_group: str = "") -> str:
    """Only the authenticated user's own group is supported by this backend.

    Future shared-group support must validate membership server-side before
    accepting another scope; a query parameter is not an authorization grant.
    """
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("Missing authenticated user identity")
    if requested_group and requested_group != user_id:
        raise PermissionError("Group access denied")
    return user_id
