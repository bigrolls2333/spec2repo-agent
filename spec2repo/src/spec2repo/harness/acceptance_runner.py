"""Trusted stdlib-only HTTP checks. Executed in a separate, workspace-free container.

Never import generated Python, execute project tests, or trust an application's
stdout as a verdict. Inputs are frozen here; only bounded observations leave.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def request(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8000" + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    try:
        response = urllib.request.build_opener(NoRedirect).open(req, timeout=2)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("Response exceeds limit")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            value = None
        return response.code, value


def run(case, phase, saved):
    for _ in range(50):
        try:
            request("GET", "/")
            break
        except (OSError, ValueError):
            time.sleep(0.1)
    else:
        return {"startup_failed": True, "checks": [], "saved": []}
    checks, records = [], []

    def check(ac, reqs, hidden, expected, action):
        try:
            passed = bool(action())
            actual = "matched" if passed else "HTTP status or response contract mismatch"
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
            passed, actual = False, "Response unavailable or invalid"
        checks.append({"acceptance_id": ac, "requirement_ids": reqs,
                       "held_out": hidden, "passed": passed,
                       "expected": expected, "actual": actual})

    if phase == "restart":
        for record in saved:
            ac = "A3" if case == "notes" else "AC-PERSIST"
            reqs = ["R2"] if case == "notes" else ["REQ-PERSIST"]
            check(ac, reqs, record["hidden"], "Previously created resource survives process restart",
                  lambda r=record: request("GET", r["path"]) == (200, r["body"]))
        if not saved:
            check("A3" if case == "notes" else "AC-PERSIST",
                  ["R2"] if case == "notes" else ["REQ-PERSIST"], False,
                  "A resource exists for restart verification", lambda: False)
        return {"checks": checks, "saved": []}

    for variant in range(3):
        hidden = variant > 0
        if case == "notes":
            value = ["hello notes", "边界🙂" * 20, "z" * 200][variant]
            status, body = request("POST", "/notes", {"text": value})
            valid = (status == 201 and isinstance(body, dict)
                     and type(body.get("id")) is int and body.get("text") == value)
            check("A1", ["R1"], hidden, "201 with integer id and unchanged text", lambda v=valid: v)
            check("A2", ["R1"], hidden, "Empty and over-limit text return 422",
                  lambda: all(request("POST", "/notes", {"text": text})[0] == 422
                              for text in ("", "x" * 201)))
            check("A4", ["R2"], hidden, "Missing resource returns 404",
                  lambda: request("GET", "/notes/987654321")[0] == 404)
            if valid:
                path = f"/notes/{body['id']}"
                check("A3", ["R2"], hidden, "Read returns created note",
                      lambda p=path, b=body: request("GET", p) == (200, b))
                records.append({"path": path, "body": body, "hidden": hidden})
        else:
            _, before = request("GET", "/tasks")
            baseline = before.get("total", 0) if isinstance(before, dict) else 0
            title = ["Write tests", "边界任务", "t" * 120][variant]
            status, body = request("POST", "/tasks", {"title": "  " + title + "  "})
            valid = (status == 201 and isinstance(body, dict)
                     and isinstance(body.get("id"), str) and bool(body["id"])
                     and body.get("title") == title and body.get("status") == "todo")
            check("AC-CREATE", ["REQ-CREATE"], hidden,
                  "201 with unique string id, trimmed title and todo", lambda v=valid: v)

            def invalid_titles(baseline=baseline, valid=valid):
                ok = all(request("POST", "/tasks", value)[0] == 422 for value in
                         ({"title": "   "}, {"title": "t" * 121}, {}))
                _, page = request("GET", "/tasks")
                return ok and isinstance(page, dict) and page.get("total") == baseline + int(valid)

            check("AC-INVALID-TITLE", ["REQ-CREATE"], hidden,
                  "Invalid titles return 422 without insertion", invalid_titles)
            check("AC-INVALID-PAGE", ["REQ-LIST"], hidden,
                  "Invalid pagination returns 422",
                  lambda: all(request("GET", "/tasks?" + query)[0] == 422
                              for query in ("offset=-1", "limit=0", "limit=101")))
            created = [request("POST", "/tasks", {"title": f"page-{variant}-{i}"})
                       for i in range(3)]

            def pagination(baseline=baseline, created=created, body=body):
                code, page = request("GET", f"/tasks?offset={baseline + 2}&limit=1")
                return (code == 200 and isinstance(page, dict)
                        and all(c == 201 and isinstance(b, dict) for c, b in created)
                        and page.get("items") == [created[1][1]]
                        and page.get("total") == baseline + 4
                        and len({b["id"] for _, b in created} | {body["id"]}) == 4)

            check("AC-LIST", ["REQ-LIST"], hidden, "Stable creation-order pagination and total",
                  pagination)
            if valid:
                path = "/tasks/" + urllib.parse.quote(body["id"], safe="")
                check("AC-READ", ["REQ-READ"], hidden, "Read existing 200 and missing 404",
                      lambda p=path, b=body: request("GET", p) == (200, b)
                      and request("GET", "/tasks/missing-acceptance-id")[0] == 404)

                def transitions(path=path, body=body):
                    responses = [request("PATCH", path, {"status": state}) for state in
                                 ("done", "doing", "done", "done", "todo")]
                    code, final = request("GET", path)
                    body["status"] = "done"
                    return ([r[0] for r in responses] == [409, 200, 200, 200, 409]
                            and code == 200 and final == body
                            and responses[1][1].get("status") == "doing"
                            and responses[2][1].get("status") == "done")

                check("AC-TRANSITIONS", ["REQ-STATUS"], hidden,
                      "Only todo to doing to done or unchanged; conflicts return 409", transitions)
                check("AC-INVALID-STATUS", ["REQ-STATUS"], hidden,
                      "Unknown enum 422 and missing resource 404",
                      lambda p=path: request("PATCH", p, {"status": "unknown"})[0] == 422
                      and request("PATCH", "/tasks/missing-acceptance-id",
                                  {"status": "todo"})[0] == 404)
                records.append({"path": path, "body": body, "hidden": hidden})
    return {"checks": checks, "saved": records}


if __name__ == "__main__":
    try:
        result = run(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        result = {"startup_failed": False, "contract_failed": True, "checks": [], "saved": []}
    print(json.dumps(result, ensure_ascii=True))
