"""REST API for the monitoring backend (PRD §11). register_api(server) mounts
routes on any Flask app -- in production that's Dash's app.server, so the
API shares the dashboard's process/port rather than standing up a second one.

Gated by a static API key (PRD §16 gap 6) rather than the dashboard's session
login, since callers are scripts/curl, not a logged-in browser. A Blueprint
scopes the before_request check to just these routes, leaving the Dash UI's
own session-based gate (auth.py) untouched.
"""
import hmac
import os

from flask import Blueprint, jsonify, request

import data
import db

bp = Blueprint("api", __name__)


@bp.before_request
def _require_api_key():
    configured = os.environ.get("API_KEY")
    if not configured:
        return jsonify({"error": "API key not configured"}), 503
    # hmac.compare_digest, not != -- a plain string compare short-circuits
    # on the first differing byte, leaking key-prefix-match timing to a
    # network attacker (security review H1). This is the API's only auth gate.
    if not hmac.compare_digest(request.headers.get("X-API-Key", ""), configured):
        return jsonify({"error": "unauthorized"}), 401


def _monitor_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "type": row["type"],
        "keyword": row["keyword"],
        "interval_sec": row["interval_sec"],
        "port": row["port"],
        "push_token": row["push_token"],
        "retries": row["retries"],
        "timeout_sec": row["timeout_sec"],
        "http_method": row["http_method"],
        "http_body": row["http_body"],
        "http_body_encoding": row["http_body_encoding"],
        "status": row["status"],
        "active": bool(row["active"]),
        "last_checked_at": row["last_checked_at"],
        "last_response_ms": row["last_response_ms"],
        "created_at": row["created_at"],
    }


def _valid_url(url, target_type):
    # Only website/crm checks are actual HTTP requests -- tcp/dns targets
    # are a bare host/hostname, not a URL with a scheme.
    if target_type in ("website", "crm"):
        return url.startswith("http://") or url.startswith("https://")
    return True


def register_api(server):
    @bp.route("/monitors", methods=["GET"])
    def api_list_monitors():
        return jsonify([_monitor_dict(m) for m in db.list_monitors()])

    @bp.route("/monitors", methods=["POST"])
    def api_create_monitor():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        url = (body.get("url") or "").strip()
        target_type = body.get("type") or "website"
        keyword = (body.get("keyword") or "").strip() or None
        interval_sec = body.get("interval_sec") or 60
        port = body.get("port")
        retries = body.get("retries")
        timeout_sec = body.get("timeout_sec")
        http_method = body.get("http_method") or "GET"
        http_body = body.get("http_body")
        http_body_encoding = body.get("http_body_encoding") or "json"
        if not name:
            return jsonify({"error": "name is required"}), 400
        if target_type != "push" and not url:
            return jsonify({"error": "url is required"}), 400
        if not _valid_url(url, target_type):
            return jsonify({"error": "url must start with http:// or https://"}), 400
        if target_type == "tcp" and not port:
            return jsonify({"error": "port is required for tcp monitors"}), 400
        monitor_id = db.add_monitor(name, url, target_type, keyword=keyword, interval_sec=interval_sec,
                                     port=port, retries=retries, timeout_sec=timeout_sec,
                                     http_method=http_method, http_body=http_body,
                                     http_body_encoding=http_body_encoding)
        return jsonify(_monitor_dict(db.get_monitor(monitor_id))), 201

    @bp.route("/monitors/<int:monitor_id>", methods=["PUT"])
    def api_edit_monitor(monitor_id):
        row = db.get_monitor(monitor_id)
        if row is None:
            return jsonify({"error": "not found"}), 404
        body = request.get_json(silent=True) or {}
        name = (body.get("name", row["name"]) or "").strip()
        url = (body.get("url", row["url"]) or "").strip()
        keyword = body.get("keyword", row["keyword"])
        if isinstance(keyword, str):
            keyword = keyword.strip() or None
        interval_sec = body.get("interval_sec", row["interval_sec"])
        port = body.get("port", row["port"])
        retries = body.get("retries", row["retries"])
        timeout_sec = body.get("timeout_sec", row["timeout_sec"])
        http_method = body.get("http_method", row["http_method"]) or "GET"
        http_body = body.get("http_body", row["http_body"])
        http_body_encoding = body.get("http_body_encoding", row["http_body_encoding"]) or "json"
        if not name:
            return jsonify({"error": "name is required"}), 400
        if row["type"] != "push" and not url:
            return jsonify({"error": "url is required"}), 400
        if not _valid_url(url, row["type"]):
            return jsonify({"error": "url must start with http:// or https://"}), 400
        if row["type"] == "tcp" and not port:
            return jsonify({"error": "port is required for tcp monitors"}), 400
        data.update_target(monitor_id, name, url, keyword, interval_sec, port=port,
                            retries=retries, timeout_sec=timeout_sec, http_method=http_method,
                            http_body=http_body, http_body_encoding=http_body_encoding)
        return jsonify(_monitor_dict(db.get_monitor(monitor_id)))

    @bp.route("/monitors/<int:monitor_id>", methods=["DELETE"])
    def api_delete_monitor(monitor_id):
        if db.get_monitor(monitor_id) is None:
            return jsonify({"error": "not found"}), 404
        data.delete_target(monitor_id)
        return "", 204

    @bp.route("/status", methods=["GET"])
    def api_status():
        return jsonify(data.get_targets())

    @bp.route("/incidents", methods=["GET"])
    def api_list_incidents():
        return jsonify(data.get_incidents(
            status=request.args.get("status"),
            target_id=request.args.get("target_id", type=int),
            since=request.args.get("since", type=float),
            until=request.args.get("until", type=float),
        ))

    @bp.route("/incidents/<int:incident_id>", methods=["GET"])
    def api_incident_detail(incident_id):
        detail = data.get_incident_detail(incident_id)
        if detail is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(detail)

    @bp.route("/incidents/<int:incident_id>/acknowledge", methods=["POST"])
    def api_acknowledge(incident_id):
        inc = db.get_incident(incident_id)
        if inc is None:
            return jsonify({"error": "not found"}), 404
        if inc["resolved"] is not None:
            return jsonify({"error": "incident already resolved"}), 409
        who = ((request.get_json(silent=True) or {}).get("who") or "").strip()
        if not who:
            return jsonify({"error": "who is required"}), 400
        data.acknowledge_incident(incident_id, who)
        return jsonify(data.get_incident_detail(incident_id))

    @bp.route("/targets/<int:target_id>/detail", methods=["GET"])
    def api_target_detail(target_id):
        detail = data.get_target_detail(target_id)
        if detail is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(detail)

    @bp.route("/server/metrics", methods=["GET"])
    def api_server_metrics():
        return jsonify(data.get_server_metrics())

    server.register_blueprint(bp)
