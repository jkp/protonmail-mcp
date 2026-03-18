import logging
import os
import sys
import warnings

warnings.filterwarnings("ignore", message="TripleDES has been moved")

# Suppress HuggingFace/tokenizers noise
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)

# When running interactively, prevent any early log messages from polluting
# stderr before our full logging config runs (progress bars need clean stderr)
if sys.stderr.isatty():
    logging.root.handlers = []
