"""Phone alerts (Web Push): every in-app notification also reaches the
phones and browsers where that person turned alerts on.

Run:  python3.13 tests_push.py
"""
import os
import sys

os.environ.pop("DATABASE_URL", None)
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import webpush as W  # noqa: E402
import app as A  # noqa: E402
from app import app, db, Employee, PushSub, AppKey, Notification  # noqa: E402
from tests_helper import csrf_client  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

app.config["TESTING"] = True
A.PUSH_ASYNC = False
PW = "longpass123"
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + extra) if extra and not cond else ""))


# ---- 1. The encryption is the standard one ----------------------------------
as_key = ec.derive_private_key(int.from_bytes(W.b64u_dec("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                               ec.SECP256R1())
body = W.encrypt(b"When I grow up, I want to be a watermelon",
                 "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                 "BTBZMqHH6r4Tts7J_aSIgg", salt=W.b64u_dec("DGv6ra1nlYgDCS1FRnbzlw"), as_key=as_key)
check("1.1 Message encryption matches the RFC 8291 example byte for byte", W.b64u(body) == (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"))
pub, priv = W.new_keys()
h = W.vapid_header("https://fcm.googleapis.com/fcm/send/x", priv, pub, "mailto:a@b.c")
check("1.2 The sender signature names the push service and carries the public key",
      h.startswith("vapid t=") and h.endswith("k=" + pub))

# ---- 2. Keys, service worker, subscribing -------------------------------------
with app.app_context():
    db.create_all()
    e = Employee.query.filter_by(email="push_p@x.com").first()
    if not e:
        e = Employee(name="Push Picker", email="push_p@x.com", staff_type="picker")
        e.set_password(PW)
        db.session.add(e)
        db.session.commit()
    EID = e.id
    PushSub.query.filter_by(emp_id=EID).delete()
    Notification.query.filter_by(emp_id=EID).delete()
    db.session.commit()
    k1, k2 = A.vapid_keys(), A.vapid_keys()
    check("2.1 The app makes its push keys once and keeps them", k1 == k2 and db.session.get(AppKey, "vapid_public") is not None)

c = csrf_client(app.test_client())
r = c.get("/sw.js")
check("2.2 The service worker is served from the site root, allowed for the whole site",
      r.status_code == 200 and r.headers.get("Service-Worker-Allowed") == "/" and b"showNotification" in r.data)
r.close()
c.post("/login", data={"email": "push_p@x.com", "password": PW}, follow_redirects=True)
check("2.3 A logged-in person gets the public key", c.get("/push/key").get_json()["key"] == k1[0])
bad = c.post("/push/subscribe", json={"subscription": {"endpoint": "http://evil.example/x", "keys": {"p256dh": "a", "auth": "b"}}})
check("2.4 Only real https push endpoints are accepted", bad.status_code == 400)
ua = ec.generate_private_key(ec.SECP256R1())
from cryptography.hazmat.primitives import serialization  # noqa: E402
ua_pub = W.b64u(ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/test-device", "keys": {"p256dh": ua_pub, "auth": W.b64u(os.urandom(16))}}
r = c.post("/push/subscribe", json={"subscription": SUB})
c.post("/push/subscribe", json={"subscription": SUB})
with app.app_context():
    check("2.5 Subscribing saves the device once", r.status_code == 200 and PushSub.query.filter_by(emp_id=EID).count() == 1)

# ---- 3. Every notification goes to the phone ------------------------------------
sent = []
orig = W.send
W.send = lambda sub, data, *a, **k: (sent.append((sub["endpoint"], data)) or 201)
try:
    with app.app_context():
        A.notify_employee(EID, "URGENT bill IP2627/4600", "4 items to pick", link="/dashboard", sms=False)
    check("3.1 A notification is pushed to the person's phone", len(sent) == 1 and sent[0][1]["title"] == "URGENT bill IP2627/4600"
          and sent[0][1]["body"] == "4 items to pick", str(sent))
    check("3.2 Tapping it opens the right page", sent and sent[0][1]["link"] == "/dashboard")
    with app.app_context():
        check("3.3 It is also kept in the app's notification list",
              Notification.query.filter_by(emp_id=EID).filter(Notification.title.like("URGENT%")).count() == 1)
    W.send = lambda *a, **k: 410
    with app.app_context():
        A.notify_employee(EID, "Late", "x", sms=False)
        check("3.4 A phone that unsubscribed (410) is removed", PushSub.query.filter_by(emp_id=EID).count() == 0)
finally:
    W.send = orig
r = c.post("/push/subscribe", json={"subscription": SUB})
c.post("/push/unsubscribe", json={"endpoint": SUB["endpoint"]})
with app.app_context():
    check("3.5 Turning alerts off removes the device", PushSub.query.filter_by(emp_id=EID).count() == 0)

# ---- 4. Pages offer it --------------------------------------------------------
d = c.get("/dashboard").data
check("4.1 The staff dashboard offers 'Turn on phone alerts'", b"js/push.js" in d and b"manifest.json" in d)
check("4.2 The app can be added to the home screen (manifest + icons)",
      os.path.isfile(os.path.join(REPO, "static", "manifest.json")) and os.path.isfile(os.path.join(REPO, "static", "icons", "icon-192.png")))
check("4.3 The admin page offers it too", b"js/push.js" in open(os.path.join(REPO, "templates", "admin.html"), "rb").read())

with app.app_context():
    e = Employee.query.filter_by(email="push_p@x.com").first()
    if e:
        Notification.query.filter_by(emp_id=e.id).delete()
        PushSub.query.filter_by(emp_id=e.id).delete()
        db.session.delete(e)
        db.session.commit()

print("\n" + "=" * 62)
print("PASSED %d    FAILED %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\nFailures:")
    for f in FAILED:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAILED else 0)
