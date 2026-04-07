import logging
import os
import sys
import warnings

warnings.filterwarnings("ignore", message="TripleDES has been moved")

# Suppress HuggingFace/tokenizers noise
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)

# Constrain PyTorch CPU threads at the process level, before any model loads.
# Without this, torch defaults to ALL cores (e.g. 16 on a 24-core box),
# causing 400%+ CPU and thermal throttling during inference.
_torch_threads = os.environ.get("TORCH_THREADS") or str(min(4, os.cpu_count() or 1))
os.environ.setdefault("OMP_NUM_THREADS", _torch_threads)
os.environ.setdefault("MKL_NUM_THREADS", _torch_threads)
try:
    import torch

    torch.set_num_threads(int(_torch_threads))
    torch.set_num_interop_threads(int(_torch_threads))
except ImportError:
    pass

# When running interactively, prevent any early log messages from polluting
# stderr before our full logging config runs (progress bars need clean stderr)
if sys.stderr.isatty():
    logging.root.handlers = []
