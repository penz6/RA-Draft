"""Exercise the real Connect GroupMe form and CSP with a mocked provider page."""

import os
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# This standalone browser test must never use a deployed database or credentials.
os.environ.update(
    DATABASE_PATH=str(Path(tempfile.mkdtemp(prefix="groupme-browser-")) / "test.db"),
    SECRET_KEY="browser-only-secret-0123456789abcdef0123456789abcdef",
    PUBLIC_HOST="ci.local", GOOGLE_CLIENT_ID="preview.apps.googleusercontent.com",
    GOOGLE_CLIENT_SECRET="preview-only", ADMIN_EMAILS="admin@rwu.edu", PROXY_HOPS="0",
    DEADLINES_JSON_URL="", GROUPME_CLIENT_ID="browser-test-client", MAIL_ENABLED="0",
)
from test_admin_management import AdminManagementTestCase  # noqa: E402
from core import app  # noqa: E402


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *_args):
        pass


def run():
    app.config.update(SESSION_COOKIE_SECURE=False, TRUSTED_HOSTS=["ci.local", "127.0.0.1"])
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    checks = 0
    try:
        with sync_playwright() as playwright:
            launch = {"headless": True}
            if os.environ.get("CHROMIUM_PATH"):
                launch["executable_path"] = os.environ["CHROMIUM_PATH"]
            browser = playwright.chromium.launch(**launch)
            for role in ("ADMIN", "HRA"):
                for width in (1440, 390):
                    fixture = AdminManagementTestCase()
                    fixture.setUp()
                    hall = fixture.add_building("Maple Hall")
                    uid = fixture.add_user(sub="browser", email="browser@rwu.edu", name="Browser Staff",
                                           role=role, building_id=hall)
                    fixture.login_as(uid)
                    context = browser.new_context(viewport={"width": width, "height": 1000})
                    cookie = fixture.client.get_cookie("ra_draft_session", domain="ci.local")
                    context.add_cookies([{"name": "ra_draft_session", "value": cookie.value, "url": origin}])
                    # All external traffic is intercepted; no GroupMe account,
                    # real authorization, bot creation, or messages are involved.
                    context.route("https://**", lambda route: route.abort())
                    provider_requests = []
                    def provider(route):
                        provider_requests.append(route.request)
                        route.fulfill(status=200, content_type="text/html",
                                      body="<title>GroupMe authorization fixture</title>")
                    context.route("https://oauth.groupme.com/oauth/authorize?*", provider)
                    page = context.new_page()
                    errors = []
                    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                    page.goto(origin + "/deadlines", wait_until="networkidle")
                    page.get_by_role("button", name="Connect GroupMe", exact=True).click()
                    try:
                        page.wait_for_url("https://oauth.groupme.com/oauth/authorize?*", timeout=5000)
                    except Exception:
                        raise AssertionError(f"{role}/{width}: GroupMe navigation blocked: {errors}") from None
                    page.wait_for_load_state("domcontentloaded")
                    assert page.title() == "GroupMe authorization fixture"
                    assert len(provider_requests) == 1
                    request = provider_requests[0]
                    assert request.method == "GET" and request.post_data is None
                    params = parse_qs(urlsplit(request.url).query)
                    assert params["client_id"] == ["browser-test-client"]
                    assert len(params["state"][0]) >= 43
                    assert not any("Content Security Policy" in message for message in errors)
                    checks += 1
                    context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print(f"Passed {checks} live GroupMe connect browser checks (Admin/HRA, desktop/mobile).")


if __name__ == "__main__":
    run()
