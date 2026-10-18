"""Deploy the allowlisted backend bundle; OpenAI credentials are never needed here."""

import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def deploy(space_id: str, token: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", space_id) or not token:
        raise ValueError("Configure HF_SPACE_ID and the scoped HF_TOKEN repository secret.")
    from huggingface_hub import HfApi

    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="rtl-agent-deploy-") as temporary:
        output = Path(temporary)
        subprocess.run(
            [sys.executable, str(root / "scripts/package-hosting.py"), "--output", temporary],
            check=True,
            capture_output=True,
        )
        files = output / "space"
        with zipfile.ZipFile(output / "rtl-agent-backend.zip") as archive:
            archive.extractall(files)
        HfApi(token=token).upload_folder(
            repo_id=space_id,
            repo_type="space",
            folder_path=str(files),
            delete_patterns=["*"],
            commit_message="Deploy RTL Agent backend",
        )


if __name__ == "__main__":
    try:
        deploy(os.environ.get("HF_SPACE_ID", ""), os.environ.get("HF_TOKEN", ""))
    except Exception:
        # Provider exception strings must not accidentally echo deployment credentials.
        raise SystemExit(
            "Backend deployment failed. Check the Space ID, scoped token and build logs."
        ) from None
    print("Backend files uploaded. Check the Space build status.")
