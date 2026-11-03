"""Shared test helper: make a Flask test client speak CSRF.

CSRF protection is deliberately NOT disabled under testing. Turning a security
control off so the suite stays green is how it ends up switched off in
production too, and it would mean every other suite silently stops exercising
the real request path.

Instead this wraps the client so a POST carries the session's token, which is
what a real browser does after the server renders it into the page. A test that
wants to prove a request is refused WITHOUT a token simply calls the unwrapped
client, as tests_csrf.py does throughout.
"""


def csrf_client(client):
    """Wrap client.post so form and JSON requests carry the session token."""
    original_post = client.post

    def post(*args, **kwargs):
        with client.session_transaction() as sess:
            token = sess.get("_csrf")
            if not token:
                import secrets
                token = secrets.token_urlsafe(32)
                sess["_csrf"] = token

        if kwargs.get("json") is not None:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRFToken", token)
            kwargs["headers"] = headers
        else:
            data = kwargs.get("data")
            if isinstance(data, dict):
                if "_csrf_token" not in data:
                    data = dict(data)
                    data["_csrf_token"] = token
                    kwargs["data"] = data
            elif data is None:
                kwargs["data"] = {"_csrf_token": token}
        return original_post(*args, **kwargs)

    client.post = post
    return client
