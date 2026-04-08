"""
Run both Flask app and Telegram bot together (for local development or single-process hosting).
"""
import threading
import os
from dotenv import load_dotenv

load_dotenv()

def run_flask():
    from app import app
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

def run_bot():
    import time
    time.sleep(2)  # Wait for Flask to start
    from bot import main
    main()

if __name__ == "__main__":
    print("🚀 Iniciando Trocas Dolk...")
    print("📡 Flask API + Painel Web")
    print("🤖 Telegram Bot de Suporte")
    print()
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Bot runs in main thread (telegram library needs it)
    run_bot()
