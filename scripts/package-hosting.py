"""Build separate, credential-free source bundles for Vercel and a Docker Space."""

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    frontend = ["vercel.json", ".vercelignore", "scripts/build-web.mjs", "web/index.html"]
    backend = ["Dockerfile", ".dockerignore", "pyproject.toml", "web/index.html"]
    backend += [
        str(path.relative_to(ROOT)) for path in sorted((ROOT / "src/rtl_agent").glob("*.py"))
    ]
    backend += [str(path.relative_to(ROOT)) for path in sorted((ROOT / "examples").glob("*.txt"))]
    for filename, prefix, paths in [
        ("rtl-agent-vercel.zip", "rtl-agent-vercel/", frontend),
        ("rtl-agent-backend.zip", "", backend),
    ]:
        with zipfile.ZipFile(output / filename, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in paths:
                archive.write(ROOT / name, prefix + name)
            if prefix:
                archive.write(ROOT / "docs/hosting.md", prefix + "SETUP.md")
            else:
                archive.write(ROOT / "deploy/huggingface/README.md", "README.md")
        with zipfile.ZipFile(output / filename) as archive:
            assert archive.testzip() is None
            assert not any(".env" in name or "/runs/" in name for name in archive.namelist())
        print(output / filename)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/hosting")
    package(parser.parse_args().output)
