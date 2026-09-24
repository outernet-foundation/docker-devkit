from __future__ import annotations

from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import pathspec
from bashrun.bash import bash, bash_output

from .documents import BakeDocument


def compute_service_shas(repo_root: Path, bake: BakeDocument) -> dict[str, str]:
    repo_root = repo_root.resolve()

    service_dirs: set[str] = set()
    for config in bake.services.values():
        if not config.build.tags:
            continue
        service_dirs.add(str(PurePosixPath(config.build.dockerfile).parent))

    tree_entries = bash_output("git ls-tree -r HEAD", cwd=repo_root).splitlines()

    dockerignore = repo_root / ".dockerignore"
    spec = pathspec.PathSpec.from_lines("gitignore", dockerignore.read_text().splitlines())

    allowed_entries: list[tuple[str, str, str]] = []
    for entry in tree_entries:
        meta, path = entry.split("\t", 1)
        if spec.match_file(path):
            continue
        mode, _type, obj_hash = meta.split()
        allowed_entries.append((mode, obj_hash, path))

    docker_prefix = "docker/"
    shared_entries: list[tuple[str, str, str]] = []
    per_service: dict[str, list[tuple[str, str, str]]] = {d: [] for d in service_dirs}

    for mode, obj_hash, path in allowed_entries:
        if path.startswith(docker_prefix):
            for service_dir in service_dirs:
                if path.startswith(service_dir + "/"):
                    per_service[service_dir].append((mode, obj_hash, path))
                    break
        else:
            shared_entries.append((mode, obj_hash, path))

    result: dict[str, str] = {}
    for service_dir in sorted(service_dirs):
        entries = shared_entries + per_service[service_dir]
        index_input = "\n".join(f"{mode} {obj_hash}\t{path}" for mode, obj_hash, path in entries) + "\n"

        with TemporaryDirectory() as tmpdir:
            index_env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
            bash("git update-index --index-info", cwd=repo_root, env=index_env, stdin_text=index_input)
            tree_hash = bash_output("git write-tree", cwd=repo_root, env=index_env).strip()

        result[PurePosixPath(service_dir).name.upper().replace("-", "_") + "_SHA"] = f"tree-{tree_hash}"

    return result
