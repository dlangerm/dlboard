"""Routes for doing general api things."""

import dash
from flask import request

app = dash.get_app()


@app.server.route("/log", methods=["POST"])
def func() -> dict:
    req = request.get_json()
    assert req
    return {}


@app.server.route("/get", methods=["GET"])
def getstuff() -> str:
    return "got it"
