#!/usr/bin/env python3
"""Download the compress-fasttext English model into models/.

Idempotent: if the model file already exists the script exits without
making any network request. Safe to call from CI when model may or may
not be present — integration tests that need the model are gated with
@pytest.mark.skipif(not _model_present(), ...) in tests/test_embeddings.py.

Usage:
    python scripts/fetch_fasttext.py
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
import urllib.request

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODEL_PATH = _REPO_ROOT / "models" / "cc.en.300-compress.bin"

# Pre-compressed English Common Crawl fastText model saved in the
# compress-fasttext (CompressedFastTextKeyedVectors) serialization format.
# Produced by: compress_fasttext.prune_ft_freq(gensim_ft_model, pq=True)
# Source repository: https://github.com/avidale/compress-fasttext
MODEL_URL = (
    "https://storage.yandexcloud.net/nlp/compress-fasttext/models/"
    "cc.en.300-compress.bin"
)


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    if MODEL_PATH.exists():
        print(
            f"[fetch_fasttext] model already present at {MODEL_PATH} "
            "— skipping download"
        )
        return

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"[fetch_fasttext] source URL: {MODEL_URL}")
    print(f"[fetch_fasttext] downloading …")

    tmp_path = MODEL_PATH.with_suffix(".tmp")
    try:
        urllib.request.urlretrieve(MODEL_URL, tmp_path)
        sha = _sha256(tmp_path)
        print(f"[fetch_fasttext] SHA256: {sha}")
        tmp_path.rename(MODEL_PATH)
        print(f"[fetch_fasttext] saved to {MODEL_PATH}")
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        print(f"[fetch_fasttext] download failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
