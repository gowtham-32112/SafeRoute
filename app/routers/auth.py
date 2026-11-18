"""Authentication, session management, and RBAC verification routes."""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from app.config import Settings
from app.deps import (
    get_current_user,
    get_settings,
    require_admin,
    require_auth_csrf,
    require_csrf,
)
from app.services.auth import (
    AuthenticatedUser,
    LoginPayload,
    SignupPayload,
    UserPublicResponse,
    login_with_email,
    logout_session,
    signup_with_email,
)
from app.services.session import (
    clear_session_cookies,
    extract_session_tokens,
    issue_csrf_cookie,
    set_session_cookies,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _is_form_submission(request: Request) -> bool:
    """Return True when the request uses HTML form encoding."""
    content_type = request.headers.get("content-type", "").lower()
    return (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    )


async def _parse_request_data(request: Request) -> Dict[str, Any]:
    """Parse either JSON body or HTML form body into a plain dictionary."""
    if _is_form_submission(request):
        form = await request.form()
        return {k: str(v) for k, v in form.items()}
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid request body.",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Request body must be a JSON object.",
        )
    return body


def _browser_wants_redirect(request: Request) -> bool:
    """Return True when a browser HTML form expects a 303 redirect."""
    if not _is_form_submission(request):
        return False
    accept = request.headers.get("accept", "").lower()
    return "text/html" in accept and "application/json" not in accept


@router.get("/csrf", summary="Issue CSRF Token Cookie")
async def get_csrf_token(
    response: Response,
    app_settings: Settings = Depends(get_settings),
) -> Dict[str, str]:
    """Issue a double-submit CSRF cookie for browser/HTMX state-changing requests."""
    token = issue_csrf_cookie(response, app_settings)
    return {"csrf_token": token}


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    summary="Email/Password User Signup",
    dependencies=[Depends(require_auth_csrf)],
)
async def signup(
    request: Request,
    app_settings: Settings = Depends(get_settings),
) -> Response:
    """Register a user via Supabase email/password Auth and set secure session cookies."""
    raw_data = await _parse_request_data(request)
    try:
        payload = SignupPayload.model_validate(raw_data)
    except ValidationError as exc:
        first_msg = exc.errors()[0].get("msg", "Invalid signup input.")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=first_msg,
        ) from exc

    auth_session = signup_with_email(payload, app_settings)
    user_out = UserPublicResponse(
        id=auth_session.user.id,
        email=auth_session.user.email,
        full_name=auth_session.user.full_name,
        role=auth_session.user.role,
    )

    if _browser_wants_redirect(request):
        target = "/login?confirm_email=1" if auth_session.email_confirmation_required else "/map"
        resp: Response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    else:
        resp = JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "message": (
                    "Account created. Please check your email to confirm your account."
                    if auth_session.email_confirmation_required
                    else "Account created and authenticated."
                ),
                "email_confirmation_required": auth_session.email_confirmation_required,
                "user": user_out.model_dump(),
            },
        )

    if auth_session.access_token:
        set_session_cookies(
            response=resp,
            access_token=auth_session.access_token,
            refresh_token=auth_session.refresh_token,
            app_settings=app_settings,
            expires_in=auth_session.expires_in,
        )
    return resp


@router.post(
    "/login",
    summary="Email/Password User Login",
    dependencies=[Depends(require_auth_csrf)],
)
async def login(
    request: Request,
    app_settings: Settings = Depends(get_settings),
) -> Response:
    """Authenticate a user via Supabase Auth and issue HTTP-only session cookies."""
    raw_data = await _parse_request_data(request)
    try:
        payload = LoginPayload.model_validate(raw_data)
    except ValidationError as exc:
        first_msg = exc.errors()[0].get("msg", "Invalid login input.")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=first_msg,
        ) from exc

    auth_session = login_with_email(payload, app_settings)
    user_out = UserPublicResponse(
        id=auth_session.user.id,
        email=auth_session.user.email,
        full_name=auth_session.user.full_name,
        role=auth_session.user.role,
    )

    if _browser_wants_redirect(request):
        resp: Response = RedirectResponse(url="/map", status_code=status.HTTP_303_SEE_OTHER)
    else:
        resp = JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "message": "Authenticated successfully.",
                "user": user_out.model_dump(),
            },
        )

    if auth_session.access_token:
        set_session_cookies(
            response=resp,
            access_token=auth_session.access_token,
            refresh_token=auth_session.refresh_token,
            app_settings=app_settings,
            expires_in=auth_session.expires_in,
        )
    return resp


@router.post(
    "/logout",
    summary="Logout and Invalidate Session",
    dependencies=[Depends(require_csrf)],
)
async def logout(
    request: Request,
    app_settings: Settings = Depends(get_settings),
) -> Response:
    """Revoke Supabase session (best-effort) and clear HTTP-only auth & CSRF cookies."""
    access_token, _, _ = extract_session_tokens(request, app_settings)
    logout_session(access_token, app_settings)

    if _browser_wants_redirect(request):
        resp: Response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    else:
        resp = JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"message": "Logged out successfully."},
        )

    clear_session_cookies(resp, app_settings)
    return resp


@router.get(
    "/me",
    response_model=UserPublicResponse,
    summary="Current Authenticated User Session",
)
async def read_current_user(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> UserPublicResponse:
    """Protected authenticated-user endpoint returning validated identity and role."""
    return UserPublicResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
    )


@router.get(
    "/admin-check",
    summary="Admin-Only Authorization Verification Endpoint",
)
async def admin_authorization_check(
    admin_user: AuthenticatedUser = Depends(require_admin),
) -> Dict[str, str]:
    """Protected admin-only endpoint verifying profiles.role = 'admin' enforcement."""
    return {
        "status": "authorized",
        "role": admin_user.role,
        "user_id": admin_user.id,
    }
