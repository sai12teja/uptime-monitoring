"""Push/passive monitor check-in endpoint (design doc: additional monitor
types). Separate Blueprint from api.py's -- the secret token in the URL is
the auth, not the shared X-API-Key, since callers are cron jobs doing a
plain `curl <url>`, not scripts holding the admin API key. auth.py's
session gate exempts this blueprint the same way it exempts "api".
"""
import time

from flask import Blueprint, jsonify

import db
import monitor_engine

bp = Blueprint("push", __name__)


@bp.route("/push/<token>", methods=["GET", "POST"])
def push_ping(token):
    monitor = db.get_monitor_by_push_token(token)
    if monitor is None:
        return jsonify({"error": "not found"}), 404
    event = monitor_engine.record_push_ping(monitor, time.time())
    monitor_engine.handle_tick_events([event] if event else [])
    return jsonify({"status": "ok"})


def register_push(server):
    server.register_blueprint(bp)
