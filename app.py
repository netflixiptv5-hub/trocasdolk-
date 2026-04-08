import os
import json
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "trocasdolk_secret_2026")

ADMIN_USER = os.getenv("ADMIN_USER", "LK3401")
ADMIN_PASS = os.getenv("ADMIN_PASS", "02022013")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")


# ============================================
# DATABASE - PostgreSQL
# ============================================
import psycopg2
import psycopg2.extras


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            type TEXT NOT NULL,
            email TEXT NOT NULL,
            senha TEXT,
            telegram_chat_id TEXT NOT NULL,
            telegram_username TEXT,
            telegram_name TEXT,
            status TEXT NOT NULL DEFAULT 'pendente',
            resolved_action TEXT,
            resolved_data TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


init_db()


def dict_from_row(cur, row):
    """Convert a row to dict using cursor description."""
    if row is None:
        return None
    cols = [desc[0] for desc in cur.description]
    return dict(zip(cols, row))


def format_date(dt):
    """Format datetime for display as DD/MM/YYYY HH:MM."""
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    return dt.strftime("%d/%m/%Y %H:%M")


# ============================================
# AUTH
# ============================================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "")
        pwd = request.form.get("password", "")
        if user == ADMIN_USER and pwd == ADMIN_PASS:
            session["logged_in"] = True
            session["username"] = user
            return redirect(url_for("dashboard"))
        flash("Usuário ou senha incorretos", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================
# DASHBOARD
# ============================================
@app.route("/")
@login_required
def dashboard():
    conn = get_db()
    cur = conn.cursor()

    # Pendentes - mais antigos primeiro (ASC by timestamp)
    cur.execute("SELECT * FROM tickets WHERE status = 'pendente' ORDER BY created_at ASC")
    cols = [desc[0] for desc in cur.description]
    pendentes_raw = [dict(zip(cols, row)) for row in cur.fetchall()]

    # Format dates for display
    for t in pendentes_raw:
        t["created_at_display"] = format_date(t["created_at"])

    total_pendentes = len(pendentes_raw)

    cur.execute("SELECT COUNT(*) FROM tickets WHERE status = 'resolvido'")
    total_resolvidos = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM tickets WHERE status = 'reprovado'")
    total_reprovados = cur.fetchone()[0]

    # Separate by type
    redefinir = [t for t in pendentes_raw if t["type"] == "redefinir_senha"]
    tela_caida = [t for t in pendentes_raw if t["type"] == "tela_caida"]
    completa_caida = [t for t in pendentes_raw if t["type"] == "completa_caida"]

    # Detect duplicate logins - same email from different users in last 7 days
    seven_days_ago = datetime.now() - timedelta(days=7)
    cur.execute(
        "SELECT id, email, telegram_chat_id, telegram_name, telegram_username, created_at "
        "FROM tickets WHERE created_at >= %s ORDER BY created_at ASC",
        (seven_days_ago,)
    )
    dup_cols = [desc[0] for desc in cur.description]
    all_recent = [dict(zip(dup_cols, row)) for row in cur.fetchall()]

    # Group by email (case insensitive)
    email_users = defaultdict(list)
    for t in all_recent:
        email_lower = t["email"].lower().strip()
        email_users[email_lower].append(t)

    # Find duplicates: same email, different users
    duplicate_tickets = {}
    for email, tickets_list in email_users.items():
        unique_users = {}
        for t in tickets_list:
            uid = t["telegram_chat_id"]
            if uid not in unique_users:
                unique_users[uid] = t

        if len(unique_users) >= 2:
            for t in tickets_list:
                others = [
                    {
                        "name": u["telegram_name"] or "N/A",
                        "username": u["telegram_username"] or "",
                        "chat_id": u["telegram_chat_id"],
                        "date": format_date(u["created_at"])
                    }
                    for uid, u in unique_users.items()
                    if uid != t["telegram_chat_id"]
                ]
                if others:
                    duplicate_tickets[t["id"]] = others

    cur.close()
    conn.close()
    return render_template("dashboard.html",
                           redefinir=redefinir,
                           tela_caida=tela_caida,
                           completa_caida=completa_caida,
                           total_pendentes=total_pendentes,
                           total_resolvidos=total_resolvidos,
                           total_reprovados=total_reprovados,
                           duplicate_tickets=duplicate_tickets,
                           format_date=format_date)


# ============================================
# HISTORY
# ============================================
@app.route("/historico")
@login_required
def historico():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM tickets WHERE status != 'pendente' ORDER BY resolved_at DESC LIMIT 100"
    )
    cols = [desc[0] for desc in cur.description]
    tickets = [dict(zip(cols, row)) for row in cur.fetchall()]
    for t in tickets:
        t["created_at_display"] = format_date(t["created_at"])
        t["resolved_at_display"] = format_date(t["resolved_at"])
    cur.close()
    conn.close()
    return render_template("historico.html", tickets=tickets, format_date=format_date)


# ============================================
# ACTIONS (resolve tickets)
# ============================================
@app.route("/api/ticket/<int:ticket_id>/trocar-senha", methods=["POST"])
@login_required
def trocar_senha(ticket_id):
    data = request.get_json()
    nova_senha = data.get("nova_senha", "").strip()
    if not nova_senha:
        return jsonify({"error": "Nova senha é obrigatória"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,))
    cols = [desc[0] for desc in cur.description]
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "Ticket não encontrado"}), 404
    ticket = dict(zip(cols, row))

    now = datetime.now()
    cur.execute("""
        UPDATE tickets SET status = 'resolvido', resolved_action = 'trocar_senha',
        resolved_data = %s, resolved_at = %s WHERE id = %s
    """, (json.dumps({"nova_senha": nova_senha}), now, ticket_id))
    conn.commit()

    # Send Telegram message
    msg = (
        f"✅ *Senha Alterada com Sucesso!*\n\n"
        f"📧 Referente ao email: `{ticket['email']}`\n"
        f"🔑 Nova Senha: `{nova_senha}`\n\n"
        f"_Qualquer dúvida, abra um novo suporte!_ ❤️"
    )
    send_telegram_message(ticket["telegram_chat_id"], msg)

    cur.close()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/ticket/<int:ticket_id>/trocar-email", methods=["POST"])
@login_required
def trocar_email(ticket_id):
    data = request.get_json()
    novo_email = data.get("novo_email", "").strip()
    nova_senha = data.get("nova_senha", "").strip()
    if not novo_email:
        return jsonify({"error": "Novo email é obrigatório"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,))
    cols = [desc[0] for desc in cur.description]
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "Ticket não encontrado"}), 404
    ticket = dict(zip(cols, row))

    now = datetime.now()
    cur.execute("""
        UPDATE tickets SET status = 'resolvido', resolved_action = 'trocar_email',
        resolved_data = %s, resolved_at = %s WHERE id = %s
    """, (json.dumps({"novo_email": novo_email, "nova_senha": nova_senha}), now, ticket_id))
    conn.commit()

    senha_texto = f"\n🔑 Nova Senha: `{nova_senha}`" if nova_senha else ""
    msg = (
        f"✅ *Email Trocado com Sucesso!*\n\n"
        f"📧 Email antigo: `{ticket['email']}`\n"
        f"📧 Novo Email: `{novo_email}`{senha_texto}\n\n"
        f"_Qualquer dúvida, abra um novo suporte!_ ❤️"
    )
    send_telegram_message(ticket["telegram_chat_id"], msg)

    cur.close()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/ticket/<int:ticket_id>/resolvido", methods=["POST"])
@login_required
def problema_resolvido(ticket_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,))
    cols = [desc[0] for desc in cur.description]
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "Ticket não encontrado"}), 404
    ticket = dict(zip(cols, row))

    now = datetime.now()
    cur.execute("""
        UPDATE tickets SET status = 'resolvido', resolved_action = 'resolvido',
        resolved_at = %s WHERE id = %s
    """, (now, ticket_id))
    conn.commit()

    msg = (
        f"✅ *Solicitação Resolvida!*\n\n"
        f"📧 Referente ao email: `{ticket['email']}`\n\n"
        f"Sua solicitação foi resolvida, volte a assistir agora mesmo! ❤️\n\n"
        f"_Qualquer dúvida, abra um novo suporte!_"
    )
    send_telegram_message(ticket["telegram_chat_id"], msg)

    cur.close()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/ticket/<int:ticket_id>/reprovar", methods=["POST"])
@login_required
def reprovar(ticket_id):
    data = request.get_json()
    motivo = data.get("motivo", "").strip()
    if not motivo:
        return jsonify({"error": "Motivo é obrigatório"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,))
    cols = [desc[0] for desc in cur.description]
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "Ticket não encontrado"}), 404
    ticket = dict(zip(cols, row))

    now = datetime.now()
    cur.execute("""
        UPDATE tickets SET status = 'reprovado', resolved_action = 'reprovado',
        resolved_data = %s, resolved_at = %s WHERE id = %s
    """, (json.dumps({"motivo": motivo}), now, ticket_id))
    conn.commit()

    msg = (
        f"❌ *Solicitação Reprovada*\n\n"
        f"📧 Referente ao email: `{ticket['email']}`\n"
        f"📝 Motivo: {motivo}\n\n"
        f"_Se discordar, abra um novo suporte!_"
    )
    send_telegram_message(ticket["telegram_chat_id"], msg)

    cur.close()
    conn.close()
    return jsonify({"success": True})


# ============================================
# API for bot to create tickets
# ============================================
@app.route("/api/tickets/pendentes")
@login_required
def tickets_pendentes():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE status = 'pendente' ORDER BY created_at ASC")
    cols = [desc[0] for desc in cur.description]
    pendentes = [dict(zip(cols, row)) for row in cur.fetchall()]

    cur.execute("SELECT COUNT(*) FROM tickets WHERE status = 'resolvido'")
    total_resolvidos = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tickets WHERE status = 'reprovado'")
    total_reprovados = cur.fetchone()[0]
    cur.close()
    conn.close()

    tickets_list = []
    for t in pendentes:
        tickets_list.append({
            "id": t["id"],
            "type": t["type"],
            "email": t["email"],
            "senha": t["senha"],
            "telegram_name": t["telegram_name"],
            "telegram_username": t["telegram_username"],
            "telegram_chat_id": t["telegram_chat_id"],
            "created_at": format_date(t["created_at"])
        })

    return jsonify({
        "tickets": tickets_list,
        "total_pendentes": len(pendentes),
        "total_resolvidos": total_resolvidos,
        "total_reprovados": total_reprovados
    })


@app.route("/api/ticket/<int:ticket_id>/cancelar", methods=["POST"])
def cancelar_ticket(ticket_id):
    """Cancela (deleta) ticket pendente. Chamado pelo bot quando usuario cancela."""
    data = request.get_json() or {}
    chat_id = data.get("chat_id", "")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id = %s AND status = 'pendente'", (ticket_id,))
    cols = [desc[0] for desc in cur.description]
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "Ticket não encontrado ou já resolvido"}), 404
    ticket = dict(zip(cols, row))

    # Só o dono ou admin pode cancelar
    if chat_id and str(ticket["telegram_chat_id"]) != str(chat_id):
        cur.close()
        conn.close()
        return jsonify({"error": "Sem permissão"}), 403

    cur.execute("DELETE FROM tickets WHERE id = %s", (ticket_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/ticket", methods=["POST"])
def create_ticket():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON required"}), 400

    required = ["type", "email", "telegram_chat_id"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"{field} is required"}), 400

    conn = get_db()
    cur = conn.cursor()
    now = datetime.now()
    cur.execute("""
        INSERT INTO tickets (type, email, senha, telegram_chat_id, telegram_username, telegram_name, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
    """, (
        data["type"],
        data["email"],
        data.get("senha"),
        data["telegram_chat_id"],
        data.get("telegram_username"),
        data.get("telegram_name"),
        now
    ))
    ticket_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True, "ticket_id": ticket_id})


# ============================================
# TELEGRAM SEND
# ============================================
def send_telegram_message(chat_id, text):
    """Send message to user via Telegram Bot API."""
    import urllib.request
    import urllib.parse

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM] Erro ao enviar mensagem: {e}")


# ============================================
# CONFIG HELPERS (shared DB between web + worker)
# ============================================
def get_cfg(key):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM config WHERE key = %s", (key,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None

def set_cfg(key, value):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value)
    )
    conn.commit()
    cur.close()
    conn.close()

# ============================================
# API - MAINTENANCE TOGGLE (called by sales bot)
# ============================================
@app.route("/api/chat-ids", methods=["GET"])
def api_chat_ids():
    """Retorna todos os chat_ids únicos para spam."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT telegram_chat_id FROM tickets WHERE telegram_chat_id IS NOT NULL")
    ids = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify({"chat_ids": ids})


@app.route("/api/maintenance", methods=["POST"])
def api_maintenance():
    """Toggle maintenance mode. Called by sales bot."""
    data = request.get_json() or {}
    action = data.get("action", "toggle")
    secret = data.get("secret", "")

    if secret != "lkstore2026":
        return jsonify({"error": "unauthorized"}), 401

    if action == "on":
        set_cfg('maintenance', '1')
        return jsonify({"maintenance": True})
    elif action == "off":
        set_cfg('maintenance', '0')
        return jsonify({"maintenance": False})
    else:
        current = get_cfg('maintenance')
        new_val = '0' if current == '1' else '1'
        set_cfg('maintenance', new_val)
        return jsonify({"maintenance": new_val == '1'})

@app.route("/api/maintenance", methods=["GET"])
def api_maintenance_status():
    """Check maintenance status."""
    return jsonify({"maintenance": get_cfg('maintenance') == '1'})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
