from __future__ import annotations

import os
from pathlib import Path

from bashrun.bash import bash

CRANE_VERSION = "v0.22.1"


def install_crane() -> None:
    print(f"Install crane {CRANE_VERSION}")
    sudo = "sudo " if os.geteuid() != 0 else ""
    archive = "go-containerregistry_Linux_x86_64.tar.gz"
    bash(f"curl -fsSLO https://github.com/google/go-containerregistry/releases/download/{CRANE_VERSION}/{archive}")
    bash(f"{sudo}tar -xzf {archive} -C /usr/local/bin crane")
    Path(archive).unlink(missing_ok=True)
