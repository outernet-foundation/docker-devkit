from os import environ
from pathlib import Path


def resolve_compose_profiles(env_file: Path) -> list[str]:
    file_values = parse_env_file(env_file)
    raw_profiles = environ.get("COMPOSE_PROFILES") or file_values.get("COMPOSE_PROFILES", "")
    profiles: list[str] = []
    for entry in raw_profiles.split(","):
        profile = entry.strip()
        if profile and profile not in profiles:
            profiles.append(profile)
    return profiles


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip("'\"")
    return result
