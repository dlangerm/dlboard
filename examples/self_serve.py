"""Serve a custom set of plugins from a python script."""

from dlboard.serve import run_production_server

if __name__ == "__main__":
    run_production_server(
        host="0.0.0.0",
        port=8050,
        workers=1,
        blocking_threads=1,
        plugins_target="dlboard.plugins:LOCAL_DEPLOYMENT",
    )
