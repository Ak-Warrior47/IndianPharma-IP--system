"""
Smoke tests — cover core flows without needing a real DB or Twilio.
Run: pytest tests/ -v
"""
import os
import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture(scope="session")
def app():
    from app import app as flask_app, db, init_db
    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with flask_app.app_context():
        db.create_all()
        init_db()
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_client(client):
    """Client pre-logged in as admin."""
    client.post("/login", data={"email": os.environ.get("ADMIN_EMAIL", "admin@pharmaip.com"),
                                "password": os.environ.get("ADMIN_PASSWORD", "admin123")})
    return client


# ── Auth ───────────────────────────────────────────────────────────────────────

def test_login_page_loads(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert b"Login" in r.data or b"login" in r.data.lower()


def test_login_bad_credentials(client):
    r = client.post("/login", data={"email": "nobody@x.com", "password": "wrong"})
    assert r.status_code == 200
    assert b"Invalid" in r.data or b"invalid" in r.data.lower()


def test_login_success_redirects(client):
    r = client.post("/login",
                    data={"email": os.environ.get("ADMIN_EMAIL", "admin@pharmaip.com"),
                          "password": os.environ.get("ADMIN_PASSWORD", "admin123")},
                    follow_redirects=False)
    assert r.status_code in (302, 303)


def test_dashboard_requires_login(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302


def test_admin_dashboard_requires_admin(client):
    r = client.get("/admin_dashboard", follow_redirects=False)
    assert r.status_code == 302


# ── Health ─────────────────────────────────────────────────────────────────────

def test_health_check(client):
    r = client.get("/health")
    assert r.status_code in (200, 500)
    data = r.get_json()
    assert "status" in data
    assert "database" in data


# ── Admin pages (authenticated) ────────────────────────────────────────────────

def test_admin_dashboard_loads(auth_client):
    r = auth_client.get("/admin_dashboard")
    assert r.status_code == 200


def test_admin_add_user(auth_client):
    r = auth_client.post("/admin/add_user", data={
        "name": "Test Picker",
        "email": "testpicker@pharmaip.com",
        "password": "testpass1",
        "staff_type": "picker",
        "role": "Picker",
    }, follow_redirects=True)
    assert r.status_code == 200


def test_admin_bulk_import_page(auth_client):
    r = auth_client.get("/admin/bulk_import")
    assert r.status_code in (200, 404)


# ── Analytics unit tests ───────────────────────────────────────────────────────

def test_build_analytics_empty():
    from app import build_analytics
    result = build_analytics([], "picker")
    assert result is None


def test_build_analytics_safe_div():
    from app import safe_div
    assert safe_div(10, 2) == 5.0
    assert safe_div(10, 0) == 0.0
    assert safe_div(10, 0, default=-1) == -1


def test_grade_from_score():
    from app import grade_from_score
    assert grade_from_score(95) == "EXCELLENT"
    assert grade_from_score(80) == "GOOD"
    assert grade_from_score(65) == "AVERAGE"
    assert grade_from_score(40) == "NEEDS WORK"


def test_bill_validation_evaluate():
    from app import BillValidation
    bv = BillValidation()
    bv.picker_id = 1
    bv.picker_count = 100
    bv.checker1_id = 2; bv.checker1_count = 100
    bv.checker2_id = 3; bv.checker2_count = 100
    bv.checker3_id = 4; bv.checker3_count = 100
    status, wrong = bv.evaluate()
    assert status == "confirmed"
    assert wrong == []


def test_bill_validation_mismatch():
    from app import BillValidation
    bv = BillValidation()
    bv.picker_id = 1;   bv.picker_count = 100
    bv.checker1_id = 2; bv.checker1_count = 100
    bv.checker2_id = 3; bv.checker2_count = 100
    bv.checker3_id = 4; bv.checker3_count = 99   # outlier
    status, wrong = bv.evaluate()
    assert status == "mismatch"
    assert 4 in wrong


# ── Validations ────────────────────────────────────────────────────────────────

def test_validations_page_requires_login(client):
    r = client.get("/validations", follow_redirects=False)
    assert r.status_code == 302


def test_validations_page_loads_authed(auth_client):
    r = auth_client.get("/validations")
    assert r.status_code == 200
