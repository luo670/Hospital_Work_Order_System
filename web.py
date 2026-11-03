"""
Web 辅助层：模板渲染、闪现消息、鉴权依赖

目标：在 FastAPI 下尽量保留 Flask 时代模板的用法——
- 模板内仍可使用 url_for('xxx')、get_flashed_messages()、g.user
- 通过注入 endpoint（当前路由名）实现侧边栏高亮
"""

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# 鉴权重定向异常：依赖中 raise，由 main.py 注册的异常处理器统一转为 302
# （Starlette 的依赖若直接 return Response 不会短路，必须走异常处理器）
# ---------------------------------------------------------------------------
class AuthRedirect(Exception):
    def __init__(self, url, flash_message=None):
        self.url = url
        self.flash_message = flash_message


# ---------------------------------------------------------------------------
# 闪现消息（flash）：基于会话存储，渲染后清空
# ---------------------------------------------------------------------------
def flash(request: Request, message: str):
    flashes = request.session.get("_flashes", [])
    flashes.append(message)
    request.session["_flashes"] = flashes


# ---------------------------------------------------------------------------
# 当前登录用户（由 load_user 中间件写入 request.state.user）
# ---------------------------------------------------------------------------
def get_current_user(request: Request):
    return getattr(request.state, "user", None)


# ---------------------------------------------------------------------------
# 鉴权依赖：未登录或越权时抛出 AuthRedirect，交由异常处理器统一 302 跳转
# ---------------------------------------------------------------------------
def login_required(request: Request):
    if not get_current_user(request):
        raise AuthRedirect(str(request.url_for("login")))


def role_required(*roles):
    def dep(request: Request):
        user = get_current_user(request)
        if not user:
            raise AuthRedirect(str(request.url_for("login")))
        if user["role"] not in roles:
            raise AuthRedirect(
                str(request.url_for("dashboard")),
                flash_message="您没有权限访问该页面",
            )

    return dep


# ---------------------------------------------------------------------------
# 模板渲染：统一注入模板所需的全局变量
# ---------------------------------------------------------------------------
def render(request: Request, name: str, **ctx):
    flashes = request.session.get("_flashes", [])
    request.session["_flashes"] = []

    # 当前路由名（用于侧边栏高亮）：在 handler 内调用时路由已完成匹配，
    # request.scope["endpoint"] 为对应可调用对象。
    ep = request.scope.get("endpoint")
    endpoint = getattr(ep, "__name__", None) if callable(ep) else None

    ctx["request"] = request
    ctx["url_for"] = request.url_for
    ctx["get_flashed_messages"] = lambda with_categories=False: flashes
    ctx["g"] = {"user": get_current_user(request)}
    ctx["endpoint"] = endpoint
    return templates.TemplateResponse(request, name, ctx)
