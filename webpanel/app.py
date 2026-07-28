from __future__ import annotations
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

import aiosqlite
import httpx
from fastapi import FastAPI, Request, Form, UploadFile, File as FastAPIFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT_DIR))

from config import cfg
from services.panel import panel
from utils.qr import make_qr

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="VPN Bot Admin Panel")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

DB_PATH = str(BOT_DIR / cfg.db_path)

PANEL_ADMIN_USER = os.getenv("PANEL_ADMIN_USER", "admin")
PANEL_ADMIN_PASS = os.getenv("PANEL_ADMIN_PASS", "admin")

SESSIONS: dict[str, float] = {}
SESSION_TTL = 60 * 60 * 12


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = time.time() + SESSION_TTL
    return token


def check_session(request: Request) -> bool:
    token = request.cookies.get("session")
    if not token or token not in SESSIONS:
        return False
    if SESSIONS[token] < time.time():
        del SESSIONS[token]
        return False
    return True


ENV_PATH = BOT_DIR / ".env"


def _unquote_env_value(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


def _quote_env_value(value: str) -> str:
    """Always wrap in double quotes so values with spaces/special chars are
    safe both for python-dotenv (config.py) and for any bash script that
    does `source .env` (e.g. scripts/vpn-bot.sh health check)."""
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def env_get_all() -> dict[str, str]:
    """Read the .env file into an ordered dict, preserving line order for rewriting."""
    result: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if not line or line.strip().startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = _unquote_env_value(value)
    return result


def env_get(key: str, default: str = "") -> str:
    return env_get_all().get(key, default)


def env_set_many(updates: dict[str, str]) -> None:
    """Update or append multiple keys in .env, preserving existing lines/order.
    Values are always written wrapped in double quotes to survive spaces/
    special characters safely everywhere they're consumed."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={_quote_env_value(updates[key])}"
            seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={_quote_env_value(value)}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def restart_bot() -> None:
    import subprocess
    try:
        subprocess.run(["systemctl", "restart", "vpnbot"], check=False, timeout=15)
    except Exception:
        pass


async def db():
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn


def _telegram_proxy() -> str | None:
    if cfg.proxy_enabled:
        return f"socks5://{cfg.proxy_host}:{cfg.proxy_port}"
    return None


async def send_telegram_message(chat_id: int, text: str) -> bool:
    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        async with httpx.AsyncClient(proxy=_telegram_proxy(), timeout=15) as client:
            r = await client.post(url, json=payload)
            return r.status_code == 200
    except Exception:
        return False


async def send_telegram_photo(chat_id: int, photo_bytes: bytes, caption: str = "") -> None:
    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendPhoto"
    files = {"photo": ("qr.png", photo_bytes, "image/png")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    try:
        async with httpx.AsyncClient(proxy=_telegram_proxy(), timeout=20) as client:
            await client.post(url, data=data, files=files)
    except Exception:
        pass


async def send_log(text: str) -> None:
    if not cfg.log_channel_id:
        return
    await send_telegram_message(cfg.log_channel_id, text)


async def get_setting(conn: aiosqlite.Connection, key: str, default: str = "") -> str:
    async with conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
        return row["value"] if row else default


async def set_setting(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO bot_settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    await conn.commit()


async def clear_reminder(conn: aiosqlite.Connection, telegram_id: int, client_email: str) -> None:
    await conn.execute(
        "DELETE FROM reminder_sent WHERE telegram_id=? AND client_email=?",
        (telegram_id, client_email),
    )


async def apply_referral_bonus(conn: aiosqlite.Connection, tg_id: int, amount: int) -> None:
    referral_enabled = await get_setting(conn, "referral_enabled", "1")
    if referral_enabled != "1":
        return

    async with conn.execute(
        "SELECT inviter_id FROM referrals WHERE invitee_id=?", (tg_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return

    inviter_id = row["inviter_id"]
    bonus = int(amount * 0.05)
    if bonus <= 0:
        return

    await conn.execute(
        "INSERT OR IGNORE INTO wallets(telegram_id, balance) VALUES(?, 0)", (inviter_id,)
    )
    await conn.execute(
        "UPDATE wallets SET balance = balance + ? WHERE telegram_id = ?", (bonus, inviter_id)
    )
    await conn.execute(
        "INSERT INTO transactions(telegram_id, amount, description) VALUES(?, ?, ?)",
        (inviter_id, bonus, f"پاداش ۵٪ دعوت کاربر {tg_id}"),
    )
    await conn.commit()

    async with conn.execute(
        "SELECT balance FROM wallets WHERE telegram_id=?", (inviter_id,)
    ) as cur:
        inviter_bal = (await cur.fetchone())["balance"]

    await send_telegram_message(
        inviter_id,
        f"🎁 <b>پاداش دعوت!</b>\n\n"
        f"یکی از دوستانی که دعوت کردید اولین شارژ کیف پولش را انجام داد.\n"
        f"💰 <b>{bonus:,} تومان</b> (۵٪) به کیف پول شما اضافه شد!\n"
        f"💳 موجودی جدید: {inviter_bal:,} تومان",
    )
    await send_log(
        f"🎁 <b>پاداش دعوت</b>\n\n"
        f"👤 دعوت‌کننده: <code>{inviter_id}</code>\n"
        f"👤 دعوت‌شده: <code>{tg_id}</code>\n"
        f"💰 مبلغ شارژ: {amount:,} تومان\n"
        f"🎁 پاداش ۵٪: {bonus:,} تومان"
    )
    await conn.execute("DELETE FROM referrals WHERE invitee_id=?", (tg_id,))
    await conn.commit()

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if check_session(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == PANEL_ADMIN_USER and password == PANEL_ADMIN_PASS:
        token = new_session()
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=SESSION_TTL)
        return resp
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "نام کاربری یا رمز عبور اشتباه است."}
    )


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        SESSIONS.pop(token, None)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        async with conn.execute("SELECT COUNT(*) c FROM wallets") as cur:
            total_users = (await cur.fetchone())["c"]
        async with conn.execute("SELECT COUNT(*) c FROM orders") as cur:
            total_orders = (await cur.fetchone())["c"]
        async with conn.execute("SELECT COALESCE(SUM(amount_paid),0) s FROM orders") as cur:
            total_revenue = (await cur.fetchone())["s"]
        async with conn.execute(
            "SELECT COALESCE(SUM(amount_paid),0) s FROM orders WHERE created_at >= date('now')"
        ) as cur:
            today_revenue = (await cur.fetchone())["s"]
        async with conn.execute(
            "SELECT COUNT(*) c FROM pending_payments WHERE status='pending'"
        ) as cur:
            pending_count = (await cur.fetchone())["c"]
        async with conn.execute("SELECT COALESCE(SUM(balance),0) s FROM wallets") as cur:
            total_wallet = (await cur.fetchone())["s"]
    finally:
        await conn.close()

    bot_status = "running" if _service_is_active("vpnbot") else "stopped"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "total_users": total_users,
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "today_revenue": today_revenue,
            "pending_count": pending_count,
            "total_wallet": total_wallet,
            "bot_status": bot_status,
        },
    )


def _service_is_active(service_name: str) -> bool:
    import subprocess
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name], timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


@app.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    backup_dir = BOT_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    backups = sorted(backup_dir.glob("backup_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = [
        {"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1)}
        for p in backups
    ]
    return templates.TemplateResponse("backup.html", {"request": request, "backups": items})


@app.post("/backup/create")
async def backup_create(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    import tarfile
    from datetime import datetime

    backup_dir = BOT_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = backup_dir / f"backup_{ts}.tar.gz"

    items = ["config.py", "database.py", "main.py", "requirements.txt", "vpn_bot.db",
             "handlers", "keyboards", "middlewares", "services", "utils", "scripts"]
    with tarfile.open(out_path, "w:gz") as tar:
        env_file = BOT_DIR / ".env"
        if env_file.exists():
            tar.add(env_file, arcname=".env")
        for item in items:
            p = BOT_DIR / item
            if p.exists():
                tar.add(p, arcname=item)

    return RedirectResponse("/backup?ok=بکاپ ساخته شد", status_code=303)


@app.get("/backup/{filename}/download")
async def backup_download(filename: str, request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    from fastapi.responses import FileResponse

    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = BOT_DIR / "backups" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")

    return FileResponse(path, filename=filename, media_type="application/gzip")


@app.post("/backup/{filename}/delete")
async def backup_delete(filename: str, request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    if "/" in filename or ".." in filename:
        return RedirectResponse("/backup?err=نام فایل نامعتبر", status_code=303)

    path = BOT_DIR / "backups" / filename
    if path.exists():
        path.unlink()

    return RedirectResponse("/backup?ok=بکاپ حذف شد", status_code=303)


@app.post("/backup/upload-restore")
async def backup_upload_restore(request: Request, backup_file: UploadFile = FastAPIFile(...)):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    import tarfile
    import subprocess

    if not backup_file.filename.endswith(".tar.gz"):
        return RedirectResponse("/backup?err=فایل باید .tar.gz باشد", status_code=303)

    backup_dir = BOT_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    tmp_path = backup_dir / f"_uploaded_{backup_file.filename}"

    content_bytes = await backup_file.read()
    tmp_path.write_bytes(content_bytes)

    try:
        subprocess.run(["systemctl", "stop", "vpnbot"], check=False, timeout=15)
        with tarfile.open(tmp_path, "r:gz") as tar:
            tar.extractall(path=BOT_DIR)
        subprocess.run(["systemctl", "start", "vpnbot"], check=False, timeout=15)
    except Exception as e:
        return RedirectResponse(f"/backup?err=خطا در ریستور: {e}", status_code=303)
    finally:
        tmp_path.unlink(missing_ok=True)

    return RedirectResponse("/backup?ok=ریستور کامل شد و بات ری‌استارت شد", status_code=303)


@app.post("/backup/{filename}/restore")
async def backup_restore_existing(filename: str, request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    import tarfile
    import subprocess

    if "/" in filename or ".." in filename:
        return RedirectResponse("/backup?err=نام فایل نامعتبر", status_code=303)

    path = BOT_DIR / "backups" / filename
    if not path.exists():
        return RedirectResponse("/backup?err=فایل پیدا نشد", status_code=303)

    try:
        subprocess.run(["systemctl", "stop", "vpnbot"], check=False, timeout=15)
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(path=BOT_DIR)
        subprocess.run(["systemctl", "start", "vpnbot"], check=False, timeout=15)
    except Exception as e:
        return RedirectResponse(f"/backup?err=خطا در ریستور: {e}", status_code=303)

    return RedirectResponse("/backup?ok=ریستور کامل شد و بات ری‌استارت شد", status_code=303)


@app.post("/bot/{action}")
async def bot_control(action: str, request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    if action not in ("start", "stop", "restart"):
        return RedirectResponse("/?err=دستور نامعتبر", status_code=303)

    import subprocess
    try:
        subprocess.run(["systemctl", action, "vpnbot"], check=False, timeout=15)
    except Exception:
        pass

    labels = {"start": "استارت شد", "stop": "متوقف شد", "restart": "ری‌استارت شد"}
    return RedirectResponse(f"/?ok=بات {labels[action]}", status_code=303)

@app.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        async with conn.execute(
            "SELECT * FROM pending_payments WHERE status='pending' ORDER BY created_at ASC"
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await conn.close()

    payments = []
    for r in rows:
        meta = {}
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            pass
        payments.append(
            {
                "id": r["id"],
                "telegram_id": r["telegram_id"],
                "amount": r["amount"],
                "purpose": r["purpose"],
                "meta": meta,
                "created_at": r["created_at"],
                "receipt_file_id": r["receipt_file_id"],
            }
        )

    return templates.TemplateResponse("payments.html", {"request": request, "payments": payments})


@app.get("/payments/{payment_id}/receipt")
async def payment_receipt(payment_id: int, request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        async with conn.execute(
            "SELECT receipt_file_id FROM pending_payments WHERE id=?", (payment_id,)
        ) as cur:
            row = await cur.fetchone()
    finally:
        await conn.close()

    if not row or not row["receipt_file_id"]:
        raise HTTPException(status_code=404, detail="Receipt not found")

    file_id = row["receipt_file_id"]

    async with httpx.AsyncClient(proxy=_telegram_proxy(), timeout=20) as client:
        get_file_resp = await client.get(
            f"https://api.telegram.org/bot{cfg.bot_token}/getFile",
            params={"file_id": file_id},
        )
        get_file_data = get_file_resp.json()
        if not get_file_data.get("ok"):
            raise HTTPException(status_code=502, detail="Could not fetch receipt from Telegram")

        file_path = get_file_data["result"]["file_path"]
        file_resp = await client.get(
            f"https://api.telegram.org/file/bot{cfg.bot_token}/{file_path}"
        )

    content_type = file_resp.headers.get("content-type", "image/jpeg")
    return Response(content=file_resp.content, media_type=content_type)


@app.post("/payments/{payment_id}/approve")
async def approve_payment(payment_id: int, request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        async with conn.execute("SELECT * FROM pending_payments WHERE id=?", (payment_id,)) as cur:
            payment = await cur.fetchone()

        if not payment or payment["status"] != "pending":
            return RedirectResponse("/payments", status_code=303)

        await conn.execute(
            "UPDATE pending_payments SET status='approved', reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (payment_id,),
        )
        await conn.commit()

        tg_id = payment["telegram_id"]
        amount = payment["amount"]
        purpose = payment["purpose"]
        meta = json.loads(payment["meta"] or "{}")

        if purpose == "wallet":
            await conn.execute(
                "INSERT OR IGNORE INTO wallets(telegram_id, balance) VALUES(?, 0)", (tg_id,)
            )
            await conn.execute(
                "UPDATE wallets SET balance = balance + ? WHERE telegram_id = ?", (amount, tg_id)
            )
            await conn.execute(
                "INSERT INTO transactions(telegram_id, amount, description) VALUES(?, ?, ?)",
                (tg_id, amount, "شارژ کیف پول (پنل وب)"),
            )
            await conn.commit()
            async with conn.execute(
                "SELECT balance FROM wallets WHERE telegram_id=?", (tg_id,)
            ) as cur:
                new_bal = (await cur.fetchone())["balance"]

            await send_telegram_message(
                tg_id,
                f"✅ پرداخت تایید شد!\n💰 {amount:,} تومان به کیف پول اضافه شد.\n💳 موجودی: {new_bal:,} تومان",
            )
            await send_log(
                f"💳 <b>شارژ کیف پول</b>\n\n🆔 ID: <code>{tg_id}</code>\n💰 {amount:,} تومان\n💳 موجودی جدید: {new_bal:,} تومان"
            )
            await apply_referral_bonus(conn, tg_id, amount)

        elif purpose == "purchase":
            gb = meta["gb"]
            service_name = meta.get("service_name", "")
            email, sub_id = await panel.create_client(gb=gb, days=30, email=service_name)
            url = panel.sub_url(sub_id)
            await conn.execute(
                "INSERT INTO orders(telegram_id, client_email, gb, amount_paid, order_type) VALUES(?,?,?,?,?)",
                (tg_id, email, gb, amount, "new"),
            )
            await conn.commit()

            await send_telegram_message(
                tg_id,
                f"🎉 <b>سرویس شما آماده است!</b>\n\n📦 حجم: {gb} گیگابایت\n📅 مدت: ۳۰ روز\n\n🔗 لینک اشتراک:\n<code>{url}</code>",
            )
            qr_bytes = make_qr(url)
            await send_telegram_photo(tg_id, qr_bytes, caption="📱 QR Code لینک اشتراک")
            await send_log(
                f"🛒 <b>خرید سرویس جدید (پنل وب)</b>\n\n🆔 ID: <code>{tg_id}</code>\n📧 Email: <code>{email}</code>\n"
                f"📦 حجم: {gb} گیگابایت\n💰 مبلغ: {amount:,} تومان\n🔗 لینک: <code>{url}</code>"
            )

        elif purpose == "renew":
            email = meta["client_email"]
            gb = meta["gb"]
            await panel.renew_client(email, gb)
            await conn.execute(
                "INSERT INTO orders(telegram_id, client_email, gb, amount_paid, order_type) VALUES(?,?,?,?,?)",
                (tg_id, email, gb, amount, "renew"),
            )
            await clear_reminder(conn, tg_id, email)
            await conn.commit()

            await send_telegram_message(
                tg_id, f"✅ سرویس تمدید شد!\n📦 {gb} گیگابایت اضافه شد\n📅 ۳۰ روز به انقضا اضافه شد"
            )
            await send_log(
                f"🔁 <b>تمدید سرویس (پنل وب)</b>\n\n🆔 ID: <code>{tg_id}</code>\n📧 Email: <code>{email}</code>\n"
                f"📦 حجم اضافه: {gb} گیگابایت\n💰 مبلغ: {amount:,} تومان"
            )

    finally:
        await conn.close()

    return RedirectResponse("/payments", status_code=303)


@app.post("/payments/{payment_id}/reject")
async def reject_payment(payment_id: int, request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        async with conn.execute("SELECT * FROM pending_payments WHERE id=?", (payment_id,)) as cur:
            payment = await cur.fetchone()

        if payment and payment["status"] == "pending":
            await conn.execute(
                "UPDATE pending_payments SET status='rejected', reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                (payment_id,),
            )
            await conn.commit()
            await send_telegram_message(
                payment["telegram_id"], "❌ پرداخت شما تایید نشد.\nدر صورت نیاز با پشتیبانی تماس بگیرید."
            )
            await send_log(
                f"❌ <b>پرداخت رد شد (پنل وب)</b>\n\n🆔 ID: <code>{payment['telegram_id']}</code>\n💰 مبلغ: {payment['amount']:,} تومان"
            )
    finally:
        await conn.close()

    return RedirectResponse("/payments", status_code=303)

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        prices = {
            "low": int(await get_setting(conn, "price_low", str(cfg.price_low))),
            "mid": int(await get_setting(conn, "price_mid", str(cfg.price_mid))),
            "high": int(await get_setting(conn, "price_high", str(cfg.price_high))),
            "tier1_max": int(await get_setting(conn, "price_tier1_max", str(cfg.price_tier1_max))),
            "tier2_max": int(await get_setting(conn, "price_tier2_max", str(cfg.price_tier2_max))),
        }
        referral_enabled = await get_setting(conn, "referral_enabled", "1")
        trial_enabled = await get_setting(conn, "trial_enabled", "1")
    finally:
        await conn.close()

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "prices": prices,
            "referral_enabled": referral_enabled,
            "trial_enabled": trial_enabled,
        },
    )


@app.post("/settings/prices")
async def settings_prices(
    request: Request,
    tier1: int = Form(...),
    tier2: int = Form(...),
    low: int = Form(...),
    mid: int = Form(...),
    high: int = Form(...),
):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        await set_setting(conn, "price_tier1_max", str(tier1))
        await set_setting(conn, "price_tier2_max", str(tier2))
        await set_setting(conn, "price_low", str(low))
        await set_setting(conn, "price_mid", str(mid))
        await set_setting(conn, "price_high", str(high))
    finally:
        await conn.close()

    return RedirectResponse("/settings?ok=قیمت‌ها ذخیره شد", status_code=303)


@app.post("/settings/toggle")
async def settings_toggle(request: Request, key: str = Form(...), value: str = Form(...)):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    if key not in ("referral_enabled", "trial_enabled"):
        return RedirectResponse("/settings", status_code=303)

    conn = await db()
    try:
        await set_setting(conn, key, value)
    finally:
        await conn.close()

    return RedirectResponse("/settings?ok=تنظیمات به‌روزرسانی شد", status_code=303)

def _parse_inbounds_raw(raw: str) -> list[dict]:
    """INBOUNDS format: id:remark:flow,id:remark:flow,..."""
    items = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        items.append(
            {
                "id": parts[0].strip() if len(parts) > 0 else "",
                "remark": parts[1].strip() if len(parts) > 1 else "",
                "flow": parts[2].strip() if len(parts) > 2 else "",
            }
        )
    return items


@app.get("/settings/panel", response_class=HTMLResponse)
async def settings_panel_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    env = env_get_all()
    current_inbounds = _parse_inbounds_raw(env.get("INBOUNDS", ""))

    return templates.TemplateResponse(
        "settings_panel.html",
        {
            "request": request,
            "panel_url": env.get("PANEL_URL", ""),
            "panel_path": env.get("PANEL_PATH", ""),
            "panel_api_token": env.get("PANEL_API_TOKEN", ""),
            "sub_base_url": env.get("SUB_BASE_URL", ""),
            "current_inbounds": current_inbounds,
            "fetched_inbounds": None,
            "fetch_error": None,
        },
    )


@app.post("/settings/panel/connection")
async def settings_panel_connection(
    request: Request,
    panel_url: str = Form(...),
    panel_path: str = Form(...),
    panel_api_token: str = Form(...),
    sub_base_url: str = Form(...),
):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    env_set_many(
        {
            "PANEL_URL": panel_url.rstrip("/"),
            "PANEL_PATH": panel_path.rstrip("/"),
            "PANEL_API_TOKEN": panel_api_token,
            "SUB_BASE_URL": sub_base_url.rstrip("/"),
        }
    )
    restart_bot()
    return RedirectResponse(
        "/settings/panel?ok=اطلاعات پنل ذخیره و بات ری‌استارت شد", status_code=303
    )


@app.post("/settings/panel/fetch", response_class=HTMLResponse)
async def settings_panel_fetch(request: Request):
    """Fetch the inbound list from the panel and show it for selection."""
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    env = env_get_all()
    panel_url = env.get("PANEL_URL", "")
    panel_path = env.get("PANEL_PATH", "")
    panel_api_token = env.get("PANEL_API_TOKEN", "")
    current_inbounds = _parse_inbounds_raw(env.get("INBOUNDS", ""))

    fetched_inbounds = None
    fetch_error = None
    try:
        async with httpx.AsyncClient(verify=False, proxy=_telegram_proxy(), timeout=15) as client:
            resp = await client.get(
                f"{panel_url}{panel_path}/panel/api/inbounds/list",
                headers={"Authorization": f"Bearer {panel_api_token}"},
            )
            data = resp.json()
        if not data.get("success"):
            fetch_error = "پنل درخواست را رد کرد. توکن یا آدرس را بررسی کنید."
        else:
            fetched_inbounds = []
            for inb in data.get("obj", []):
                stream = inb.get("streamSettings", {})
                if isinstance(stream, str):
                    try:
                        stream = json.loads(stream)
                    except Exception:
                        stream = {}
                is_reality = stream.get("security") == "reality"
                fetched_inbounds.append(
                    {
                        "id": str(inb.get("id")),
                        "remark": inb.get("remark") or f"inbound-{inb.get('id')}",
                        "protocol": inb.get("protocol", "?"),
                        "port": inb.get("port", "?"),
                        "is_reality": is_reality,
                    }
                )
    except Exception as e:
        fetch_error = f"اتصال به پنل ناموفق بود: {e}"

    return templates.TemplateResponse(
        "settings_panel.html",
        {
            "request": request,
            "panel_url": panel_url,
            "panel_path": panel_path,
            "panel_api_token": panel_api_token,
            "sub_base_url": env.get("SUB_BASE_URL", ""),
            "current_inbounds": current_inbounds,
            "fetched_inbounds": fetched_inbounds,
            "fetch_error": fetch_error,
        },
    )


@app.post("/settings/panel/inbounds")
async def settings_panel_inbounds(request: Request):
    """
    Save the selected inbounds. Expects repeated form fields:
    - selected_id: list of inbound ids the admin checked
    - vision_id: list of inbound ids for which Vision flow should be enabled
      (only meaningful/shown for Reality inbounds in the template)
    - remark_<id> / protocol_<id> hidden fields to rebuild remarks
    """
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    form = await request.form()
    selected_ids = form.getlist("selected_id")
    vision_ids = set(form.getlist("vision_id"))

    entries = []
    for inb_id in selected_ids:
        remark = form.get(f"remark_{inb_id}", f"inbound-{inb_id}")
        flow = "xtls-rprx-vision" if inb_id in vision_ids else ""
        entries.append(f"{inb_id}:{remark}:{flow}")

    inbounds_raw = ",".join(entries)
    updates = {"INBOUNDS": inbounds_raw}
    if entries:
        updates["INBOUND_ID"] = entries[0].split(":")[0]

    env_set_many(updates)
    restart_bot()

    return RedirectResponse(
        "/settings/panel?ok=اینباندهای انتخاب‌شده ذخیره و بات ری‌استارت شد", status_code=303
    )

@app.get("/settings/bot", response_class=HTMLResponse)
async def settings_bot_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env = env_get_all()
    return templates.TemplateResponse(
        "settings_bot.html",
        {"request": request, "bot_token": env.get("BOT_TOKEN", ""), "admin_ids": env.get("ADMIN_IDS", "")},
    )


@app.post("/settings/bot")
async def settings_bot_save(request: Request, bot_token: str = Form(...), admin_ids: str = Form(...)):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env_set_many({"BOT_TOKEN": bot_token, "ADMIN_IDS": admin_ids})
    restart_bot()
    return RedirectResponse("/settings/bot?ok=ذخیره و ری‌استارت شد", status_code=303)

@app.get("/settings/channel", response_class=HTMLResponse)
async def settings_channel_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env = env_get_all()
    return templates.TemplateResponse(
        "settings_channel.html",
        {
            "request": request,
            "channel_id": env.get("REQUIRED_CHANNEL_ID", "0"),
            "channel_invite": env.get("REQUIRED_CHANNEL_INVITE", ""),
        },
    )


@app.post("/settings/channel")
async def settings_channel_save(request: Request, channel_id: str = Form(""), channel_invite: str = Form("")):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env_set_many({"REQUIRED_CHANNEL_ID": channel_id or "0", "REQUIRED_CHANNEL_INVITE": channel_invite})
    restart_bot()
    return RedirectResponse("/settings/channel?ok=ذخیره و ری‌استارت شد", status_code=303)


@app.post("/settings/channel/disable")
async def settings_channel_disable(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env_set_many({"REQUIRED_CHANNEL_ID": "0", "REQUIRED_CHANNEL_INVITE": ""})
    restart_bot()
    return RedirectResponse("/settings/channel?ok=غیرفعال شد", status_code=303)

@app.get("/settings/proxy", response_class=HTMLResponse)
async def settings_proxy_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env = env_get_all()
    return templates.TemplateResponse(
        "settings_proxy.html",
        {"request": request, "proxy_host": env.get("PROXY_HOST", ""), "proxy_port": env.get("PROXY_PORT", "")},
    )


@app.post("/settings/proxy")
async def settings_proxy_save(request: Request, proxy_host: str = Form(""), proxy_port: str = Form("")):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env_set_many({"PROXY_HOST": proxy_host, "PROXY_PORT": proxy_port})
    restart_bot()
    return RedirectResponse("/settings/proxy?ok=ذخیره و ری‌استارت شد", status_code=303)


@app.post("/settings/proxy/disable")
async def settings_proxy_disable(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env_set_many({"PROXY_HOST": "", "PROXY_PORT": ""})
    restart_bot()
    return RedirectResponse("/settings/proxy?ok=غیرفعال شد", status_code=303)

@app.get("/settings/log", response_class=HTMLResponse)
async def settings_log_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env = env_get_all()
    return templates.TemplateResponse(
        "settings_log.html", {"request": request, "log_channel_id": env.get("LOG_CHANNEL_ID", "0")}
    )


@app.post("/settings/log")
async def settings_log_save(request: Request, log_channel_id: str = Form("")):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env_set_many({"LOG_CHANNEL_ID": log_channel_id or "0"})
    restart_bot()
    return RedirectResponse("/settings/log?ok=ذخیره و ری‌استارت شد", status_code=303)


@app.post("/settings/log/disable")
async def settings_log_disable(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env_set_many({"LOG_CHANNEL_ID": "0"})
    restart_bot()
    return RedirectResponse("/settings/log?ok=غیرفعال شد", status_code=303)

@app.get("/settings/reminder", response_class=HTMLResponse)
async def settings_reminder_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env = env_get_all()
    return templates.TemplateResponse(
        "settings_reminder.html",
        {
            "request": request,
            "reminder_enabled": env.get("REMINDER_ENABLED", "n"),
            "gb_threshold": env.get("REMINDER_GB_THRESHOLD", "3"),
            "days_threshold": env.get("REMINDER_DAYS_THRESHOLD", "3"),
        },
    )


@app.post("/settings/reminder")
async def settings_reminder_save(request: Request, gb_threshold: str = Form(...), days_threshold: str = Form(...)):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env_set_many({"REMINDER_GB_THRESHOLD": gb_threshold, "REMINDER_DAYS_THRESHOLD": days_threshold})
    restart_bot()
    return RedirectResponse("/settings/reminder?ok=ذخیره و ری‌استارت شد", status_code=303)


@app.post("/settings/reminder/toggle")
async def settings_reminder_toggle(request: Request, enabled: str = Form(...)):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env_set_many({"REMINDER_ENABLED": enabled})
    restart_bot()
    return RedirectResponse("/settings/reminder?ok=وضعیت تغییر کرد", status_code=303)

def _parse_cards_raw(raw: str) -> list[dict]:
    items = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 1)
        items.append({"number": parts[0].strip(), "name": parts[1].strip() if len(parts) > 1 else ""})
    return items


@app.get("/settings/cards", response_class=HTMLResponse)
async def settings_cards_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env = env_get_all()
    cards = _parse_cards_raw(env.get("CARDS", ""))
    return templates.TemplateResponse("settings_cards.html", {"request": request, "cards": cards})


@app.post("/settings/cards/add")
async def settings_cards_add(request: Request, number: str = Form(...), name: str = Form(...)):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env = env_get_all()
    cards = _parse_cards_raw(env.get("CARDS", ""))
    cards.append({"number": number.strip(), "name": name.strip()})
    raw = ",".join(f"{c['number']}:{c['name']}" for c in cards)
    env_set_many({"CARDS": raw})
    restart_bot()
    return RedirectResponse("/settings/cards?ok=کارت اضافه شد", status_code=303)


@app.post("/settings/cards/remove")
async def settings_cards_remove(request: Request, index: int = Form(...)):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)
    env = env_get_all()
    cards = _parse_cards_raw(env.get("CARDS", ""))
    if 0 <= index < len(cards):
        cards.pop(index)
    raw = ",".join(f"{c['number']}:{c['name']}" for c in cards)
    env_set_many({"CARDS": raw})
    restart_bot()
    return RedirectResponse("/settings/cards?ok=کارت حذف شد", status_code=303)

@app.get("/codes", response_class=HTMLResponse)
async def codes_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        async with conn.execute("SELECT * FROM discount_codes ORDER BY id DESC") as cur:
            rows = await cur.fetchall()
    finally:
        await conn.close()

    codes = [dict(r) for r in rows]
    return templates.TemplateResponse("codes.html", {"request": request, "codes": codes})


@app.post("/codes/add")
async def codes_add(
    request: Request,
    code: str = Form(...),
    percent: int = Form(...),
    max_uses: int = Form(...),
):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        try:
            await conn.execute(
                "INSERT INTO discount_codes(code, percent, max_uses) VALUES(?,?,?)",
                (code.strip().upper(), percent, max_uses),
            )
            await conn.commit()
        except aiosqlite.IntegrityError:
            return RedirectResponse(
                f"/codes?err=کد {code.strip().upper()} از قبل وجود دارد", status_code=303
            )
    finally:
        await conn.close()

    return RedirectResponse("/codes?ok=کد تخفیف ساخته شد", status_code=303)


@app.post("/codes/{code}/delete")
async def codes_delete(code: str, request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        await conn.execute("DELETE FROM discount_codes WHERE code=?", (code.strip().upper(),))
        await conn.commit()
    finally:
        await conn.close()

    return RedirectResponse("/codes?ok=کد حذف شد", status_code=303)

@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        async with conn.execute("SELECT COUNT(*) c FROM wallets") as cur:
            user_count = (await cur.fetchone())["c"]
    finally:
        await conn.close()

    return templates.TemplateResponse(
        "broadcast.html", {"request": request, "user_count": user_count, "result": None}
    )


@app.post("/broadcast/send", response_class=HTMLResponse)
async def broadcast_send(request: Request, message: str = Form(...)):
    if not check_session(request):
        return RedirectResponse("/login", status_code=303)

    conn = await db()
    try:
        async with conn.execute("SELECT telegram_id FROM wallets") as cur:
            rows = await cur.fetchall()
        user_ids = [r["telegram_id"] for r in rows]
    finally:
        await conn.close()

    sent, failed = 0, 0
    for uid in user_ids:
        ok = await send_telegram_message(uid, message)
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)

    return templates.TemplateResponse(
        "broadcast.html",
        {
            "request": request,
            "user_count": len(user_ids),
            "result": {"sent": sent, "failed": failed},
        },
    )
