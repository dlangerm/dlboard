"""DLBoard main entrypoint."""

from pkgutil import extend_path

# `dlboard` is split across two distributions (`dlboard-client` ships this file, `dlboard` ships
# `serve/`, `plugins/`, ... with no `__init__.py`). Installed into one site-packages, the two simply
# merge on disk. A hermetic build like Bazel puts each wheel on its own `sys.path` entry instead,
# and a regular package (this one) found first hides every other `dlboard/` directory, namespace
# portions included -- so `dlboard.serve` would be unimportable. Extending `__path__` pulls those
# directories back in.
__path__ = extend_path(__path__, __name__)
