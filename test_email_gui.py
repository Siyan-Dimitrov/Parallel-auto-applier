"""Quick GUI to test Gmail IMAP email integration."""
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import yaml
from pathlib import Path


def load_email_config():
    cfg_path = Path("config/config.yaml")
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        email = raw.get("email", {})
        return email.get("email", ""), email.get("app_password", "")
    return "", ""


def test_connection(email, password, status_var, log_text):
    """Test IMAP connection in a background thread."""
    status_var.set("Connecting...")
    log_text.delete("1.0", tk.END)
    log_text.insert(tk.END, f"Connecting to imap.gmail.com as {email}...\n")

    try:
        from imap_tools import MailBox, AND
        import datetime

        with MailBox("imap.gmail.com").login(email, password, initial_folder=None) as mb:
            mb.folder.set("INBOX", readonly=True)
            log_text.insert(tk.END, "Connected (read-only).\n\n")

            since = datetime.date.today()
            msgs = list(mb.fetch(AND(date_gte=since), limit=5))
            log_text.insert(tk.END, f"Today's emails found: {len(msgs)}\n\n")

            for i, msg in enumerate(msgs, 1):
                log_text.insert(tk.END, f"--- Email {i} ---\n")
                log_text.insert(tk.END, f"  From: {msg.from_}\n")
                log_text.insert(tk.END, f"  Subject: {msg.subject}\n")
                log_text.insert(tk.END, f"  Date: {msg.date}\n\n")

        status_var.set("Connection OK")
        log_text.insert(tk.END, "Connection test passed.\n")

    except Exception as e:
        status_var.set("Failed")
        log_text.insert(tk.END, f"ERROR: {e}\n")


def test_verification(email, password, status_var, log_text):
    """Test verification code extraction in a background thread."""
    status_var.set("Polling for verification email...")
    log_text.delete("1.0", tk.END)
    log_text.insert(tk.END, "Looking for verification emails (last 5 min)...\n\n")

    try:
        from src.utils.email_reader import EmailReader

        reader = EmailReader("imap.gmail.com", email, password)
        result = reader._check_for_verification(since_minutes=5)

        if result:
            log_text.insert(tk.END, f"Found verification!\n")
            log_text.insert(tk.END, f"  Type: {result['type']}\n")
            log_text.insert(tk.END, f"  Value: {result['value']}\n")
            status_var.set(f"Found: {result['type']}")
        else:
            log_text.insert(tk.END, "No verification emails found in last 5 minutes.\n")
            log_text.insert(tk.END, "(This is expected if no site just sent you one.)\n")
            status_var.set("No verification found")

    except Exception as e:
        status_var.set("Failed")
        log_text.insert(tk.END, f"ERROR: {e}\n")


def main():
    root = tk.Tk()
    root.title("Email Integration Test")
    root.geometry("550x480")

    saved_email, saved_pw = load_email_config()

    # Credentials
    cred_frame = ttk.LabelFrame(root, text="Gmail IMAP Credentials", padding=10)
    cred_frame.pack(fill="x", padx=10, pady=5)

    ttk.Label(cred_frame, text="Email:").grid(row=0, column=0, sticky="w")
    email_var = tk.StringVar(value=saved_email)
    ttk.Entry(cred_frame, textvariable=email_var, width=40).grid(row=0, column=1, padx=5)

    ttk.Label(cred_frame, text="App Password:").grid(row=1, column=0, sticky="w")
    pw_var = tk.StringVar(value=saved_pw)
    ttk.Entry(cred_frame, textvariable=pw_var, width=40, show="*").grid(row=1, column=1, padx=5)

    # Buttons
    btn_frame = ttk.Frame(root, padding=5)
    btn_frame.pack(fill="x", padx=10)

    status_var = tk.StringVar(value="Ready")

    def run_connection_test():
        threading.Thread(
            target=test_connection,
            args=(email_var.get(), pw_var.get(), status_var, log_text),
            daemon=True,
        ).start()

    def run_verification_test():
        threading.Thread(
            target=test_verification,
            args=(email_var.get(), pw_var.get(), status_var, log_text),
            daemon=True,
        ).start()

    ttk.Button(btn_frame, text="Test Connection", command=run_connection_test).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Test Verification Extract", command=run_verification_test).pack(side="left", padx=5)

    # Status
    ttk.Label(btn_frame, textvariable=status_var).pack(side="right", padx=5)

    # Log
    log_frame = ttk.LabelFrame(root, text="Output", padding=5)
    log_frame.pack(fill="both", expand=True, padx=10, pady=5)

    log_text = tk.Text(log_frame, wrap="word", height=15, font=("Consolas", 10))
    log_text.pack(fill="both", expand=True)

    root.mainloop()


if __name__ == "__main__":
    main()
