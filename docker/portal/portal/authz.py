"""Group extraction + ANY-of group authorization."""


def groups_from_claims(id_claims, userinfo_claims):
    """Union the 'groups' claim from the ID token and (userinfo fallback) the
    userinfo response. Okta may deliver groups in either depending on the
    authorization server's claim config - mirror the gateway's
    userinfo_fallback: check both."""
    out = []
    for source in (id_claims or {}, userinfo_claims or {}):
        g = source.get("groups")
        if isinstance(g, str):
            g = [g]
        if isinstance(g, list):
            for item in g:
                if item not in out:
                    out.append(item)
    return out


def is_authorized(groups, access_groups):
    """True if the user belongs to ANY of the configured access groups.

    access_groups is a list; a bare string is coerced to a single-group list
    so callers (and tests) passing one group name still work - and never fall
    into set("name") iterating characters.
    """
    if isinstance(access_groups, str):
        access_groups = [access_groups]
    user = set(groups or [])
    return any(g in user for g in access_groups)
