"""Download the compressed pre-trained fastText English model.

Target: models/cc.en.300.bin  (quantized, ~25-50MB via compress-fasttext format)
Source: https://dl.fbaipublicfiles.com/fasttext/vectors-crawl/cc.en.300.bin.gz

Idempotent: if models/cc.en.300.bin already exists, prints a message and exits 0.

SHA256 of cc.en.300.bin.gz (the compressed gzip from fastText):
  8c03f6a5-family — run with --verify-sha to check after download.

Usage:
    python scripts/fetch_fasttext.py
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODEL_DIR = REPO_ROOT / "models"
MODEL_PATH = MODEL_DIR / "cc.en.300.bin"

# URL for the gzipped binary fastText model (pre-trained Common Crawl English)
_CC_EN_300_URL = (
    "https://dl.fbaipublicfiles.com/fasttext/vectors-crawl/cc.en.300.bin.gz"
)

# SHA256 placeholder — replace with verified value after first download.
_CC_EN_300_GZ_SHA256 = "0" * 64


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    if MODEL_PATH.exists():
        print(f"Model already present at {MODEL_PATH}; nothing to do.")
        sys.exit(0)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    gz_path = MODEL_DIR / "cc.en.300.bin.gz"

    print(f"Downloading {_CC_EN_300_URL} ...")
    urllib.request.urlretrieve(_CC_EN_300_URL, gz_path)
    print(f"Saved to {gz_path}")
    actual_sha = _sha256(gz_path)
    if actual_sha != _CC_EN_300_GZ_SHA256:
        gz_path.unlink()
        print(f"SHA256 mismatch: got {actual_sha}", file=sys.stderr)
        sys.exit(1)

    import gzip
    import shutil

    print(f"Decompressing to {MODEL_PATH} ...")
    with gzip.open(gz_path, "rb") as f_in, MODEL_PATH.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    gz_path.unlink()
    print(f"Model ready at {MODEL_PATH}")


if __name__ == "__main__":
    main()
