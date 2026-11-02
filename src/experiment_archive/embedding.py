from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from .store import ArchiveError, canonical, digest, file_sha


MODEL_REPO = "Xenova/multilingual-e5-small"
MODEL_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
MODEL_FILES = ("config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
               "onnx/model_quantized.onnx")


def download_model(cache: Path) -> dict:
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise ArchiveError('Install semantic dependencies: pip install -e ".[semantic]"') from exc
    cache.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in MODEL_FILES:
        paths[name] = hf_hub_download(MODEL_REPO, name, revision=MODEL_REVISION,
                                     local_dir=cache, local_files_only=False)
    return {"repository": MODEL_REPO, "revision": MODEL_REVISION,
            "files": {name: {"path": path, "bytes": Path(path).stat().st_size,
                             "sha256": file_sha(Path(path))} for name, path in paths.items()}}


class E5Embedder:
    """Pinned multilingual E5, quantized ONNX, masked mean pooling, L2 normalized."""

    def __init__(self, cache: Path, threads: int = 2, batch_size: int = 4):
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise ArchiveError('Install semantic dependencies: pip install -e ".[semantic]"') from exc
        missing = [name for name in MODEL_FILES if not (cache / name).is_file()]
        if missing:
            raise ArchiveError("Model is not downloaded. Run: experiment-archive model download")
        self.tokenizer = Tokenizer.from_file(str(cache / "tokenizer.json"))
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id("<pad>") or 1, pad_token="<pad>")
        self.batch_size = batch_size
        config = ort.SessionOptions()
        config.intra_op_num_threads = max(1, threads)
        config.inter_op_num_threads = 1
        config.log_severity_level = 3
        self.session = ort.InferenceSession(str(cache / "onnx/model_quantized.onnx"),
                                           sess_options=config, providers=["CPUExecutionProvider"])
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.fingerprint = digest(canonical({"repo": MODEL_REPO, "revision": MODEL_REVISION,
            "model_sha256": file_sha(cache / "onnx/model_quantized.onnx"),
            "tokenizer_sha256": file_sha(cache / "tokenizer.json"),
            "pool": "masked_mean_l2_v1", "prefixes": ["query: ", "passage: "], "max_length": 512}))
        self.identity = {"repository": MODEL_REPO, "revision": MODEL_REVISION, "dimension": 384,
                         "fingerprint": self.fingerprint, "provider": "CPUExecutionProvider"}

    def chunks(self, text: str, max_tokens: int = 440, overlap: int = 60) -> list[str]:
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        encoding = self.tokenizer.encode(text, add_special_tokens=False)
        offsets = encoding.offsets
        if not offsets:
            return []
        result = []
        for start in range(0, len(offsets), max_tokens - overlap):
            end = min(len(offsets), start + max_tokens)
            part = text[offsets[start][0]:offsets[end - 1][1]].strip()
            if part:
                result.append(part)
            if end == len(offsets):
                break
        return result

    def encode(self, texts: list[str], query: bool = False) -> np.ndarray:
        if not texts:
            return np.empty((0, 384), dtype=np.float32)
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id("<pad>") or 1, pad_token="<pad>")
        results = []
        prefix = "query: " if query else "passage: "
        for offset in range(0, len(texts), self.batch_size):
            encoded = self.tokenizer.encode_batch([prefix + text for text in texts[offset:offset + self.batch_size]])
            arrays = {"input_ids": np.asarray([e.ids for e in encoded], dtype=np.int64),
                      "attention_mask": np.asarray([e.attention_mask for e in encoded], dtype=np.int64),
                      "token_type_ids": np.asarray([e.type_ids for e in encoded], dtype=np.int64)}
            outputs = self.session.run(None, {key: value for key, value in arrays.items() if key in self.input_names})
            hidden = outputs[0].astype(np.float32)
            mask = arrays["attention_mask"][..., None].astype(np.float32)
            vectors = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
            vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
            if vectors.shape[1] != 384 or not np.isfinite(vectors).all():
                raise ArchiveError("Embedding output is not finite or has unexpected dimension")
            results.append(vectors)
        return np.concatenate(results)
