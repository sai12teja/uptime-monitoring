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
import whm_security

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


def _monitor_dict(row, include_token=False):
    d = {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "type": row["type"],
        "keyword": row["keyword"],
        "interval_sec": row["interval_sec"],
        "port": row["port"],
        "retries": row["retries"],
        "timeout_sec": row["timeout_sec"],
        "http_method": row["http_method"],
        "http_body": row["http_body"],
        "http_body_encoding": row["http_body_encoding"],
        "expected_status": row["expected_status"],
        "status": row["status"],
        "active": bool(row["active"]),
        "last_checked_at": row["last_checked_at"],
        "last_response_ms": row["last_response_ms"],
        "created_at": row["created_at"],
    }
    # A push_token is a bearer secret -- anyone holding it can forge check-ins.
    # Return it only on the single-resource response for the monitor you just
    # created/edited, never in the bulk list (which would dump every secret).
    if include_token:
        d["push_token"] = row["push_token"]
    return d


def _valid_url(url, target_type):
    # Only website/crm checks are actual HTTP requests -- tcp/dns targets
    # are a bare host/hostname, not a URL with a scheme.
    if target_type in ("website", "crm"):
        return url.startswith("http://") or url.startswith("https://")
    return True


_VALID_TYPES = {"website", "crm", "tcp", "dns", "push"}
_VALID_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_MIN_INTERVAL_SEC = 30    # interval_sec: 1 would hammer the target every tick
_MAX_TIMEOUT_SEC = 120


def _coerce_int(value, name, minimum=None, maximum=None, allow_none=False):
    """Return (int|None, error). Rejects bools, floats and strings -- a string
    interval_sec would otherwise be stored and silently stop the monitor from
    ever running."""
    if value is None:
        return None, (None if allow_none else f"{name} is required")
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{name} must be an integer"
    if minimum is not None and value < minimum:
        return None, f"{name} must be >= {minimum}"
    if maximum is not None and value > maximum:
        return None, f"{name} must be <= {maximum}"
    return value, None


def _validate_monitor(target_type, interval_sec, port, retries, timeout_sec, http_method,
                      expected_status=None):
    """Validate/coerce the enum + numeric fields shared by create and edit.
    Returns (fields_dict, error). Input is untrusted JSON straight off the
    wire, so a bad type/method or a string number must 400, not reach db."""
    if target_type not in _VALID_TYPES:
        return None, f"type must be one of {sorted(_VALID_TYPES)}"
    method = (http_method or "GET").upper()
    if method not in _VALID_HTTP_METHODS:
        return None, f"http_method must be one of {sorted(_VALID_HTTP_METHODS)}"
    interval_sec, err = _coerce_int(interval_sec, "interval_sec", minimum=_MIN_INTERVAL_SEC)
    if err:
        return None, err
    port, err = _coerce_int(port, "port", minimum=1, maximum=65535, allow_none=True)
    if err:
        return None, err
    retries, err = _coerce_int(retries, "retries", minimum=0, maximum=10, allow_none=True)
    if err:
        return None, err
    timeout_sec, err = _coerce_int(timeout_sec, "timeout_sec", minimum=1, maximum=_MAX_TIMEOUT_SEC, allow_none=True)
    if err:
        return None, err
    expected_status, err = _coerce_int(expected_status, "expected_status", minimum=100, maximum=599, allow_none=True)
    if err:
        return None, err
    return {"interval_sec": interval_sec, "port": port, "retries": retries,
            "timeout_sec": timeout_sec, "http_method": method, "expected_status": expected_status}, None


def register_api(server):
    @bp.route("/monitors", methods=["GET"])
    def api_list_monitors():
        return jsonify([_monitor_dict(m) for m in db.list_monitors()])  # no push_token in the bulk list

    @bp.route("/monitors", methods=["POST"])
    def api_create_monitor():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        url = (body.get("url") or "").strip()
        target_type = body.get("type") or "website"
        keyword = (body.get("keyword") or "").strip() or None
        interval_sec = body.get("interval_sec") or db.DEFAULT_CHECK_INTERVAL_SEC
        port = body.get("port")
        retries = body.get("retries")
        timeout_sec = body.get("timeout_sec")
        http_method = body.get("http_method") or "GET"
        http_body = body.get("http_body")
        http_body_encoding = body.get("http_body_encoding") or "json"
        expected_status = body.get("expected_status")
        if not name:
            return jsonify({"error": "name is required"}), 400
        if target_type != "push" and not url:
            return jsonify({"error": "url is required"}), 400
        if not _valid_url(url, target_type):
            return jsonify({"error": "url must start with http:// or https://"}), 400
        if target_type == "tcp" and not port:
            return jsonify({"error": "port is required for tcp monitors"}), 400
        fields, err = _validate_monitor(target_type, interval_sec, port, retries, timeout_sec,
                                        http_method, expected_status)
        if err:
            return jsonify({"error": err}), 400
        monitor_id = db.add_monitor(name, url, target_type, keyword=keyword,
                                     interval_sec=fields["interval_sec"], port=fields["port"],
                                     retries=fields["retries"], timeout_sec=fields["timeout_sec"],
                                     http_method=fields["http_method"], http_body=http_body,
                                     http_body_encoding=http_body_encoding,
                                     expected_status=fields["expected_status"])
        return jsonify(_monitor_dict(db.get_monitor(monitor_id), include_token=True)), 201

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
        expected_status = body.get("expected_status", row["expected_status"])
        if not name:
            return jsonify({"error": "name is required"}), 400
        if row["type"] != "push" and not url:
            return jsonify({"error": "url is required"}), 400
        if not _valid_url(url, row["type"]):
            return jsonify({"error": "url must start with http:// or https://"}), 400
        if row["type"] == "tcp" and not port:
            return jsonify({"error": "port is required for tcp monitors"}), 400
        fields, err = _validate_monitor(row["type"], interval_sec, port, retries, timeout_sec,
                                        http_method, expected_status)
        if err:
            return jsonify({"error": err}), 400
        data.update_target(monitor_id, name, url, keyword, fields["interval_sec"], port=fields["port"],
                            retries=fields["retries"], timeout_sec=fields["timeout_sec"],
                            http_method=fields["http_method"], http_body=http_body,
                            http_body_encoding=http_body_encoding,
                            expected_status=fields["expected_status"])
        return jsonify(_monitor_dict(db.get_monitor(monitor_id), include_token=True))

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

    @bp.route("/whm-security", methods=["POST"])
    def api_whm_security():
        # Push path: a root-owned agent on the WHM box POSTs its own security
        # counts (Imunify360 malware / cPHulk / ModSecurity), so Rovix alerts
        # on them without ever holding WHM credentials. Same API-key gate as the
        # rest of this blueprint.
        body = request.get_json(silent=True) or {}
        host = (body.get("host") or "").strip()
        accepted, err = whm_security.ingest_push(host, body.get("counts"))
        if err:
            return jsonify({"error": err}), 400
        return jsonify({"host": host, "accepted": accepted}), 200

    server.register_blueprint(bp)
