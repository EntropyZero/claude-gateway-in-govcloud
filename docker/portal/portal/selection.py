"""Cost-center / team mapping parsing + download-selection validation."""


class SelectionError(Exception):
    pass


# Download platforms the portal serves: form value -> release-manifest
# platform key + the binary's name in the release (and in the ZIP).
PLATFORMS = {
    "windows": {"manifest_key": "win32-x64", "binary_name": "claude.exe"},
    "linux": {"manifest_key": "linux-x64", "binary_name": "claude"},
}


def validate_platform(platform):
    """Reject anything not a served platform. None (a pre-platform bookmark
    or a hand-built URL without the parameter) defaults to windows - the only
    platform the portal served before the selector existed."""
    if platform is None:
        return "windows"
    if platform not in PLATFORMS:
        raise SelectionError("platform %r is not an allowed value" % platform)
    return platform


# Mirrors Install-ClaudeCode.ps1's ValidatePattern('^[^,\s]*$') - a value
# that would break OTEL_RESOURCE_ATTRIBUTES parsing or the install.cmd
# argument - PLUS the shell metacharacters ($ " ` \ ' %) that the generated
# install.sh / install.cmd wrappers interpolate into quoted shell/batch
# context: values are admin-configured, but the boot-time validator must not
# accept strings the wrapper generators cannot safely quote.
_SHELL_UNSAFE = set('$"`\\\'%')


def clean_token(value):
    return (value != ""
            and not any(c.isspace() for c in value)
            and "," not in value
            and not any(c in _SHELL_UNSAFE for c in value))


def parse_cost_center_teams(raw):
    """Parse 'CC-1000:platform|data,CC-2000:security' into an ordered
    {cost_center: [teams]} dict. Every token must survive clean_token (the
    installer's own argument rules) and the delimiters (: | ,) are reserved,
    so a malformed entry raises ValueError at boot rather than rendering a
    broken or empty dropdown."""
    mapping = {}
    for entry in [x.strip() for x in raw.split(",") if x.strip()]:
        cc, sep, teams_raw = entry.partition(":")
        cc = cc.strip()
        teams = [t.strip() for t in teams_raw.split("|") if t.strip()]
        if not sep or not cc or not teams:
            raise ValueError(
                "PORTAL_COST_CENTER_TEAMS entry %r must look like "
                "'<cost-center>:<team>|<team>'" % entry)
        for token in [cc] + teams:
            if not clean_token(token) or ":" in token or "|" in token:
                raise ValueError(
                    "PORTAL_COST_CENTER_TEAMS value %r must have no spaces, "
                    "commas, colons, pipes, or shell metacharacters "
                    "($ \" ` \\ ' %%)" % token)
        if cc in mapping:
            raise ValueError("PORTAL_COST_CENTER_TEAMS lists cost center %r twice" % cc)
        if len(set(teams)) != len(teams):
            raise ValueError(
                "PORTAL_COST_CENTER_TEAMS lists a team twice under %r" % cc)
        mapping[cc] = teams
    return mapping


def validate_cost_center(cost_center, config):
    """Reject anything not a configured cost center (and, defensively,
    anything with whitespace/commas). Returns cost_center or raises."""
    if cost_center is None:
        raise SelectionError("cost_center is required")
    if not clean_token(cost_center):
        raise SelectionError("cost_center must not contain spaces or commas")
    if cost_center not in config.cost_center_teams:
        raise SelectionError("cost_center %r is not an allowed value" % cost_center)
    return cost_center


def validate_selection(team, cost_center, config):
    """Reject anything not in the configured mapping - the team must belong
    to the selected cost center, not merely appear somewhere in the config.
    Returns (team, cost_center) or raises."""
    if team is None or cost_center is None:
        raise SelectionError("both team and cost_center are required")
    cost_center = validate_cost_center(cost_center, config)
    if not clean_token(team):
        raise SelectionError("team must not contain spaces or commas")
    if team not in config.cost_center_teams[cost_center]:
        raise SelectionError("team %r is not an allowed value for cost center %r"
                             % (team, cost_center))
    return team, cost_center
