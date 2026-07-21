"""Minimal public web layer over the agent harness.

Serves a chat page at `/` and a JSON endpoint at `/api/chat`. The page and the
API share an origin, so no CORS setup is needed. This is intentionally thin — the
real logic lives in the harness; here we just expose `agent.run()` over HTTP.

Run locally:  uvicorn web.app:app --reload
In Docker:    see Dockerfile / docker-compose.yml
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from config.settings import settings  # noqa: E402
from tradeflow import registry  # noqa: E402
from tradeflow.agent.loop import AgentStep  # noqa: E402
from tradeflow.compose import compose_system_prompt  # noqa: E402
from tradeflow.factory import build_agent  # noqa: E402
from tradeflow.llm.base import Message, Role  # noqa: E402
from tradeflow.tools.compliance import compliance_gate  # noqa: E402
from web import accounts, auth, store  # noqa: E402
from web.import_tools import build_import_tools  # noqa: E402
from web.listing_gen import generate_listing  # noqa: E402
from web.opp_suggest import suggest_opportunities  # noqa: E402
from web.database import connect, init_db  # noqa: E402
from web.documents import (create_document, documents_for_session, get_document,
                           list_versions, update_document)  # noqa: E402
from web.import_service import (ads_chat_context, ads_overview, competitor_rows, customer_overview, list_imports,
                                parse_upload, parse_upload_preview, save_import, suggest_mapping)  # noqa: E402

app = FastAPI(title="TradeFlow-AI")
STATIC = Path(__file__).parent / "static"

# 前端可选项 = "通用助手" + 注册表里的全部业务智能体（单一事实来源，见 0.2 registry）。
_DEFAULT = {"key": "default", "label": "通用助手", "desc": "默认工具集，无专属人设"}


@app.on_event("startup")
def startup() -> None:
    init_db()


# 除登录/登出接口本身外，所有 /api/* 都要求带有效 session cookie；未登录统一 401。
# 登出也豁免：cookie 已失效时也要能把浏览器那份 cookie 清掉，不能被这道门禁挡在 handler 之外。
# 兜底门禁：即便某个 handler 忘记加 Depends(auth.current_user)，这里也会先拦下来。
_PUBLIC_API_PATHS = {"/api/auth/login", "/api/auth/logout"}


@app.middleware("http")
async def require_session(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in _PUBLIC_API_PATHS:
        token = request.cookies.get(settings.session_cookie_name, "")
        if not auth.resolve_session(token):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
    # X-TradeFlow-Token 是另一套独立于登录的机器令牌校验，登录/登出接口不应该被它挡住
    # （前端从不发这个 header，配了 TRADEFLOW_API_TOKEN 的部署否则会导致登录本身 401）。
    if (request.url.path.startswith("/api/") and request.url.path not in _PUBLIC_API_PATHS
            and settings.api_token and request.headers.get("X-TradeFlow-Token") != settings.api_token):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "无效的访问令牌"}, status_code=401)
    return await call_next(request)


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginIn, response: Response) -> Dict[str, Any]:
    # 读密码哈希、bcrypt 校验、写 session 全部锁在同一个事务里（BEGIN IMMEDIATE 立刻拿写锁），
    # 不然管理员重置密码的 DELETE FROM sessions 可能刚好夹在"校验通过"和"写 session"之间
    # 跑完，旧密码这次登录写入的 session 就赶不上被清掉，重置形同虚设。
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id,name,password_hash,is_admin FROM users WHERE username=?",
                         (body.username,)).fetchone()
        # 用户名不存在时也跑一次 bcrypt 比对（对着一个固定假 hash），让响应耗时接近，
        # 避免通过时间差侧信道枚举出哪些用户名存在。
        password_hash = row["password_hash"] if row and row["password_hash"] else auth.DUMMY_PASSWORD_HASH
        ok = auth.verify_password(body.password, password_hash)
        if not row or not row["password_hash"] or not ok:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = auth.create_session_in(db, row["id"])
    response.set_cookie(
        settings.session_cookie_name, token, httponly=True, samesite="lax",
        secure=settings.secure_cookies, max_age=settings.session_ttl_days * 86400,
    )
    return {"id": row["id"], "name": row["name"], "is_admin": bool(row["is_admin"])}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> Dict[str, bool]:
    auth.delete_session(request.cookies.get(settings.session_cookie_name, ""))
    response.delete_cookie(settings.session_cookie_name)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user_id: str = Depends(auth.current_user)) -> Dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT id,name,is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return {"id": row["id"], "name": row["name"], "is_admin": bool(row["is_admin"])}


def require_admin(user_id: str = Depends(auth.current_user)) -> str:
    with connect() as db:
        row = db.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row["is_admin"]:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user_id


@app.get("/api/admin/users")
def admin_list_users(_: str = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": accounts.list_users()}


class AdminCreateUserIn(BaseModel):
    username: str
    password: str
    name: str = ""
    is_admin: bool = False


@app.post("/api/admin/users")
def admin_create_user(body: AdminCreateUserIn, _: str = Depends(require_admin)) -> Dict[str, Any]:
    try:
        user_id = accounts.create_user(body.username, body.password, body.name or None, body.is_admin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": user_id}


class AdminResetPasswordIn(BaseModel):
    password: str


@app.post("/api/admin/users/{target_id}/reset-password")
def admin_reset_password(target_id: str, body: AdminResetPasswordIn,
                         _: str = Depends(require_admin)) -> Dict[str, bool]:
    try:
        accounts.reset_password(target_id, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/stores")
def stores(x_tradeflow_user: str = Depends(auth.current_user)) -> Dict[str, Any]:
    with connect() as db:
        rows = db.execute("SELECT id,name,marketplace,created_at FROM stores WHERE user_id=? ORDER BY created_at",
                          (x_tradeflow_user,)).fetchall()
    return {"items": [dict(r) for r in rows]}


class StoreIn(BaseModel):
    name: str
    marketplace: str = "US"


@app.post("/api/stores")
def create_store(body: StoreIn, x_tradeflow_user: str = Depends(auth.current_user)) -> Dict[str, Any]:
    import uuid
    store_id = f"store_{uuid.uuid4().hex[:10]}"
    with connect() as db:
        db.execute("INSERT INTO stores(id,user_id,name,marketplace) VALUES(?,?,?,?)",
                   (store_id, x_tradeflow_user, body.name, body.marketplace))
    return {"id": store_id, "name": body.name, "marketplace": body.marketplace}


def _current_store(x_tradeflow_user: str = Depends(auth.current_user),
                   x_tradeflow_store: str = Header(default="default")) -> str:
    """校验 X-TradeFlow-Store 确实属于当前登录用户，不能直接信任客户端自报的店铺 id
    （否则能拼出 user_id=自己、store_id=别人店铺 的写入，见登录功能 review）。
    不属于就直接 403——不要静默换成该用户名下别的店铺（哪怕是"自己的"），那样一个
    过期/被篡改/切账号后没刷新的 header 会让写操作悄悄落到用户没有选中的店铺上，
    比明确报错更危险。"""
    with connect() as db:
        ok = db.execute("SELECT 1 FROM stores WHERE id=? AND user_id=?",
                        (x_tradeflow_store, x_tradeflow_user)).fetchone()
    if not ok:
        raise HTTPException(status_code=403, detail="无权访问该店铺")
    return x_tradeflow_store


def _agent_list() -> List[Dict[str, str]]:
    return [_DEFAULT] + [{"key": s.name, "label": s.label, "desc": s.description}
                         for s in registry.list_specs()]


def _build_agent(agent_key: str, observer):
    if agent_key in registry.REGISTRY:
        return registry.build(agent_key, observer=observer)
    return build_agent(observer=observer)


class ChatTurn(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    message: str
    agent: str = "default"
    history: List[ChatTurn] = Field(default_factory=list)


class ChatOut(BaseModel):
    reply: str
    iterations: int
    stopped_early: bool
    steps: List[Dict[str, Any]]


class ChatSessionIn(BaseModel):
    title: str = "新对话"
    agent: str = "default"


class ChatMessageIn(BaseModel):
    role: str
    content: str


class DocumentCreateIn(BaseModel):
    doc_type: str
    title: str
    content: str
    session_id: str | None = None
    source_message_id: int | None = None


class DocumentUpdateIn(BaseModel):
    title: str
    content: str


def _history_text(history: List[ChatTurn]) -> str:
    return "\n".join(f"{h.role}: {h.content}" for h in history[-8:])


def _to_messages(history: List[ChatTurn]) -> List[Message]:
    messages: List[Message] = []
    for item in history[-8:]:
        role = Role.ASSISTANT if item.role == "assistant" else Role.USER
        if item.content.strip():
            messages.append(Message(role=role, content=item.content[-4000:]))
    return messages


def _imported_report_chat_context(message: str, user_id: str, store_id: str,
                                  history: List[ChatTurn] | None = None) -> str | None:
    text = (message + "\n" + _history_text(history or [])).lower()
    asks_ads = any(k in text for k in ("广告", "acos", "竞价", "否词", "否定", "加预算", "降价", "搜索词"))
    asks_imported = any(k in text for k in ("导入", "报表", "刚上传", "刚导", "excel", "xlsx", "csv"))
    if not (asks_ads and asks_imported):
        return None
    context = ads_chat_context(user_id, store_id)
    if context:
        return context
    return "我还没有读到已导入的广告搜索词报表。请先到「数据导入」上传广告报表，并确认字段映射后再让我分析。"


def _build_imported_ads_agent(observer=None):
    prompt = compose_system_prompt("ads") + (
        "\n\n# 当前任务\n"
        "用户正在通过对话分析已经上传并入库的广告报表。报表上下文会作为用户消息的一部分提供。"
        "你要像真实广告优化师一样根据用户问题动态分析，而不是复述固定模板。"
        "如果上下文中已有足够数据，直接给结论；如果缺少成本、毛利或目标 ACOS，则明确标注缺口。"
        "严禁引用未提供的规则表、阈值或币种；金额单位保持上下文原样。"
        "没有毛利、成本和目标 ACOS 时，不能计算利润/亏损金额，只能基于花费、销售额、订单和 ACOS 判断优先级。"
        "销售额不是利润，禁止用 花费-销售额 或 ACOS>100% 直接声称明确亏损。"
    )
    return build_agent(system_prompt=prompt, tools=[], observer=observer)


def _imported_ads_user_input(original_message: str, context: str, history: List[ChatTurn]) -> str:
    history_block = _history_text(history)
    return (
        f"最近对话上下文：\n{history_block or '（无）'}\n\n"
        f"用户原始问题：{original_message}\n\n"
        f"{context}\n\n"
        "请基于上面的真实导入数据回答用户问题。"
    )


def _sanitize_imported_ads_reply(reply: str) -> str:
    replacements = {
        "实质性亏损性花费": "高风险广告花费",
        "明确亏损": "ACOS 明显偏高",
        "实际已亏": "存在明显广告效率风险",
        "典型的「负向 ROI」活动": "典型的低效广告活动",
        "负向 ROI": "低效 ROI",
        "每单广告净亏": "单均广告花费偏高",
        "净亏": "广告花费偏高",
        "亏损型活动": "高 ACOS 活动",
        "亏损活动": "高 ACOS 活动",
        "亏损性": "高风险",
        "亏损": "高风险消耗",
        "花出去的钱比赚回来的多": "广告花费高于广告归因销售额",
        "花费比销售额还高": "广告花费高于广告归因销售额",
    }
    out = reply
    for src, dst in replacements.items():
        out = out.replace(src, dst)
    return out


# 明确指向"用户上传的表格/报表"的强信号：只要当前消息出现即可进导入分析支线。
_IMPORT_STRONG_TERMS = (
    "导入", "已导", "刚导", "刚上传", "上传", "报表", "excel", "xlsx", "csv", "字段映射",
)
# 分析范围词：本身太泛（"数据/广告/订单"随处可见），只有在"确有已入库文件"或
# "对话此前已进入导入分析"时，才作为进支线的依据——否则会被业务对话里的常见词误伤。
_IMPORT_SCOPE_TERMS = (
    "数据", "订单", "库存", "广告", "acos", "搜索词", "sku",
)
# 延续/追问词：仅在导入分析已经激活时才用于"留在支线"，绝不单独触发进入。
_IMPORT_CONTINUE_TERMS = ("第二个", "继续", "接着", "为什么", "那这个", "还有呢")


def _is_import_data_query(message: str, history: List[ChatTurn] | None = None,
                          has_imports: bool = False) -> bool:
    """是否应把本轮对话交给"导入数据分析"支线。

    只扫历史里的**强信号**判断支线是否已激活；泛范围词只看当前消息，避免助手上一轮
    回复里出现"数据"之类常见词，把后续无关对话（如生成 Listing 文案）劫持进本支线。
    """
    current = (message or "").lower()
    if _contains_any(current, _IMPORT_STRONG_TERMS):
        return True
    # 对话此前已明确进入导入分析（历史里出现过强信号）时，本轮的范围词/追问词才生效。
    branch_active = _contains_any(_history_text(history or []).lower(), _IMPORT_STRONG_TERMS)
    if branch_active and _contains_any(current, _IMPORT_SCOPE_TERMS + _IMPORT_CONTINUE_TERMS):
        return True
    # 用户确有已入库文件，且当前消息带范围词时，也进支线（有真实数据可分析）。
    if has_imports and _contains_any(current, _IMPORT_SCOPE_TERMS):
        return True
    return False


ADS_SCOPE_TERMS = (
    "广告", "acos", "roas", "搜索词", "竞价", "否词", "否定", "投放", "campaign", "ppc",
)
ORDER_SCOPE_TERMS = (
    "商品交易", "交易数据", "订单", "订单数据", "销售数据", "商品数据", "sku",
    "销量", "销售额", "客单价", "商品表现",
)
INVENTORY_SCOPE_TERMS = ("库存", "补货", "缺货", "库龄", "available", "stock")
COMPETITOR_SCOPE_TERMS = ("竞品", "竞争", "对手", "asin")
COMBINE_SCOPE_TERMS = ("结合", "一起", "综合", "全链路", "同时", "对比", "全部数据", "所有数据")


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _scope_hits(text: str) -> int:
    groups = (ADS_SCOPE_TERMS, ORDER_SCOPE_TERMS, INVENTORY_SCOPE_TERMS, COMPETITOR_SCOPE_TERMS)
    return sum(1 for terms in groups if _contains_any(text, terms))


def _import_data_scope(message: str, history: List[ChatTurn] | None = None) -> str:
    current = message.lower()
    if _contains_any(current, COMBINE_SCOPE_TERMS) and _scope_hits(current) > 1:
        return "multi"
    if "全部数据" in current or "所有数据" in current:
        return "multi"
    if _contains_any(current, ORDER_SCOPE_TERMS) and not _contains_any(current, ADS_SCOPE_TERMS):
        return "orders"
    if _contains_any(current, ADS_SCOPE_TERMS):
        return "ads_search_terms"
    if _contains_any(current, INVENTORY_SCOPE_TERMS):
        return "inventory"
    if _contains_any(current, COMPETITOR_SCOPE_TERMS):
        return "competitors"

    context = _history_text(history or []).lower()
    if _contains_any(context, COMBINE_SCOPE_TERMS) and _scope_hits(context) > 1:
        return "multi"
    if _contains_any(context, ORDER_SCOPE_TERMS) and not _contains_any(context, ADS_SCOPE_TERMS):
        return "orders"
    if _contains_any(context, ADS_SCOPE_TERMS):
        return "ads_search_terms"
    if _contains_any(context, INVENTORY_SCOPE_TERMS):
        return "inventory"
    if _contains_any(context, COMPETITOR_SCOPE_TERMS):
        return "competitors"
    return "auto"


def _import_data_scope_directive(message: str, history: List[ChatTurn]) -> str:
    scope = _import_data_scope(message, history)
    directives = {
        "orders": (
            "本次用户要求分析商品交易/订单/销售类数据。请优先且只使用 report_type='orders' "
            "的导入文件；不要调用或分析 ads_search_terms，不要主动提 ACOS、ROAS、广告投放、"
            "搜索词、竞价、否词，除非用户明确要求结合广告数据。优化建议只能基于订单/交易指标，"
            "例如 SKU 销售额、订单数、销量、客单价、退款/调整、费用、总额、地区或履约等字段。"
        ),
        "ads_search_terms": (
            "本次用户要求分析广告/搜索词/投放类数据。请优先使用 report_type='ads_search_terms' "
            "的导入文件；不要把订单交易文件混入结论，除非用户明确要求结合订单或商品交易数据。"
        ),
        "inventory": (
            "本次用户要求分析库存类数据。请优先使用 report_type='inventory' 的导入文件；"
            "不要把广告或订单文件混入结论，除非用户明确要求跨报表综合分析。"
        ),
        "competitors": (
            "本次用户要求分析竞品/ASIN 类数据。请优先使用 report_type='competitors' 的导入文件；"
            "不要把广告或订单文件混入结论，除非用户明确要求跨报表综合分析。"
        ),
        "multi": (
            "本次用户明确要求跨报表或综合分析。可以先 list_imported_files，再按问题选择多个 "
            "report_type；结论必须说明每个判断分别来自哪些导入文件。"
        ),
        "auto": (
            "本次用户没有明确限定报表类型。请先 list_imported_files 判断可用文件，再根据用户问题选择"
            "最相关的 report_type；不要因为系统中存在其他文件就自动混用。"
        ),
    }
    return directives[scope]


def _build_import_data_agent(user_id: str, store_id: str, observer=None):
    prompt = (
        "你是 TradeFlow-AI 的通用导入数据分析智能体。用户上传的 Excel/CSV 已经入库，"
        "你不能假设只分析某一种文件，也不能声称没有收到文件，除非工具返回确实没有数据。\n\n"
        "工作方式：\n"
        "1. 先判断用户当前问题限定的数据范围，再决定调用哪些工具。不要因为系统中存在其他导入文件就混用。\n"
        "2. 不知道有哪些文件时，先调用 list_imported_files。\n"
        "3. 用户明确限定订单、广告、库存、竞品等范围时，工具调用必须传对应 report_type；只有用户明确要求"
        "跨报表/综合分析时才跨 report_type。\n"
        "4. 不知道字段含义时，调用 inspect_imported_file 看字段、样例和数字列概览。\n"
        "5. 需要汇总排行、分组对比、筛选明细时，调用 aggregate_imported_file 或 sample_imported_rows。\n"
        "6. 工具结果不足以回答时，可以继续调用工具；足够时再结束。\n\n"
        "边界：只基于工具返回的真实导入数据。金额没有币种时不要加币种。"
        "销售额不是利润；没有成本、毛利、回款率时不要计算利润/亏损金额。"
        "可以说 ACOS 高、广告效率风险高、花费高于广告归因销售额，但不要说明确亏损。"
        "回答要贴近用户 query；用户追问“第二个/继续/为什么”时，要结合最近对话上下文理解指代。"
    )
    return build_agent(
        system_prompt=prompt,
        tools=build_import_tools(user_id, store_id),
        observer=observer,
        max_iterations=12,
    )


def _import_data_user_input(message: str, history: List[ChatTurn]) -> str:
    return (
        f"最近对话上下文：\n{_history_text(history) or '（无）'}\n\n"
        f"数据范围约束：\n{_import_data_scope_directive(message, history)}\n\n"
        f"用户当前问题：{message}\n\n"
        "请按需调用导入数据工具进行实时分析。"
    )


def _short_title(text: str) -> str:
    title = " ".join((text or "").strip().split())
    return (title[:28] + "…") if len(title) > 28 else (title or "新对话")


def _session_owned(db, session_id: str, user_id: str, store_id: str | None = None):
    if store_id:
        row = db.execute(
            "SELECT id,title,agent,created_at,updated_at FROM chat_sessions "
            "WHERE id=? AND user_id=? AND store_id=?",
            (session_id, user_id, store_id),
        ).fetchone()
        if row:
            return row
    return db.execute(
        "SELECT id,title,agent,created_at,updated_at FROM chat_sessions "
        "WHERE id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone()


@app.get("/api/chat/sessions")
def chat_sessions(x_tradeflow_user: str = Depends(auth.current_user),
                  x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            "SELECT id,title,agent,created_at,updated_at FROM chat_sessions "
            "WHERE user_id=? AND store_id=? ORDER BY updated_at DESC LIMIT 30",
            (x_tradeflow_user, x_tradeflow_store),
        ).fetchall()
        if not rows:
            rows = db.execute(
                "SELECT id,title,agent,created_at,updated_at FROM chat_sessions "
                "WHERE user_id=? ORDER BY updated_at DESC LIMIT 30",
                (x_tradeflow_user,),
            ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/chat/sessions")
def create_chat_session(body: ChatSessionIn,
                        x_tradeflow_user: str = Depends(auth.current_user),
                        x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    # x_tradeflow_store 已经过 _current_store 校验，保证确实属于当前用户，不需要再自己重查一遍。
    session_id = f"chat_{uuid.uuid4().hex[:12]}"
    title = _short_title(body.title)
    with connect() as db:
        db.execute(
            "INSERT INTO chat_sessions(id,user_id,store_id,title,agent) VALUES(?,?,?,?,?)",
            (session_id, x_tradeflow_user, x_tradeflow_store, title, body.agent or "default"),
        )
    return {"id": session_id, "title": title, "agent": body.agent or "default"}


@app.get("/api/chat/sessions/{session_id}")
def get_chat_session(session_id: str,
                     x_tradeflow_user: str = Depends(auth.current_user),
                     x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    with connect() as db:
        row = _session_owned(db, session_id, x_tradeflow_user, x_tradeflow_store)
        if not row:
            raise HTTPException(status_code=404, detail="对话不存在")
        messages = db.execute(
            "SELECT id,role,content,created_at FROM chat_messages WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
    documents = {d["source_message_id"]: d for d in documents_for_session(
        session_id, x_tradeflow_user, x_tradeflow_store
    ) if d.get("source_message_id") is not None}
    items = []
    for message in messages:
        item = dict(message)
        if message["id"] in documents:
            item["document"] = documents[message["id"]]
        items.append(item)
    return {**dict(row), "messages": items}


@app.post("/api/chat/sessions/{session_id}/messages")
def add_chat_message(session_id: str, body: ChatMessageIn,
                     x_tradeflow_user: str = Depends(auth.current_user),
                     x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    role = body.role if body.role in {"user", "assistant"} else ""
    content = (body.content or "").strip()
    if not role or not content:
        raise HTTPException(status_code=400, detail="消息无效")
    with connect() as db:
        row = _session_owned(db, session_id, x_tradeflow_user, x_tradeflow_store)
        if not row:
            raise HTTPException(status_code=404, detail="对话不存在")
        cur = db.execute(
            "INSERT INTO chat_messages(session_id,role,content) VALUES(?,?,?)",
            (session_id, role, content[:12000]),
        )
        if role == "user" and row["title"] == "新对话":
            db.execute("UPDATE chat_sessions SET title=? WHERE id=?", (_short_title(content), session_id))
        db.execute("UPDATE chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
    return {"ok": True, "id": cur.lastrowid}


@app.post("/api/documents")
def create_business_document(body: DocumentCreateIn,
                             x_tradeflow_user: str = Depends(auth.current_user),
                             x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    try:
        return create_document(x_tradeflow_user, x_tradeflow_store, body.doc_type, body.title,
                               body.content, body.session_id, body.source_message_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/documents/{document_id}")
def business_document(document_id: str,
                      x_tradeflow_user: str = Depends(auth.current_user),
                      x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    document = get_document(document_id, x_tradeflow_user, x_tradeflow_store)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    return document


@app.patch("/api/documents/{document_id}")
def update_business_document(document_id: str, body: DocumentUpdateIn,
                             x_tradeflow_user: str = Depends(auth.current_user),
                             x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    try:
        document = update_document(document_id, x_tradeflow_user, x_tradeflow_store,
                                   body.title, body.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    return document


@app.get("/api/documents/{document_id}/versions")
def business_document_versions(document_id: str,
                               x_tradeflow_user: str = Depends(auth.current_user),
                               x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    versions = list_versions(document_id, x_tradeflow_user, x_tradeflow_store)
    if versions is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"items": versions}


@app.get("/api/documents/{document_id}/export")
def export_business_document(document_id: str,
                             x_tradeflow_user: str = Depends(auth.current_user),
                             x_tradeflow_store: str = Depends(_current_store)) -> Response:
    document = get_document(document_id, x_tradeflow_user, x_tradeflow_store)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    body = f"# {document['title']}\n\n{document['content']}\n"
    filename = f"tradeflow-{document['doc_type']}-{document['id']}.md"
    return Response(body, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/")
def index() -> FileResponse:
    # 新的完整产品原型页（对话已接后端；机会上新等模块仍在接入中）。
    return FileResponse(
        STATIC / "prototype.html",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/classic")
def classic() -> FileResponse:
    # 旧的极简聊天页，保留作为纯净的智能体联调入口。
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/healthz")
def healthz() -> Dict[str, bool]:
    return {"ok": True}


@app.get("/api/info")
def info() -> Dict[str, str]:
    # Lets the page show whether it's on the real model or the mock.
    return {"provider": settings.provider, "model": settings.default_model}


@app.get("/api/agents")
def agents() -> List[Dict[str, str]]:
    return _agent_list()


# ---- 机会上新：真实持久化的 CRUD（B1） ----
@app.get("/api/opportunities")
def list_opportunities(x_tradeflow_user: str = Depends(auth.current_user),
                       x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    return {"items": store.list_opps(x_tradeflow_user, x_tradeflow_store)}


@app.post("/api/opportunities")
def create_opportunity(body: Dict[str, Any], x_tradeflow_user: str = Depends(auth.current_user),
                       x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    # 入参是前端传的机会对象（name/cat/score/margin/…），存储层负责补 id/时间/去重。
    return store.add_opp(body, x_tradeflow_user, x_tradeflow_store)


@app.delete("/api/opportunities/{opp_id}")
def remove_opportunity(opp_id: str, x_tradeflow_user: str = Depends(auth.current_user),
                       x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, bool]:
    return {"ok": store.delete_opp(opp_id, x_tradeflow_user, x_tradeflow_store)}


@app.post("/api/imports/preview")
async def import_preview(file: UploadFile = File(...)) -> Dict[str, Any]:
    content = await file.read()
    if len(content) > 80 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件不能超过 80MB")
    try:
        preview = parse_upload_preview(file.filename or "upload.xlsx", content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"filename": file.filename, **preview}


@app.post("/api/imports")
async def import_commit(file: UploadFile = File(...), mapping: str = Form(default="{}"),
                        x_tradeflow_user: str = Depends(auth.current_user),
                        x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    content = await file.read()
    if len(content) > 80 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件不能超过 80MB")
    try:
        columns, rows = parse_upload(file.filename or "upload.xlsx", content)
        selected = json.loads(mapping) if mapping and mapping != "{}" else suggest_mapping(columns)
        return save_import(x_tradeflow_user, x_tradeflow_store, file.filename or "upload.xlsx",
                           columns, rows, selected)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/imports")
def imports(x_tradeflow_user: str = Depends(auth.current_user),
            x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    return {"items": list_imports(x_tradeflow_user, x_tradeflow_store)}


@app.get("/api/optimization/ads")
def optimization_ads(x_tradeflow_user: str = Depends(auth.current_user),
                     x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    return ads_overview(x_tradeflow_user, x_tradeflow_store)


@app.get("/api/customers")
def customers(x_tradeflow_user: str = Depends(auth.current_user),
              x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    return customer_overview(x_tradeflow_user, x_tradeflow_store)


# ---- 合规预检：直接调 #1 合规的确定性工具，返回结构化结果（B1，无需过模型） ----
class ComplianceIn(BaseModel):
    text: str = ""
    category: str = ""
    site: str = "US"


@app.post("/api/compliance/check")
def compliance_check(body: ComplianceIn) -> Dict[str, Any]:
    return compliance_gate.func(body.text, body.category, body.site)


# ---- 生成 Listing 素材：#2 文案（JSON 契约）+ 词根库 + 合规，结构化返回（B0） ----
class ListingGenIn(BaseModel):
    name: str
    category: str = ""
    site: str = "US"


@app.post("/api/listing/generate")
def listing_generate(body: ListingGenIn) -> Dict[str, Any]:
    return generate_listing(body.name, body.category, body.site)


# ---- 对话选品：结构化机会清单，供前端渲染可「放入机会上新」的卡片（B0） ----
class OppSuggestIn(BaseModel):
    query: str
    top_n: int = 4


@app.post("/api/opportunities/suggest")
def opportunities_suggest(body: OppSuggestIn,
                          x_tradeflow_user: str = Depends(auth.current_user),
                          x_tradeflow_store: str = Depends(_current_store)) -> Dict[str, Any]:
    return suggest_opportunities(
        body.query, body.top_n, competitor_rows(x_tradeflow_user, x_tradeflow_store))


@app.post("/api/chat", response_model=ChatOut)
def chat(body: ChatIn, x_tradeflow_user: str = Depends(auth.current_user),
         x_tradeflow_store: str = Depends(_current_store)) -> ChatOut:
    steps: List[Dict[str, Any]] = []

    def observe(step: AgentStep) -> None:
        steps.append({
            "iteration": step.iteration,
            "reasoning": step.reasoning,
            "text": step.text,
            "tools": step.tool_calls,
        })

    has_imports = bool(list_imports(x_tradeflow_user, x_tradeflow_store))
    if _is_import_data_query(body.message, body.history, has_imports):
        result = _build_import_data_agent(x_tradeflow_user, x_tradeflow_store, observe).run(
            _import_data_user_input(body.message, body.history))
        return ChatOut(
            reply=_sanitize_imported_ads_reply(result.output),
            iterations=result.iterations,
            stopped_early=result.stopped_early,
            steps=steps,
        )

    # Fresh agent per request → clean, single-turn conversations (no shared state).
    agent = _build_agent(body.agent, observe)
    result = agent.run(body.message, history=_to_messages(body.history))
    return ChatOut(
        reply=result.output,
        iterations=result.iterations,
        stopped_early=result.stopped_early,
        steps=steps,
    )


def _sse(event: Dict[str, Any]) -> str:
    """把一个事件序列化成一帧 SSE。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn, x_tradeflow_user: str = Depends(auth.current_user),
                      x_tradeflow_store: str = Depends(_current_store)) -> StreamingResponse:
    """SSE 流式版对话：边跑边把进度推给前端，避免长任务（抓竞品等）看起来像卡死。

    agent.run_stream 逐事件产出：("tools",…) 本轮要调的工具、("token",…) 最终答案的
    token 增量、("final",…) 结束。整个循环同步阻塞（内部还等爬虫），放线程里跑，
    通过线程安全队列把事件转成 SSE。前端据此：抓取时显示"正在调用…"、答案逐字蹦出。
    """
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def push(evt: Dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, evt)

    def run_agent() -> None:
        try:
            has_imports = bool(list_imports(x_tradeflow_user, x_tradeflow_store))
            is_import_query = _is_import_data_query(body.message, body.history, has_imports)
            if is_import_query:
                agent = _build_import_data_agent(x_tradeflow_user, x_tradeflow_store, lambda _step: None)
                user_input = _import_data_user_input(body.message, body.history)
                history = None
            else:
                agent = _build_agent(body.agent, lambda _step: None)  # 流式下不用 observer
                user_input = body.message
                history = _to_messages(body.history)
            for kind, payload in agent.run_stream(user_input, history=history):
                if kind == "token":
                    push({"type": "token", "text": payload})
                elif kind == "reset":
                    push({"type": "reset"})
                elif kind == "tools":
                    push({"type": "status", "tools": payload,
                          "message": "正在调用工具：" + "、".join(payload) + " …"})
                elif kind == "final":
                    reply = _sanitize_imported_ads_reply(payload.output) if is_import_query else payload.output
                    push({"type": "final", "reply": reply,
                          "iterations": payload.iterations,
                          "stopped_early": payload.stopped_early})
        except Exception as exc:  # noqa: BLE001 —— 把错误推给前端而非静默
            push({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
        push({"type": "done"})

    threading.Thread(target=run_agent, daemon=True).start()

    async def event_gen():
        # 先推一个"已收到、开始处理"，让前端立刻有反馈。
        yield _sse({"type": "status", "iteration": 0, "message": "已收到，开始处理 …"})
        while True:
            evt = await queue.get()
            if evt.get("type") == "done":
                break
            yield _sse(evt)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/static/prototype.html")
def prototype_static() -> FileResponse:
    return FileResponse(
        STATIC / "prototype.html",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache", "Expires": "0"},
    )


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
