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
# compress-fasttext (CompressedFastTextKeyedVectors) serialization format
# (freq-pruned to 400K/100K vocab + product quantization).
# Published as a GitHub release asset of the compress-fasttext repo —
# the storage.yandexcloud.net mirror previously referenced here 404s.
MODEL_URL = (
    "https://github.com/avidale/compress-fasttext/releases/download/"
    "gensim-4-draft/ft_cc.en.300_freqprune_400K_100K_pq_300.bin"
)

# SHA256 of the release asset, verified on download 2026-07-05. The
# download aborts (and the partial file is removed) on any mismatch.
EXPECTED_SHA256 = (
    "ec32a88dbc1170652d99ca512108ff73594a76bdbda2b4840ebf96833812e696"
)


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    if MODEL_PATH.exists():
        sha = _sha256(MODEL_PATH)
        if sha != EXPECTED_SHA256:
            print(
                f"[fetch_fasttext] existing model at {MODEL_PATH} has "
                f"SHA256 {sha}, expected {EXPECTED_SHA256} — stale or "
                "corrupt artifact (e.g. from the retired mirror). "
                "Delete the file and re-run.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"[fetch_fasttext] model already present at {MODEL_PATH} "
            "(SHA256 verified) — skipping download"
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
        if sha != EXPECTED_SHA256:
            raise RuntimeError(
                f"SHA256 mismatch: expected {EXPECTED_SHA256}, got {sha}"
            )
        tmp_path.rename(MODEL_PATH)
        print(f"[fetch_fasttext] saved to {MODEL_PATH}")
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        print(f"[fetch_fasttext] download failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
