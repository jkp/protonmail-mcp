import logging
import sys
import warnings

warnings.filterwarnings("ignore", message="TripleDES has been moved")

# When running interactively, prevent any early log messages from polluting
# stderr before our full logging config runs (progress bars need clean stderr)
if sys.stderr.isatty():
    logging.root.handlers = []
