import os, json, subprocess, threading, zipfile, shutil, time
from pathlib import Path
from datetime import datetime

from flask import Flask, request
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
import psutil

# ======================== ENV / CONFIG ========================
BOT_TOKEN = os.getenv("8492782828:AAHbPAvruc-j9_FLiksOM3QUBFuPVLH-waA", "")  # set on your host
ADMIN_ID = int(os.getenv("7394704068", "0"))  # your Telegram user id
MONGO_URI = os.getenv("mongodb+srv://BOTFORHOSTING:jAHt1ywD6M9XoWcz@cluster0.rs82s3q.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0", "")
WEBHOOK = os.getenv("WEBHOOK", "false").lower() == "true"  # true -> webhook mode
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Filesystem paths (must be WRITEABLE on your host)
BASE_DIR = Path(__file__).parent.resolve()
BOTS_DIR = BASE_DIR / "bots"
LOGS_DIR = BASE_DIR / "logs"
BOTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ======================== TELEGRAM BOT ========================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)

# ======================== FLASK (keepalive + webhook) =========
app = Flask(__name__)

@app.get("/")
def index():
    return "✅ Hosting Bot (MongoDB) alive"

@app.post(f"/{BOT_TOKEN}")
def webhook_route():
    # Webhook endpoint (for WEBHOOK mode)
    try:
        payload = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(payload)
        bot.process_new_updates([update])
    except Exception as e:
        print("Webhook error:", e)
    return "OK", 200

def run_keepalive():
    # Run Flask in a background thread (for Replit/Railway keepalive pings)
    app.run(host="0.0.0.0", port=PORT)

if not WEBHOOK:
    threading.Thread(target=run_keepalive, daemon=True).start()

# ======================== MONGO DB ============================
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is not set")

client = MongoClient(MONGO_URI)
db = client["hostingbot"]
col_users = db["users"]        # {id, slots, banned}
col_process = db["processes"]  # {uid, file, pid, started_at}
col_state = db["states"]       # ephemeral (e.g. last_file), {uid, key, value}

# ====================== IN-MEMORY PROCESS HANDLES ============
# key: absolute file path -> {"p": Popen, "log": file-handle}
processes = {}

# pending requirements upload: user_id -> target_workdir path (str)
pending_reqs = {}

# ====================== HELPERS ==============================
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def get_user(uid: int):
    u = col_users.find_one({"id": uid})
    if not u:
        u = {"id": uid, "slots": 3, "banned": False}
        col_users.insert_one(u)
    return u

def set_user(uid: int, **fields):
    col_users.update_one({"id": uid}, {"$set": fields}, upsert=True)

def is_banned(uid: int) -> bool:
    u = get_user(uid)
    return bool(u.get("banned", False))

def user_folder(uid: int) -> Path:
    p = BOTS_DIR / str(uid)
    p.mkdir(parents=True, exist_ok=True)
    return p

def ensure_logfile(path: Path) -> Path:
    # one log file per script file
    name = f"{path.name}.log"
    log_path = LOGS_DIR / str(path.parent.name) / name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.touch()
    return log_path

def last_lines(path: Path, n=500) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception as e:
        return f"(log read error) {e}"

def chunk_and_send(chat_id: int, text: str, chunk_size=3500):
    if not text:
        bot.send_message(chat_id, "(empty log)")
        return
    start = 0
    while start < len(text):
        part = text[start:start+chunk_size]
        bot.send_message(chat_id, f"<pre>{telebot.util.escape(part)}</pre>")
        start += chunk_size

def list_user_files(uid: int):
    folder = user_folder(uid)
    files = sorted([p for p in folder.iterdir() if p.is_file() and not p.name.endswith(".log")])
    return files

def analyze_upload(saved_path: Path):
    """
    Returns: (kind, run_cmd:list, workdir:Path, main_path:Path)
    kind in {"py","js","zip"}
    """
    lower = saved_path.name.lower()
    if lower.endswith(".py"):
        return "py", ["python3", saved_path.name], saved_path.parent, saved_path
    if lower.endswith(".js"):
        return "js", ["node", saved_path.name], saved_path.parent, saved_path
    if lower.endswith(".zip"):
        # extract zip to folder <stem>_proj
        extract_root = saved_path.parent / (saved_path.stem + "_proj")
        if extract_root.exists():
            shutil.rmtree(extract_root, ignore_errors=True)
        extract_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(saved_path, "r") as z:
            z.extractall(extract_root)
        # find main.py
        main_path = None
        for root, dirs, files in os.walk(extract_root):
            if "main.py" in files:
                main_path = Path(root) / "main.py"
                break
        if main_path is None:
            raise RuntimeError("Zip extracted but main.py not found.")
        return "zip", ["python3", "main.py"], main_path.parent, main_path
    raise RuntimeError("Unsupported file. Only .py, .js, .zip")

def start_process(uid: int, fpath: Path):
    key = str(fpath.resolve())
    if key in processes:
        return "Already running."
    kind, run_cmd, workdir, main_path = analyze_upload(fpath)

    # optional: ensure Node exists for .js
    if run_cmd[0] == "node":
        try:
            subprocess.run(["node", "-v"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except Exception:
            return "Node.js not available on this host."

    # open log file
    log_file = ensure_logfile(fpath)
    lf = open(log_file, "a", buffering=1, encoding="utf-8", errors="ignore")

    # spawn
    p = subprocess.Popen(run_cmd, cwd=str(workdir), stdout=lf, stderr=lf)
    processes[key] = {"p": p, "log": lf}

    # record in DB
    col_process.update_one(
        {"uid": uid, "file": fpath.name},
        {"$set": {"pid": p.pid, "started_at": datetime.utcnow(), "abs": key}},
        upsert=True
    )
    return f"Started ✅ (PID {p.pid})"

def stop_process(uid: int, fpath: Path):
    key = str(fpath.resolve())
    info = processes.get(key)
    if not info:
        # maybe it was started in a previous run, try pid from DB
        doc = col_process.find_one({"uid": uid, "file": fpath.name})
        if not doc:
            return "Not running."
        try:
            pid = int(doc.get("pid", 0))
            if pid and psutil.pid_exists(pid):
                psutil.Process(pid).terminate()
        except Exception:
            pass
        col_process.delete_one({"uid": uid, "file": fpath.name})
        return "Stopped ⏹"
    try:
        p = info["p"]
        p.terminate()
        try:
            p.wait(timeout=6)
        except Exception:
            p.kill()
    finally:
        try:
            info["log"].close()
        except Exception:
            pass
        processes.pop(key, None)
        col_process.delete_one({"uid": uid, "file": fpath.name})
    return "Stopped ⏹"

def is_running(uid: int, fpath: Path) -> bool:
    # prefer in-memory handle
    key = str(fpath.resolve())
    if key in processes:
        p = processes[key]["p"]
        return (p.poll() is None)
    # fallback DB pid
    doc = col_process.find_one({"uid": uid, "file": fpath.name})
    if not doc:
        return False
    pid = int(doc.get("pid", 0))
    return pid and psutil.pid_exists(pid)

def pip_install_requirements(req_path: Path) -> (bool, str):
    try:
        proc = subprocess.run(["pip", "install", "-r", str(req_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return proc.returncode == 0, proc.stdout
    except Exception as e:
        return False, str(e)

def run_shell(cmd: str, workdir: Path) -> (int, str):
    try:
        proc = subprocess.run(cmd, cwd=str(workdir), shell=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return proc.returncode, proc.stdout
    except Exception as e:
        return 1, str(e)

def set_last_file(uid: int, fname: str):
    col_state.update_one({"uid": uid, "key": "last_file"}, {"$set": {"value": fname}}, upsert=True)

def get_last_file(uid: int) -> str | None:
    doc = col_state.find_one({"uid": uid, "key": "last_file"})
    return doc.get("value") if doc else None

# ====================== UI BUILDERS ==========================
def kb_menu(uid: int):
    u = get_user(uid)
    used = col_process.count_documents({"uid": uid})
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("📂 Upload File", callback_data="upload_info"))
    kb.row(InlineKeyboardButton("📝 Check Files", callback_data="myfiles"))
    kb.row(InlineKeyboardButton("ℹ️ Help", callback_data="help"))
    return kb, used, u["slots"]

def kb_file_actions(uid: int, fname: str, back_to="myfiles"):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("▶️ Start", callback_data=f"start|{uid}|{fname}"),
        InlineKeyboardButton("🔄 Restart", callback_data=f"restart|{uid}|{fname}")
    )
    kb.row(
        InlineKeyboardButton("⏹ Stop", callback_data=f"stop|{uid}|{fname}"),
        InlineKeyboardButton("❌ Delete", callback_data=f"delete|{uid}|{fname}")
    )
    kb.row(
        InlineKeyboardButton("📜 Logs", callback_data=f"logs|{uid}|{fname}"),
        InlineKeyboardButton("⚙️ Custom Command", callback_data=f"cmd|{uid}|{fname}")
    )
    kb.row(InlineKeyboardButton("🔙 Back", callback_data=back_to))
    kb.row(InlineKeyboardButton("🏠 Back to Menu", callback_data="menu"))
    return kb

def kb_custom_cmd(uid: int, fname: str):
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("💻 Run Command", callback_data=f"run|{uid}|{fname}"))
    kb.row(InlineKeyboardButton("📦 Install Module (pip)", callback_data=f"pip|{uid}|{fname}"),
           InlineKeyboardButton("📦 Install Module (npm)", callback_data=f"npm|{uid}|{fname}"))
    kb.row(InlineKeyboardButton("📄 Install from requirements.txt", callback_data=f"reqrun|{uid}|{fname}"))
    kb.row(InlineKeyboardButton("🔙 Back", callback_data=f"file|{uid}|{fname}"))
    kb.row(InlineKeyboardButton("🏠 Back to Menu", callback_data="menu"))
    return kb

# ====================== COMMANDS =============================

@bot.message_handler(commands=["start"])
def cmd_start(m):
    if is_banned(m.from_user.id):
        return
    u = get_user(m.from_user.id)
    kb, used, slots = kb_menu(m.from_user.id)
    bot.reply_to(m, (
        "👋 <b>Welcome to Hosting Bot!</b>\n\n"
        "Upload <b>.py</b>, <b>.js</b> or a <b>.zip</b> with <code>main.py</code> to host.\n"
        "After upload, you can send <code>requirements.txt</code> (or skip).\n\n"
        f"🎯 Slots used: <b>{used}/{slots}</b>\n"
        f"👤 Your ID: <code>{m.from_user.id}</code>"
    ), reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "menu")
def cb_menu(call):
    if is_banned(call.from_user.id):
        return
    kb, used, slots = kb_menu(call.from_user.id)
    bot.edit_message_text(f"🏠 Main Menu\n\nSlots used: <b>{used}/{slots}</b>", call.message.chat.id, call.message.message_id, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "help")
def cb_help(call):
    txt = (
        "ℹ️ <b>How to use</b>\n\n"
        "• Upload <code>.py</code> or <code>.js</code> to host a single file.\n"
        "• Upload <code>.zip</code> containing <code>main.py</code> for projects.\n"
        "• After upload, send <code>requirements.txt</code> (or tap Skip).\n"
        "• Manage: Start / Stop / Restart / Logs / Delete / Custom Command.\n"
        "• Logs show last 500 lines in copy-paste format.\n\n"
        "🔒 Admin uses only commands (no admin inline buttons)."
    )
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("🔙 Back to Menu", callback_data="menu"))
    bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "upload_info")
def cb_upload_info(call):
    bot.edit_message_text("📤 Send your file now (.py / .js / .zip).", call.message.chat.id, call.message.message_id,
                          reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")))

@bot.callback_query_handler(func=lambda c: c.data == "myfiles")
def cb_mybots(call):
    uid = call.from_user.id
    files = list_user_files(uid)
    kb = InlineKeyboardMarkup()
    msg = "📝 <b>Your Files</b>\n\n"
    if not files:
        kb.row(InlineKeyboardButton("🔙 Back to Menu", callback_data="menu"))
        bot.edit_message_text("❌ No files uploaded yet.", call.message.chat.id, call.message.message_id, reply_markup=kb)
        return
    for p in files:
        status = "🟢" if is_running(uid, p) else "🔴"
        kb.row(InlineKeyboardButton(f"{status} {p.name}", callback_data=f"file|{uid}|{p.name}"))
    kb.row(InlineKeyboardButton("🔙 Back to Menu", callback_data="menu"))
    bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("file|"))
def cb_file(call):
    _, uid, fname = call.data.split("|", 2)
    uid = int(uid)
    fpath = user_folder(uid) / fname
    if not fpath.exists():
        bot.answer_callback_query(call.id, "File missing")
        return
    status = "Running 🟢" if is_running(uid, fpath) else "Stopped 🔴"
    bot.edit_message_text(f"🧩 <b>{fname}</b> — {status}", call.message.chat.id, call.message.message_id,
                          reply_markup=kb_file_actions(uid, fname))

@bot.callback_query_handler(func=lambda c: any(c.data.startswith(x) for x in ["start|","stop|","restart|","logs|","delete|","cmd|","run|","pip|","npm|","reqrun|"]))
def cb_actions(call):
    parts = call.data.split("|")
    action = parts[0]
    uid = int(parts[1])
    fname = parts[2]
    fpath = user_folder(uid) / fname

    # guards
    if is_banned(uid):
        bot.answer_callback_query(call.id, "You are banned.")
        return
    u = get_user(uid)

    # slots check for starting/restarting
    def slots_available() -> bool:
        running_count = col_process.count_documents({"uid": uid})
        return running_count < int(u.get("slots", 3))

    if action == "start":
        if not fpath.exists():
            bot.answer_callback_query(call.id, "File missing"); return
        if not slots_available():
            bot.answer_callback_query(call.id, "Slot limit reached."); return
        msg = start_process(uid, fpath)
        bot.answer_callback_query(call.id, msg, show_alert=True)

    elif action == "stop":
        if not fpath.exists():
            bot.answer_callback_query(call.id, "File missing"); return
        msg = stop_process(uid, fpath)
        bot.answer_callback_query(call.id, msg, show_alert=True)

    elif action == "restart":
        if not fpath.exists():
            bot.answer_callback_query(call.id, "File missing"); return
        stop_process(uid, fpath)
        if not slots_available():
            bot.answer_callback_query(call.id, "Slot limit reached."); return
        msg = start_process(uid, fpath)
        bot.answer_callback_query(call.id, msg, show_alert=True)

    elif action == "logs":
        log_file = ensure_logfile(fpath)
        txt = last_lines(log_file, 500)
        bot.answer_callback_query(call.id, "Logs")
        bot.send_message(call.message.chat.id, f"📜 Logs for <b>{fname}</b> (last 500 lines):")
        chunk_and_send(call.message.chat.id, txt)
        bot.send_message(call.message.chat.id, "Controls:", reply_markup=kb_file_actions(uid, fname))

    elif action == "delete":
        stop_process(uid, fpath)
        try:
            # if it was a zip, remove extracted directory too
            if fpath.suffix.lower() == ".zip":
                extract_root = fpath.parent / (fpath.stem + "_proj")
                if extract_root.exists():
                    shutil.rmtree(extract_root, ignore_errors=True)
            if fpath.exists():
                fpath.unlink()
            # remove log file
            lf = ensure_logfile(fpath)
            if lf.exists():
                lf.unlink()
            bot.answer_callback_query(call.id, "Deleted ❌", show_alert=True)
        except Exception as e:
            bot.answer_callback_query(call.id, f"Delete failed: {e}", show_alert=True)

    elif action == "cmd":
        bot.edit_message_text(f"⚙️ <b>Custom Command for</b> {fname}", call.message.chat.id, call.message.message_id,
                              reply_markup=kb_custom_cmd(uid, fname))

    elif action == "run":
        # ask for a shell command
        bot.answer_callback_query(call.id, "Send a shell command (runs in this file's folder).", show_alert=True)
        msg = bot.send_message(call.message.chat.id, "💻 Send command (e.g. <code>pip install requests</code> or <code>python main.py</code>)")
        bot.register_next_step_handler(msg, lambda m: handle_shell_command(m, uid, fname))

    elif action == "pip":
        bot.answer_callback_query(call.id, "Send a Python module name (pip install).", show_alert=True)
        msg = bot.send_message(call.message.chat.id, "📦 Module name for <b>pip</b> (e.g. <code>requests</code>):")
        bot.register_next_step_handler(msg, lambda m: handle_module_install(m, uid, fname, "pip"))

    elif action == "npm":
        bot.answer_callback_query(call.id, "Send a JS package name (npm install).", show_alert=True)
        msg = bot.send_message(call.message.chat.id, "📦 Package name for <b>npm</b> (e.g. <code>node-telegram-bot-api</code>):")
        bot.register_next_step_handler(msg, lambda m: handle_module_install(m, uid, fname, "npm"))

    elif action == "reqrun":
        # try to locate requirements.txt in same folder (or extracted zip folder)
        try:
            kind, _, workdir, _ = analyze_upload(fpath)
            req = (workdir / "requirements.txt")
            if not req.exists():
                bot.answer_callback_query(call.id, "requirements.txt not found.", show_alert=True)
                return
            ok, out = pip_install_requirements(req)
            if ok:
                bot.answer_callback_query(call.id, "requirements installed ✅", show_alert=True)
            else:
                bot.answer_callback_query(call.id, "Install failed. Sending output...", show_alert=True)
            chunk_and_send(call.message.chat.id, out)
        except Exception as e:
            bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)

# Shell command handlers
def handle_shell_command(message, uid: int, fname: str):
    if is_banned(uid): return
    fpath = user_folder(uid) / fname
    if not fpath.exists():
        bot.reply_to(message, "File missing.")
        return
    # command runs inside the file's working directory
    try:
        _, _, workdir, _ = analyze_upload(fpath)
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")
        return
    code, out = run_shell(message.text, workdir)
    bot.reply_to(message, f"Exit {code}\n<pre>{telebot.util.escape(out)}</pre>")

def handle_module_install(message, uid: int, fname: str, tool: str):
    if is_banned(uid): return
    pkg = (message.text or "").strip()
    if not pkg:
        bot.reply_to(message, "No package provided.")
        return
    fpath = user_folder(uid) / fname
    try:
        _, _, workdir, _ = analyze_upload(fpath)
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")
        return
    if tool == "pip":
        cmd = f"pip install {pkg}"
    else:
        cmd = f"npm install {pkg}"
    code, out = run_shell(cmd, workdir)
    bot.reply_to(message, f"{tool} exit {code}\n<pre>{telebot.util.escape(out)}</pre>")

# =============== UPLOAD FLOW & REQUIREMENTS ASK ============
@bot.message_handler(content_types=["document"])
def on_document(message):
    uid = message.from_user.id
    if is_banned(uid): return
    get_user(uid)  # ensure exists

    # requirements upload path?
    if uid in pending_reqs:
        target_dir = Path(pending_reqs.pop(uid))
        file_info = bot.get_file(message.document.file_id)
        file_bytes = bot.download_file(file_info.file_path)
        req_path = target_dir / "requirements.txt"
        with open(req_path, "wb") as f:
            f.write(file_bytes)
        ok, out = pip_install_requirements(req_path)
        if ok:
            bot.reply_to(message, "📦 requirements.txt installed ✅", reply_markup=kb_file_actions(uid, get_last_file(uid) or "", back_to="myfiles"))
        else:
            bot.reply_to(message, "⚠️ Install failed. Output below:")
            chunk_and_send(message.chat.id, out)
        return

    # normal upload
    folder = user_folder(uid)
    file_info = bot.get_file(message.document.file_id)
    file_bytes = bot.download_file(file_info.file_path)
    safe_name = message.document.file_name
    target = folder / safe_name
    with open(target, "wb") as f:
        f.write(file_bytes)
    set_last_file(uid, target.name)

    # analyze + ask for requirements (or auto for zip)
    try:
        kind, run_cmd, workdir, main_path = analyze_upload(target)
        if kind == "zip":
            rp = workdir / "requirements.txt"
            if rp.exists():
                ok, out = pip_install_requirements(rp)
                if ok:
                    bot.reply_to(message, f"✅ Zip extracted & requirements installed.\nReady to manage <b>{target.name}</b>.", reply_markup=kb_file_actions(uid, target.name))
                else:
                    bot.reply_to(message, f"✅ Zip extracted but requirements install failed. You may upload a fixed requirements.txt or run custom commands.",
                                 reply_markup=kb_file_actions(uid, target.name))
                return
        # non-zip or no reqs inside zip → offer upload/skip
        kb = InlineKeyboardMarkup()
        kb.row(InlineKeyboardButton("📤 Upload requirements.txt", callback_data=f"req|{uid}|{target.name}"))
        kb.row(InlineKeyboardButton("⏭️ Skip", callback_data=f"skipreq|{uid}|{target.name}"))
        kb.row(InlineKeyboardButton("🏠 Back to Menu", callback_data="menu"))
        bot.reply_to(message, f"✅ File saved: <code>{target.name}</code>\nIf your project needs dependencies, upload requirements.txt now or Skip.",
                     reply_markup=kb)
    except Exception as e:
        bot.reply_to(message, f"⚠️ {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("req|") or c.data.startswith("skipreq|"))
def cb_requirements(call):
    action, uid, fname = call.data.split("|", 2)
    uid = int(uid)
    fpath = user_folder(uid) / fname
    if not fpath.exists():
        bot.answer_callback_query(call.id, "File missing")
        return
    if action == "req":
        # determine working dir (zip -> extracted folder, else file folder)
        try:
            _, _, workdir, _ = analyze_upload(fpath)
        except Exception as e:
            bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)
            return
        pending_reqs[uid] = str(workdir)
        bot.answer_callback_query(call.id, "Send requirements.txt now")
        bot.send_message(call.message.chat.id, "📤 Please upload <code>requirements.txt</code> for this project.")
    else:
        bot.answer_callback_query(call.id, "Skipped")
        bot.edit_message_text("Choose an action:", call.message.chat.id, call.message.message_id,
                              reply_markup=kb_file_actions(uid, fname))

# ===================== ADMIN (COMMANDS ONLY) =================
@bot.message_handler(commands=["admin"])
def admin_help(m):
    if not is_admin(m.from_user.id): return
    bot.reply_to(m,
        "👑 <b>Admin Panel (commands)</b>\n"
        "• /broadcast &lt;msg&gt;\n"
        "• /addslot &lt;user_id&gt; &lt;n&gt;\n"
        "• /removeslot &lt;user_id&gt; &lt;n&gt;\n"
        "• /users\n"
        "• /ban &lt;user_id&gt;\n"
        "• /unban &lt;user_id&gt;\n"
        "• /stats"
    )

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(m):
    if not is_admin(m.from_user.id): return
    text = m.text.replace("/broadcast", "", 1).strip()
    if not text:
        bot.reply_to(m, "Usage: /broadcast message"); return
    sent = 0
    for u in col_users.find({}):
        try:
            bot.send_message(int(u["id"]), f"📢 {text}")
            sent += 1
        except Exception:
            pass
    bot.reply_to(m, f"✅ Broadcast sent to {sent} users")

@bot.message_handler(commands=["addslot"])
def cmd_addslot(m):
    if not is_admin(m.from_user.id): return
    try:
        _, uid, n = m.text.split()
        uid, n = int(uid), int(n)
        u = get_user(uid)
        set_user(uid, slots=max(0, n))
        bot.reply_to(m, f"✅ Slots for {uid} set to {n}")
    except Exception:
        bot.reply_to(m, "Usage: /addslot user_id number")

@bot.message_handler(commands=["removeslot"])
def cmd_removeslot(m):
    if not is_admin(m.from_user.id): return
    try:
        _, uid, n = m.text.split()
        uid, n = int(uid), int(n)
        u = get_user(uid)
        newv = max(0, int(u.get("slots", 3)) - n)
        set_user(uid, slots=newv)
        bot.reply_to(m, f"✅ Slots for {uid} now {newv}")
    except Exception:
        bot.reply_to(m, "Usage: /removeslot user_id number")

@bot.message_handler(commands=["users"])
def cmd_users_list(m):
    if not is_admin(m.from_user.id): return
    total = col_users.count_documents({})
    ids = [str(u["id"]) for u in col_users.find({}, {"id": 1}).limit(50)]
    preview = "\n".join(ids)
    more = "" if total <= 50 else f"\n... and {total-50} more"
    bot.reply_to(m, f"👥 Total users: {total}\n{preview}{more}")

@bot.message_handler(commands=["ban"])
def cmd_ban(m):
    if not is_admin(m.from_user.id): return
    try:
        _, uid = m.text.split()
        uid = int(uid)
        set_user(uid, banned=True)
        bot.reply_to(m, f"✅ Banned {uid}")
    except Exception:
        bot.reply_to(m, "Usage: /ban user_id")

@bot.message_handler(commands=["unban"])
def cmd_unban(m):
    if not is_admin(m.from_user.id): return
    try:
        _, uid = m.text.split()
        uid = int(uid)
        set_user(uid, banned=False)
        bot.reply_to(m, f"✅ Unbanned {uid}")
    except Exception:
        bot.reply_to(m, "Usage: /unban user_id")

@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not is_admin(m.from_user.id): return
    cpu = psutil.cpu_percent(interval=0.6)
    mem = psutil.virtual_memory()
    running = col_process.count_documents({})
    bot.reply_to(m, (
        f"📊 System Stats\nCPU: {cpu}%\n"
        f"RAM: {mem.used//(1024*1024)} MB / {mem.total//(1024*1024)} MB\n"
        f"Running Bots: {running}\n"
        f"Total Users: {col_users.count_documents({})}"
    ))

# =============== Register everyone else to create user =======
@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'video', 'audio', 'voice', 'sticker'])
def on_any(m):
    if is_banned(m.from_user.id): return
    get_user(m.from_user.id)  # ensure exists

# ====================== RUN (WEBHOOK / POLLING) ==============
if WEBHOOK:
    # In webhook mode, your platform must route POSTs to /<BOT_TOKEN>
    # No polling loop here.
    pass
else:
    print("Hosting Bot is running (polling mode)...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
