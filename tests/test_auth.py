"""Staff authentication gate tests (POS_PASSWORD)."""
import os, sqlite3, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import create_app


def make_app(password):
    """Create an app with POS_PASSWORD set for the duration of creation."""
    os.environ["POS_PASSWORD"] = password
    try:
        return create_app({"TESTING": True, "DATABASE": tempfile.mktemp(suffix=".sqlite3")})
    finally:
        os.environ.pop("POS_PASSWORD", None)


def seed_fixture(app):
    with app.app_context():
        app.init_db()
        db = sqlite3.connect(app.config["DATABASE"]); db.row_factory = sqlite3.Row
        db.execute("INSERT INTO categories(name) VALUES ('Drinks')")
        db.execute("INSERT INTO products(category_id,name,price,stock_tracked,stock_qty,active) VALUES (1,'Latte',450,1,20,1)")
        db.commit(); db.close()
    return app


def test_menu_public_when_protected():
    app = make_app("espresso123")
    cl = app.test_client()
    r = cl.get("/menu")
    assert r.status_code == 200
    print("PASS menu stays public")


def test_health_public_when_protected():
    app = make_app("espresso123")
    cl = app.test_client()
    assert cl.get("/health").status_code == 200
    print("PASS health stays public")


def test_staff_routes_redirect_to_login():
    app = make_app("espresso123")
    cl = app.test_client()
    for path in ["/", "/pos", "/orders", "/products", "/reports", "/settings", "/receipt", "/export"]:
        r = cl.get(path)
        assert r.status_code == 302 and "/login" in r.headers.get("Location", ""), f"{path} not protected"
    print("PASS staff routes protected")


def test_wrong_password_rejected():
    app = make_app("espresso123")
    cl = app.test_client()
    r = cl.post("/login?next=/pos", data={"password": "wrong", "next": "/pos"})
    assert r.status_code == 200 and b"Wrong password" in r.data
    assert cl.get("/pos").status_code == 302  # still locked out
    print("PASS wrong password rejected")


def test_correct_password_grants_access():
    app = make_app("espresso123")
    cl = app.test_client()
    r = cl.post("/login?next=/pos", data={"password": "espresso123", "next": "/pos"})
    assert r.status_code == 302
    assert cl.get("/pos").status_code == 200
    assert cl.get("/").status_code == 200
    print("PASS correct password grants access")


def test_logout_locks_again():
    app = make_app("espresso123")
    cl = app.test_client()
    cl.post("/login", data={"password": "espresso123"})
    assert cl.get("/orders").status_code == 200
    cl.get("/logout")
    assert cl.get("/orders").status_code == 302
    print("PASS logout locks again")


def test_open_when_no_password():
    app = create_app({"TESTING": True, "DATABASE": tempfile.mktemp(suffix=".sqlite3")})
    cl = app.test_client()
    assert cl.get("/pos").status_code == 200
    assert cl.get("/").status_code == 200
    print("PASS no password = open local mode")


def test_fresh_db_auto_seeds():
    os.environ["AUTO_SEED"] = "1"
    try:
        app = create_app({"TESTING": True, "DATABASE": tempfile.mktemp(suffix=".sqlite3")})
    finally:
        os.environ.pop("AUTO_SEED", None)
    with app.app_context():
        db = sqlite3.connect(app.config["DATABASE"])
        n = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        s = db.execute("SELECT shop_name FROM settings WHERE id=1").fetchone()[0]
        db.close()
    assert n > 0 and s == "Loop Co"
    print("PASS fresh database auto-seeds")
