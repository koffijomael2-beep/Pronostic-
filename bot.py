"""
Bot Telegram pour la mini app de pronostics (victoire / nul / victoire).

Variables d'environnement :
  BOT_TOKEN     token donné par BotFather
  APP_URL       URL HTTPS de pronostics.html
  ADMIN_ID      ton identifiant Telegram (pour saisir les résultats)
  DATABASE_URL  (hébergement) adresse de la base Postgres ; sans elle, un fichier SQLite local est utilisé
  WEBHOOK_URL   (facultatif) adresse publique du bot ; sur Render, RENDER_EXTERNAL_URL est utilisée automatiquement

Commandes :
  /start                 ouvre la mini app (bouton du clavier)
  /mespronos             affiche tes pronostics
  /classement            affiche le classement
  /resultat 1 2-1        (admin) enregistre le résultat du match 1 (score, ou 1 / N / 2)
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

TOKEN = os.environ["BOT_TOKEN"]
APP_URL = os.environ["APP_URL"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_FILE = "pronostics.db"

if DATABASE_URL:
    import psycopg
    from psycopg.rows import dict_row

# Mêmes identifiants et équipes que la liste MATCHES de pronostics.html.
# Optionnel : ajoute "kickoff" (ISO 8601) pour refuser les pronos après le coup d'envoi.
MATCHES = {
    1: {"home": "Paris", "away": "Marseille"},
    2: {"home": "Lyon", "away": "Monaco"},
    3: {"home": "Lille", "away": "Nice"},
    4: {"home": "Real Madrid", "away": "Barcelone"},
    5: {"home": "Arsenal", "away": "Liverpool"},
}
VALID = ("1", "N", "2")  # 1 = victoire domicile, N = nul, 2 = victoire extérieur

logging.basicConfig(level=logging.INFO)


# ---------- Base de données (Postgres si DATABASE_URL, sinon SQLite) ----------
def _run(sql, params, fetch):
    if DATABASE_URL:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cur = conn.execute(sql.replace("?", "%s"), params)
            return cur.fetchall() if fetch else None
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()] if fetch else None
    finally:
        conn.close()


def execute(sql, params=()):
    _run(sql, params, fetch=False)


def query(sql, params=()):
    return _run(sql, params, fetch=True)


def init_db():
    execute(
        "CREATE TABLE IF NOT EXISTS picks_1n2("
        "user_id BIGINT, name TEXT, match_id INTEGER, pick TEXT, "
        "PRIMARY KEY (user_id, match_id))"
    )
    execute("CREATE TABLE IF NOT EXISTS results_1n2(match_id INTEGER PRIMARY KEY, outcome TEXT)")


# ---------- Outils ----------
def label(match_id, pick):
    m = MATCHES[match_id]
    if pick == "1":
        return f"victoire {m['home']}"
    if pick == "2":
        return f"victoire {m['away']}"
    return "match nul"


def title(match_id):
    m = MATCHES[match_id]
    return f"{m['home']} - {m['away']}"


def parse_result(text):
    """Accepte '1', 'N', '2' ou un score comme '2-1'."""
    text = text.strip().upper()
    if text in VALID:
        return text
    h, a = (int(x) for x in text.split("-"))
    return "1" if h > a else "2" if h < a else "N"


def is_closed(match_id):
    """Un match est fermé s'il a un résultat ou si son coup d'envoi est passé."""
    if query("SELECT 1 FROM results_1n2 WHERE match_id=?", (match_id,)):
        return True
    kickoff = MATCHES[match_id].get("kickoff")
    if kickoff:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(kickoff)
    return False


# ---------- Commandes ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # sendData() ne fonctionne que si la mini app est ouverte depuis un bouton de clavier.
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("Faire mes pronostics", web_app=WebAppInfo(url=APP_URL))]],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "Bienvenue ! Touche le bouton pour pronostiquer chaque match : "
        "victoire d'une équipe ou match nul.\nBon pronostic : 1 point.",
        reply_markup=keyboard,
    )


async def receive_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        picks = json.loads(update.effective_message.web_app_data.data)["picks"]
    except (ValueError, KeyError, TypeError):
        await update.message.reply_text("Je n'ai pas pu lire tes pronostics. Réessaie depuis l'app.")
        return

    saved, refused = [], []
    for p in picks:
        try:
            mid, pick = int(p["match"]), str(p["pick"]).upper()
        except (ValueError, KeyError, TypeError):
            continue
        if mid not in MATCHES or pick not in VALID:
            continue
        if is_closed(mid):
            refused.append(title(mid))
            continue
        execute(
            "INSERT INTO picks_1n2(user_id, name, match_id, pick) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, match_id) DO UPDATE SET pick=excluded.pick, name=excluded.name",
            (user.id, user.full_name, mid, pick),
        )
        saved.append(f"{title(mid)} : {label(mid, pick)}")

    text = f"{len(saved)} pronostic(s) enregistré(s) :\n" + "\n".join(saved) if saved else "Aucun pronostic enregistré."
    if refused:
        text += "\n\nDéjà commencés (non modifiables) :\n" + "\n".join(refused)
    await update.message.reply_text(text)


async def mes_pronos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = query(
        "SELECT p.match_id, p.pick, r.outcome FROM picks_1n2 p "
        "LEFT JOIN results_1n2 r ON r.match_id = p.match_id "
        "WHERE p.user_id=? ORDER BY p.match_id",
        (update.effective_user.id,),
    )
    if not rows:
        await update.message.reply_text("Tu n'as pas encore de pronostic. Envoie /start pour commencer.")
        return
    lines = []
    for r in rows:
        line = f"{title(r['match_id'])} : {label(r['match_id'], r['pick'])}"
        if r["outcome"]:
            line += " (gagné, 1 pt)" if r["outcome"] == r["pick"] else " (perdu, 0 pt)"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))


async def classement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = query(
        "SELECT p.user_id, MAX(p.name) AS name, "
        "SUM(CASE WHEN p.pick = r.outcome THEN 1 ELSE 0 END) AS pts "
        "FROM picks_1n2 p JOIN results_1n2 r ON r.match_id = p.match_id "
        "GROUP BY p.user_id ORDER BY pts DESC, name LIMIT 20"
    )
    if not rows:
        await update.message.reply_text("Le classement apparaîtra dès le premier résultat.")
        return
    lines = [f"{i}. {r['name']} : {r['pts']} pt" for i, r in enumerate(rows, 1)]
    await update.message.reply_text("Classement\n" + "\n".join(lines))


async def resultat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Commande réservée à l'administrateur.")
        return
    try:
        mid = int(context.args[0])
        outcome = parse_result(context.args[1])
        assert mid in MATCHES
    except (IndexError, ValueError, AssertionError):
        await update.message.reply_text("Utilisation : /resultat 1 2-1   (ou /resultat 1 N)")
        return
    execute(
        "INSERT INTO results_1n2(match_id, outcome) VALUES (?,?) "
        "ON CONFLICT(match_id) DO UPDATE SET outcome=excluded.outcome",
        (mid, outcome),
    )
    await update.message.reply_text(
        f"Résultat enregistré : {title(mid)}, {label(mid, outcome)}. Envoie /classement pour voir les points."
    )


def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mespronos", mes_pronos))
    app.add_handler(CommandHandler("classement", classement))
    app.add_handler(CommandHandler("resultat", resultat))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, receive_picks))

    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if base_url:
        # Hébergement : Telegram appelle le bot (le service peut dormir entre deux messages).
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")),
            url_path=TOKEN,
            webhook_url=f"{base_url.rstrip('/')}/{TOKEN}",
        )
    else:
        # En local : le bot interroge Telegram.
        app.run_polling()


if __name__ == "__main__":
    main()
