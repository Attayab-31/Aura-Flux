"""Refresh the packaged Kaggle notebook and metadata in the worker directory."""
import json
import os
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target_dir = root / "workers" / "kaggle_fluxstory"
target_dir.mkdir(parents=True, exist_ok=True)
source = target_dir / "fluxstory_kaggle_auto_batch_worker.ipynb"
metadata_path = target_dir / "kernel-metadata.json"
notebook = json.loads(source.read_text(encoding="utf-8"))
cell = notebook["cells"][3]
code = "".join(cell["source"])
code = code.replace('_config_value("KAGGLE_KERNEL_ID", "unknown")',
                    '_config_value("KAGGLE_KERNEL_ID", "owner/fluxstory-on-demand-worker")')
code = code.replace('if width < 16 or height < 16 or width > 1024 or height > 1024:',
                    'if width < 16 or height < 16 or width > 2048 or height > 2048 or width % 16 or height % 16 or width * height > 4_194_304:')
code = code.replace('logger.exception("GPU request %s failed (%s)", request_id, type(exc).__name__)',
                    'logger.error("GPU request failed (%s)", type(exc).__name__)')
code = code.replace('logger.error("Could not report failure for request %s: %s", request_id, type(callback_exc).__name__)',
                    'logger.error("Could not report GPU request failure (%s)", type(callback_exc).__name__)')
cell["source"] = code.splitlines(keepends=True)
serialized = json.dumps(notebook, ensure_ascii=False, indent=1) + "\n"
source.write_text(serialized, encoding="utf-8")
existing_metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
kernel_id = (os.getenv("KAGGLE_KERNEL_ID", "").strip() or
             existing_metadata.get("id") or "owner/fluxstory-on-demand-worker")
metadata = {
    "id": kernel_id,
    "title": "FluxStory On-Demand Batch Worker",
    "code_file": source.name,
    "language": "python",
    "kernel_type": "notebook",
    "is_private": True,
    "enable_gpu": True,
    "enable_internet": True,
    "dataset_sources": [],
    "competition_sources": [],
    "kernel_sources": [],
}
metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
