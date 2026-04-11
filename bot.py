import os
import io
import json
import re
import logging
import time
import urllib.request
import base64
import threading
from datetime import datetime, timedelta, timezone

BRT = timezone(timedelta(hours=-3))
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes
)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_URL = os.getenv("API_URL", "https://web-production-d061f.up.railway.app")
DATABASE_URL = os.getenv("DATABASE_URL", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "netflixiptv5-hub/trocasdolk")
ADMIN_IDS = [925542353]

# Bot de suporte token (para enviar backups via este bot)
SUPPORT_BOT_TOKEN = BOT_TOKEN

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================
# MANUTENÇÃO - LOCAL (sem depender de API)
# ============================================
_MAINTENANCE = False
_MAINTENANCE_LOCK = threading.Lock()


def is_maintenance():
    """Check maintenance status - local variable, instant."""
    return _MAINTENANCE


def set_maintenance(value: bool):
    """Set maintenance on/off."""
    global _MAINTENANCE
    with _MAINTENANCE_LOCK:
        _MAINTENANCE = value
    # Also sync to DB via API (best effort)
    try:
        action = "on" if value else "off"
        payload = json.dumps({"action": action, "secret": "lkstore2026"}).encode("utf-8")
        req = urllib.request.Request(
            f"{API_URL}/api/maintenance",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=3)
    except:
        pass


def load_maintenance_from_db():
    """Load maintenance status from DB on startup."""
    global _MAINTENANCE
    try:
        req = urllib.request.Request(f"{API_URL}/api/maintenance", method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read())
        _MAINTENANCE = data.get("maintenance", False)
        logger.info(f"[MAINT] Loaded from DB: {'ON' if _MAINTENANCE else 'OFF'}")
    except:
        logger.warning("[MAINT] Could not load from DB, defaulting to OFF")


# Conversation states
ESCOLHER_TIPO, AGUARDANDO_EMAIL, AGUARDANDO_EMAIL_SENHA = range(3)


def create_ticket_api(data):
    """Send ticket to Flask API. Returns dict with success + ticket_id."""
    # Tenta URL interna primeiro (Railway), depois pública
    urls = [f"{API_URL}/api/ticket"]
    # Se estiver no Railway, tenta a URL interna tb
    internal = os.getenv("RAILWAY_PRIVATE_DOMAIN")
    if internal:
        port = os.getenv("PORT", "5000")
        urls.insert(0, f"http://web.railway.internal:{port}/api/ticket")

    payload = json.dumps(data).encode("utf-8")
    for url in urls:
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            logger.info(f"[TICKET] Criado OK via {url}: {result}")
            return result
        except Exception as e:
            logger.error(f"[TICKET] Erro em {url}: {e}")
            continue
    return None


def cancel_last_ticket_api(chat_id):
    """Cancel the most recent pending ticket (last 5 min) for this chat_id."""
    urls = [f"{API_URL}/api/ticket/ultimo/{chat_id}/cancelar"]
    internal = os.getenv("RAILWAY_PRIVATE_DOMAIN")
    if internal:
        port = os.getenv("PORT", "5000")
        urls.insert(0, f"http://web.railway.internal:{port}/api/ticket/ultimo/{chat_id}/cancelar")

    for url in urls:
        try:
            req = urllib.request.Request(url, data=b"", headers={"Content-Type": "application/json"}, method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            logger.info(f"[TICKET] Cancelado ultimo via {url}: {result}")
            return result
        except Exception as e:
            logger.error(f"[TICKET] Erro cancel ultimo em {url}: {e}")
            continue
    return None


def get_all_chat_ids():
    """Get all unique chat IDs via API."""
    urls = [f"{API_URL}/api/chat-ids"]
    internal = os.getenv("RAILWAY_PRIVATE_DOMAIN")
    if internal:
        port = os.getenv("PORT", "5000")
        urls.insert(0, f"http://web.railway.internal:{port}/api/chat-ids")

    for url in urls:
        try:
            req = urllib.request.Request(url, method="GET")
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            return data.get("chat_ids", [])
        except Exception as e:
            logger.error(f"[SPAM] Erro em {url}: {e}")
            continue
    return []


def extrair_email_senha(texto):
    texto = texto.strip()
    normalizado = texto.replace(">", " ").replace(":", " ")
    partes = normalizado.split()
    if not partes:
        return None, None
    email = None
    senha = None
    for i, parte in enumerate(partes):
        if "@" in parte and "." in parte:
            email = parte
            resto = [p for j, p in enumerate(partes) if j != i]
            if resto:
                senha = " ".join(resto)
            break
    if not email and len(partes) >= 1:
        if "@" in partes[0]:
            email = partes[0]
            if len(partes) > 1:
                senha = " ".join(partes[1:])
    return email, senha


async def check_maint(update: Update) -> bool:
    """Returns True and sends maintenance message if in maintenance (non-admin)."""
    if not is_maintenance():
        return False
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id in ADMIN_IDS:
        return False
    text = (
        "🔧 *SUPORTE EM MANUTENÇÃO*\n\n"
        "O suporte está temporariamente indisponível.\n"
        "Volte mais tarde!"
    )
    if update.callback_query:
        try:
            await update.callback_query.answer("🔧 Suporte em manutenção!", show_alert=True)
            await update.callback_query.edit_message_text(text=text, parse_mode="Markdown")
        except:
            pass
    elif update.message:
        await update.message.reply_text(text=text, parse_mode="Markdown")
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu principal do bot."""
    if await check_maint(update):
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("🔑 REDEFINIR SENHA", callback_data="redefinir_senha")],
        [InlineKeyboardButton("📺 CONTAS CAÍDA TELA", callback_data="tela_caida")],
        [InlineKeyboardButton("💻 CONTAS CAÍDA COMPLETAS", callback_data="completa_caida")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🔐 *SUPORTE DE QUEDA/SENHA*\n\n"
        "Olá! Selecione o tipo de suporte que precisa:"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=text, reply_markup=reply_markup, parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text=text, reply_markup=reply_markup, parse_mode="Markdown"
        )
    return ESCOLHER_TIPO


async def tipo_escolhido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_maint(update):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    tipo = query.data
    context.user_data["tipo"] = tipo
    nomes = {
        "redefinir_senha": "🔑 REDEFINIR SENHA",
        "tela_caida": "📺 CONTAS CAÍDA TELA",
        "completa_caida": "💻 CONTAS CAÍDA COMPLETAS"
    }
    nome = nomes.get(tipo, tipo)
    voltar_btn = [[InlineKeyboardButton("⬅️ Voltar", callback_data="voltar_menu")]]

    if tipo == "redefinir_senha":
        await query.edit_message_text(
            f"*{nome}*\n\n📧 Digite o *email* da conta:\n\n_Exemplo: usuario@email.com_",
            reply_markup=InlineKeyboardMarkup(voltar_btn), parse_mode="Markdown"
        )
        return AGUARDANDO_EMAIL
    else:
        await query.edit_message_text(
            f"*{nome}*\n\n📧 Digite o *email e a senha* da conta:\n\n"
            "_Aceito em qualquer formato:_\n`email senha`\n`email:senha`\n`email>senha`\n\n"
            "_Exemplo: usuario@email.com minhasenha123_",
            reply_markup=InlineKeyboardMarkup(voltar_btn), parse_mode="Markdown"
        )
        return AGUARDANDO_EMAIL_SENHA


async def receber_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_maint(update):
        return ConversationHandler.END
    texto = update.message.text.strip()
    email, senha = extrair_email_senha(texto)
    if not email or "@" not in email:
        voltar_btn = [[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="voltar_menu")]]
        await update.message.reply_text(
            "❌ Email inválido. Tente novamente.\n\n📧 Digite o *email* da conta:\n\n"
            "_Ou clique em Voltar para escolher outra opção._",
            reply_markup=InlineKeyboardMarkup(voltar_btn), parse_mode="Markdown"
        )
        return AGUARDANDO_EMAIL
    user = update.message.from_user
    data = {
        "type": context.user_data.get("tipo", "redefinir_senha"),
        "email": email, "senha": senha,
        "telegram_chat_id": str(update.message.chat_id),
        "telegram_username": user.username or "",
        "telegram_name": user.first_name or ""
    }
    result = create_ticket_api(data)
    if result and result.get("success"):
        senha_info = f"\n🔑 Senha: `{senha}`" if senha else ""
        keyboard = [[InlineKeyboardButton("🔄 Novo Suporte", callback_data="voltar_menu")]]
        await update.message.reply_text(
            "✅ *Solicitação Enviada!*\n\n"
            f"📧 Email: `{email}`{senha_info}\n📋 Tipo: Redefinir Senha\n\n"
            "⏳ Aguarde, nossa equipe irá resolver e você receberá uma resposta aqui!\n\n"
            "💡 _Se quiser cancelar, digite /cancelar em até 5 minutos._",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    else:
        keyboard = [[InlineKeyboardButton("🔄 Tentar Novamente", callback_data="voltar_menu")]]
        await update.message.reply_text(
            "❌ Erro ao enviar solicitação. Tente novamente.",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    return ConversationHandler.END


async def receber_email_senha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_maint(update):
        return ConversationHandler.END
    texto = update.message.text.strip()
    email, senha = extrair_email_senha(texto)
    if not email or "@" not in email:
        voltar_btn = [[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="voltar_menu")]]
        await update.message.reply_text(
            "❌ Email inválido. Tente novamente.\n\n📧 Digite o *email e a senha* da conta:\n\n"
            "_Aceito: email senha, email:senha, email>senha_\n\n"
            "_Ou clique em Voltar para escolher outra opção._",
            reply_markup=InlineKeyboardMarkup(voltar_btn), parse_mode="Markdown"
        )
        return AGUARDANDO_EMAIL_SENHA
    if not senha:
        voltar_btn = [[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="voltar_menu")]]
        await update.message.reply_text(
            "❌ *Senha obrigatória!*\n\nPara este tipo de suporte, preciso do email *e* da senha.\n\n"
            "📧 Digite o *email e a senha* juntos:\n\n"
            "_Aceito: email senha, email:senha, email>senha_\n"
            "_Exemplo: usuario@email.com minhasenha123_\n\n"
            "_Ou clique em Voltar para escolher outra opção._",
            reply_markup=InlineKeyboardMarkup(voltar_btn), parse_mode="Markdown"
        )
        return AGUARDANDO_EMAIL_SENHA
    user = update.message.from_user
    tipo = context.user_data.get("tipo", "tela_caida")
    nomes = {"tela_caida": "Contas Caída Tela", "completa_caida": "Contas Caída Completas"}
    data = {
        "type": tipo, "email": email, "senha": senha,
        "telegram_chat_id": str(update.message.chat_id),
        "telegram_username": user.username or "",
        "telegram_name": user.first_name or ""
    }
    result = create_ticket_api(data)
    if result and result.get("success"):
        tipo_nome = nomes.get(tipo, tipo)
        keyboard = [[InlineKeyboardButton("🔄 Novo Suporte", callback_data="voltar_menu")]]
        await update.message.reply_text(
            "✅ *Solicitação Enviada!*\n\n"
            f"📧 Email: `{email}`\n🔑 Senha: `{senha}`\n"
            f"📋 Tipo: {tipo_nome}\n\n"
            "⏳ Aguarde, nossa equipe irá resolver e você receberá uma resposta aqui!\n\n"
            "💡 _Se quiser cancelar, digite /cancelar em até 5 minutos._",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    else:
        keyboard = [[InlineKeyboardButton("🔄 Tentar Novamente", callback_data="voltar_menu")]]
        await update.message.reply_text(
            "❌ Erro ao enviar solicitação. Tente novamente.",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    return ConversationHandler.END


async def voltar_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_maint(update):
        return ConversationHandler.END
    query = update.callback_query
    if query:
        await query.answer()
    return await start(update, context)


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela a conversa ativa E tenta cancelar o último ticket pendente (5 min)."""
    chat_id = update.message.chat_id
    result = cancel_last_ticket_api(chat_id)

    keyboard = [[InlineKeyboardButton("🔄 Abrir Suporte", callback_data="voltar_menu")]]

    if result and result.get("success"):
        await update.message.reply_text(
            "🗑️ *Solicitação Cancelada!*\n\n"
            "Seu último suporte foi cancelado com sucesso.\n"
            "Caso precise, abra um novo suporte.",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "⚠️ *Nenhum suporte para cancelar.*\n\n"
            "Não há ticket pendente nos últimos 5 minutos.\n"
            "O ticket pode já ter sido resolvido ou expirou o prazo.",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    return ConversationHandler.END


# ============================================
# /manutencao - ADMIN ONLY - toggle on/off
# ============================================
async def manutencao_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle maintenance mode - admin only. Instant, local."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    current = is_maintenance()
    new_val = not current
    set_maintenance(new_val)
    
    logger.warning(f"[MAINT] Admin {update.effective_user.id} alterou: {'ON' if new_val else 'OFF'}")
    
    if new_val:
        status_text = (
            "🔧 *MANUTENÇÃO ATIVADA* ⛔\n\n"
            "O bot de suporte está *PARADO*.\n"
            "Nenhum usuário consegue abrir ticket.\n\n"
            "Para desativar: /manutencao"
        )
    else:
        status_text = (
            "✅ *MANUTENÇÃO DESATIVADA*\n\n"
            "O bot de suporte está *ONLINE* novamente.\n"
            "Usuários podem abrir tickets normalmente.\n\n"
            "Para ativar: /manutencao"
        )
    
    await update.message.reply_text(status_text, parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current bot status - admin only."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    current = is_maintenance()
    
    # Checar também o que a API diz
    api_val = "?"
    try:
        req = urllib.request.Request(f"{API_URL}/api/maintenance", method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read())
        api_val = "ON 🔧" if data.get("maintenance", False) else "OFF ✅"
    except:
        api_val = "❌ API indisponível"
    
    status_text = (
        f"📊 *STATUS DO BOT*\n\n"
        f"🤖 Estado local: {'🔧 MANUTENÇÃO' if current else '✅ ONLINE'}\n"
        f"🌐 API (DB): {api_val}\n\n"
        f"Para alternar: /manutencao"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown")


# ============================================
# BACKUP AUTOMÁTICO - A CADA 10 MINUTOS
# ============================================
def get_db_backup():
    """Export all tickets from PostgreSQL as JSON."""
    import psycopg2
    if not DATABASE_URL:
        logger.warning("[BACKUP] DATABASE_URL não definido, pulando backup")
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT * FROM tickets ORDER BY id ASC")
        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        tickets = []
        for row in rows:
            ticket = {}
            for i, col in enumerate(cols):
                val = row[i]
                if hasattr(val, 'isoformat'):
                    val = val.isoformat()
                ticket[col] = val
            tickets.append(ticket)
        cur.execute("SELECT * FROM config")
        config_rows = cur.fetchall()
        config = {r[0]: r[1] for r in config_rows}
        cur.close()
        conn.close()
        return {
            "backup_date": datetime.now(BRT).isoformat(),
            "total_tickets": len(tickets),
            "tickets": tickets,
            "config": config
        }
    except Exception as e:
        logger.error(f"[BACKUP] Erro ao exportar DB: {e}")
        return None


def send_backup_telegram(backup_data):
    """Send backup JSON file to ALL admin IDs via the support bot."""
    try:
        now = datetime.now(BRT).strftime("%Y%m%d_%H%M")
        filename = f"backup_trocasdolk_{now}.json"
        json_bytes = json.dumps(backup_data, indent=2, ensure_ascii=False).encode("utf-8")

        total = backup_data["total_tickets"]
        pendentes = sum(1 for t in backup_data["tickets"] if t.get("status") == "pendente")
        resolvidos = sum(1 for t in backup_data["tickets"] if t.get("status") == "resolvido")
        reprovados = sum(1 for t in backup_data["tickets"] if t.get("status") == "reprovado")

        caption = (
            f"💾 BACKUP SUPORTE\n"
            f"📅 {datetime.now(BRT).strftime('%d/%m/%Y %H:%M')}\n\n"
            f"📊 Total: {total} tickets\n"
            f"⏳ Pendentes: {pendentes}\n"
            f"✅ Resolvidos: {resolvidos}\n"
            f"❌ Reprovados: {reprovados}"
        )

        for admin_id in ADMIN_IDS:
            try:
                boundary = "----BackupBoundary"
                body = (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{admin_id}\r\n'
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                    f"Content-Type: application/json\r\n\r\n"
                ).encode("utf-8") + json_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

                url = f"https://api.telegram.org/bot{SUPPORT_BOT_TOKEN}/sendDocument"
                req = urllib.request.Request(url, data=body)
                req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
                urllib.request.urlopen(req, timeout=30)
            except Exception as e:
                logger.error(f"[BACKUP] Telegram erro para {admin_id}: {e}")

        logger.info(f"[BACKUP] Telegram: enviado OK ({total} tickets)")
        return True
    except Exception as e:
        logger.error(f"[BACKUP] Telegram erro: {e}")
        return False


def send_backup_github(backup_data):
    """Push backup JSON to GitHub repo."""
    if not GITHUB_TOKEN:
        logger.warning("[BACKUP] GITHUB_TOKEN not set, skipping GitHub backup")
        return False
    try:
        now = datetime.now(BRT).strftime("%Y%m%d_%H%M")
        filename = f"backups/backup_{now}.json"
        json_str = json.dumps(backup_data, indent=2, ensure_ascii=False)
        content_b64 = base64.b64encode(json_str.encode("utf-8")).decode("utf-8")

        url_latest = f"https://api.github.com/repos/{GITHUB_REPO}/contents/backups/latest.json"
        sha_latest = None
        try:
            req = urllib.request.Request(url_latest)
            req.add_header("Authorization", f"token {GITHUB_TOKEN}")
            req.add_header("Accept", "application/vnd.github.v3+json")
            resp = urllib.request.urlopen(req, timeout=10)
            existing = json.loads(resp.read())
            sha_latest = existing.get("sha")
        except:
            pass

        url_file = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        payload = {"message": f"Backup automático {now}", "content": content_b64}
        req = urllib.request.Request(url_file, data=json.dumps(payload).encode("utf-8"), method="PUT")
        req.add_header("Authorization", f"token {GITHUB_TOKEN}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/vnd.github.v3+json")
        urllib.request.urlopen(req, timeout=15)

        payload_latest = {"message": f"Backup latest {now}", "content": content_b64}
        if sha_latest:
            payload_latest["sha"] = sha_latest
        req2 = urllib.request.Request(url_latest, data=json.dumps(payload_latest).encode("utf-8"), method="PUT")
        req2.add_header("Authorization", f"token {GITHUB_TOKEN}")
        req2.add_header("Content-Type", "application/json")
        req2.add_header("Accept", "application/vnd.github.v3+json")
        urllib.request.urlopen(req2, timeout=15)

        logger.info(f"[BACKUP] GitHub: enviado OK -> {filename}")
        return True
    except Exception as e:
        logger.error(f"[BACKUP] GitHub erro: {e}")
        return False


def maintenance_sync_loop():
    """Sync maintenance status from DB every 30s.
    This allows the vendas bot to toggle maintenance via the API."""
    global _MAINTENANCE
    time.sleep(15)
    logger.info("[MAINT] Sync loop iniciado - checa API a cada 30s")
    while True:
        try:
            req = urllib.request.Request(f"{API_URL}/api/maintenance", method="GET")
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read())
            db_val = data.get("maintenance", False)
            # Only update if changed externally (not by local /manutencao)
            if db_val != _MAINTENANCE:
                logger.warning(f"[MAINT] ⚠️ Estado mudou via API: {'ON' if db_val else 'OFF'} (era {'ON' if _MAINTENANCE else 'OFF'})")
                _MAINTENANCE = db_val
        except Exception as e:
            # API fora do ar — NÃO muda o estado local
            logger.warning(f"[MAINT] API indisponível, mantendo estado atual ({'ON' if _MAINTENANCE else 'OFF'}): {e}")
        time.sleep(30)


def backup_loop():
    """Run backup every 10 minutes in a background thread."""
    # Espera 10 minutos antes do primeiro backup (evita spam em restart/deploy)
    time.sleep(600)
    logger.info("[BACKUP] Sistema de backup iniciado - a cada 10 minutos")
    while True:
        try:
            backup_data = get_db_backup()
            if backup_data:
                send_backup_telegram(backup_data)
                send_backup_github(backup_data)
        except Exception as e:
            logger.error(f"[BACKUP] Erro geral: {e}")
        time.sleep(600)  # 10 minutos





# ============================================
# /spam — ADMIN: enviar mensagem pra todos
# ============================================
SPAM_AGUARDANDO = 100  # State para ConversationHandler do spam

async def spam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin inicia o spam — envia texto, foto ou vídeo."""
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="spam_cancelar")]]
    await update.message.reply_text(
        "📢 *SPAM — ENVIAR MENSAGEM PARA TODOS*\n\n"
        "Envie agora o que quer mandar:\n\n"
        "📝 *Texto* — digite a mensagem\n"
        "📷 *Foto* — envie a foto (com legenda opcional)\n"
        "🎥 *Vídeo* — envie o vídeo (com legenda opcional)\n\n"
        "_A mensagem será enviada para todos os usuários do suporte._",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )
    return SPAM_AGUARDANDO


async def spam_receber(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recebe a mensagem de spam (texto, foto ou vídeo) e envia pra todos."""
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    msg = update.message
    chat_ids = get_all_chat_ids()
    total = len(chat_ids)
    enviados = 0
    erros = 0

    status_msg = await msg.reply_text(f"📤 Enviando para {total} usuários... aguarde.")

    for cid in chat_ids:
        try:
            if msg.video:
                await context.bot.send_video(
                    chat_id=cid, video=msg.video.file_id,
                    caption=msg.caption or "", parse_mode="Markdown"
                )
            elif msg.photo:
                await context.bot.send_photo(
                    chat_id=cid, photo=msg.photo[-1].file_id,
                    caption=msg.caption or "", parse_mode="Markdown"
                )
            elif msg.text:
                await context.bot.send_message(
                    chat_id=cid, text=msg.text, parse_mode="Markdown"
                )
            else:
                continue
            enviados += 1
        except Exception as e:
            erros += 1
            logger.warning(f"[SPAM] Erro para {cid}: {e}")

        # Delay pra não tomar rate limit do Telegram
        if enviados % 25 == 0:
            await asyncio.sleep(1)

    await status_msg.edit_text(
        f"✅ *Spam concluído!*\n\n"
        f"📤 Enviados: {enviados}\n"
        f"❌ Erros: {erros}\n"
        f"👥 Total: {total}",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def spam_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancelar o envio de spam."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Spam cancelado.", parse_mode="Markdown")
    return ConversationHandler.END


async def post_init(application):
    """Set bot commands menu after startup."""
    # Menu publico - so /start (clientes nao veem /manutencao)
    commands = [
        BotCommand("start", "🏠 Menu Principal"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("✅ Menu de comandos configurado")


def main():
    # Load maintenance status from DB on startup
    load_maintenance_from_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Admin commands
    app.add_handler(CommandHandler("manutencao", manutencao_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # Spam conversation (admin) — ANTES do conv principal
    spam_conv = ConversationHandler(
        entry_points=[CommandHandler("spam", spam_cmd)],
        states={
            SPAM_AGUARDANDO: [
                CallbackQueryHandler(spam_cancelar, pattern="^spam_cancelar$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, spam_receber),
                MessageHandler(filters.PHOTO, spam_receber),
                MessageHandler(filters.VIDEO, spam_receber),
            ],
        },
        fallbacks=[
            CommandHandler("cancelar", cancelar),
            CallbackQueryHandler(spam_cancelar, pattern="^spam_cancelar$"),
        ],
    )
    app.add_handler(spam_conv)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(voltar_menu, pattern="^voltar_menu$"),
        ],
        states={
            ESCOLHER_TIPO: [
                CallbackQueryHandler(tipo_escolhido, pattern="^(redefinir_senha|tela_caida|completa_caida)$"),
                CallbackQueryHandler(voltar_menu, pattern="^voltar_menu$"),
            ],
            AGUARDANDO_EMAIL: [
                CallbackQueryHandler(voltar_menu, pattern="^voltar_menu$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_email),
            ],
            AGUARDANDO_EMAIL_SENHA: [
                CallbackQueryHandler(voltar_menu, pattern="^voltar_menu$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_email_senha),
            ],
        },
        fallbacks=[
            CommandHandler("cancelar", cancelar),
            CommandHandler("start", start),
            CommandHandler("status", status_cmd),
            CommandHandler("manutencao", manutencao_cmd),
            CallbackQueryHandler(voltar_menu, pattern="^voltar_menu$"),
        ],
    )

    app.add_handler(conv_handler)

    # /cancelar fora da conversa (quando já deu END)
    app.add_handler(CommandHandler("cancelar", cancelar))

    # Start maintenance sync thread (syncs with API every 5s for vendas bot toggle)
    maint_thread = threading.Thread(target=maintenance_sync_loop, daemon=True)
    maint_thread.start()

    # Start backup thread
    backup_thread = threading.Thread(target=backup_loop, daemon=True)
    backup_thread.start()

    logger.info("🤖 Bot de suporte iniciado!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
