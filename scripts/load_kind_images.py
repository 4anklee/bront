"""Load local images into kind using an archive for the Docker host's platform.

Filtering the export avoids missing-digest failures with partially cached
multi-platform images in Docker Desktop's containerd image store.
Requires Docker with `image save --platform` support (API 1.48 or newer).
"""

import argparse
import subprocess
import tempfile
from pathlib import Path


def main(cluster: str, images: list[str]) -> None:
    platform = subprocess.check_output(
        ["docker", "image", "inspect", images[0], "--format", "{{.Os}}/{{.Architecture}}"],
        text=True,
    ).strip()
    with tempfile.TemporaryDirectory(prefix="inferenceguard-kind-images-") as directory:
        archive = str(Path(directory) / "images.tar")
        print(f"Exporting {platform} images; large images may take several minutes", flush=True)
        subprocess.run(
            ["docker", "image", "save", "--platform", platform, "--output", archive, *images],
            check=True,
        )
        subprocess.run(["kind", "load", "image-archive", "--name", cluster, archive], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default="inference-guard")
    parser.add_argument(
        "images", nargs="+", help="Local image tags; their platform must match the kind node"
    )
    args = parser.parse_args()
    main(args.cluster, args.images)
