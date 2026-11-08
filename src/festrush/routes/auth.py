"""Sign up, sign in, sign out."""

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from festrush import services
from festrush.deps import (
    client_id,
    connection,
    current_user,
    render,
    state,
    verify_csrf,
)
from festrush.security import SESSION_COOKIE

router = APIRouter()

NOTICES = {"created": "Account created. Sign in to continue.", "bye": "You have been signed out."}


@router.get("/register")
def register_page(request: Request) -> Response:
    return render(request, "register.html", current_user(request))


@router.post("/register", dependencies=[Depends(verify_csrf)])
def register(request: Request, username: str = Form(""), password: str = Form("")) -> Response:
    username = username.strip()
    problem = services.validate_credentials(username, password)
    if problem:
        return render(request, "register.html", None, status_code=422, error=problem, username=username)
    password_hash = state(request).passwords.hash(password)
    with connection(request) as conn:
        user_id = services.create_user(conn, username, password_hash)
    if user_id is None:
        return render(
            request,
            "register.html",
            None,
            status_code=409,
            error="That username is taken. Try another.",
            username=username,
        )
    return RedirectResponse("/login?notice=created", status_code=303)


@router.get("/login")
def login_page(request: Request, notice: str = "") -> Response:
    return render(request, "login.html", current_user(request), notice=NOTICES.get(notice, ""))


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, username: str = Form(""), password: str = Form("")) -> Response:
    st = state(request)
    client = client_id(request)
    username = username.strip()

    with connection(request) as conn:
        blocked = services.login_blocked(
            conn,
            username,
            client,
            max_failures=st.settings.login_max_failures,
            minutes=st.settings.login_window_minutes,
        )
        user = None if blocked else services.find_user(conn, username)
    if blocked:
        return render(
            request,
            "login.html",
            None,
            status_code=429,
            error="Too many failed attempts. Wait a few minutes and try again.",
            username=username,
        )

    # Hashing is deliberately slow; do it without holding a database connection.
    ok = st.passwords.verify(user["password_hash"] if user else None, password)
    if not ok or user is None:
        with connection(request) as conn:
            services.record_login_failure(conn, username, client)
        return render(
            request,
            "login.html",
            None,
            status_code=401,
            error="Invalid username or password.",
            username=username,
        )

    with connection(request) as conn:
        if st.passwords.needs_rehash(user["password_hash"]):
            services.set_password_hash(conn, user["id"], st.passwords.hash(password))
        services.clear_login_failures(conn, username, client)
        token = services.create_session(conn, user["id"], st.settings.session_hours)

    response = RedirectResponse("/events", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=st.settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=st.settings.cookie_secure,
        path="/",
    )
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with connection(request) as conn:
            services.delete_session(conn, token)
    response = RedirectResponse("/login?notice=bye", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
