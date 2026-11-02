import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BACKEND, FRONTEND = ROOT / "backend", ROOT / "frontend"
PY = str(BACKEND / ".venv" / "bin" / "python")
SECRET = "e2e-secret-e2e-secret-e2e-secret-0123456789"


def _port_open(port: int) -> bool:
    try:
        socket.create_connection(("localhost", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def _wait(url_port: int, timeout=40):
    t = time.time()
    while time.time() - t < timeout:
        try:  # "localhost" covers IPv4 and IPv6 (Vite may listen on ::1 only)
            socket.create_connection(("localhost", url_port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"port {url_port} did not open")


class Stack:
    def __init__(self, tmp: Path, api_port: int, web_port: int, auth_mode: str):
        self.db = tmp / "ui.db"
        self.api_port, self.web_port = api_port, web_port
        self.base = f"http://localhost:{web_port}"
        env = {**os.environ, "DATABASE_URL": f"sqlite:///{self.db}", "CHECKPOINT_DB": str(tmp / "ck.db"),
               "POLL_DISPUTES": "false", "CORS_ORIGINS": self.base, "AUTH_MODE": auth_mode, "AUTH_SECRET": SECRET,
               "MERCHANT_LOGIN_EMAILS": "merchant@business.example.com", "FRONTEND_URL": self.base,
               # hermetic: never call the real LLMs / web search from UI tests (backend/.env has real keys)
               "GROQ_API_KEY": "", "GEMINI_API_KEY": "", "TAVILY_API_KEY": "", "ARTIFACTS_DIR": str(tmp / "artifacts"),
               # a finished buyer session would start an agent run, which fetches the dispute from PayPal
               "TROUBLESHOOTING_REANALYSE": "false",
               "PAYPAL_CLIENT_ID": "e2e", "PAYPAL_CLIENT_SECRET": "e2e", "PYTHONPATH": str(BACKEND)}
        self.env = env
        self.procs = []
        for port in (api_port, web_port):  # never silently test a stale server left on the port
            if _port_open(port):
                raise RuntimeError(f"port {port} is already in use; stop the old server first")
        subprocess.run([PY, str(ROOT / "e2e" / "seed_ui_demo.py")], cwd=BACKEND, env=env, check=True,
                       capture_output=True)
        try:
            # start_new_session: each server gets its own process group, so stop() also kills npx's vite child
            self.procs = [
                subprocess.Popen([PY, "-m", "uvicorn", "app.main:app", "--port", str(api_port)], cwd=BACKEND,
                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True),
                subprocess.Popen(["npx", "vite", "--port", str(web_port), "--strictPort"], cwd=FRONTEND,
                                 env={**env, "VITE_API_BASE_URL": f"http://localhost:{api_port}"},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True),
            ]
            _wait(api_port)
            _wait(web_port)
        except BaseException:
            self.stop()  # don't leave orphans behind when startup fails
            raise

    def sql(self, q, *a):
        con = sqlite3.connect(self.db)
        try:
            cur = con.execute(q, a)
            con.commit()
            return cur.fetchall()
        finally:
            con.close()

    def session_token(self, exp_offset=3600):
        code = ("import time;from app.auth import sign;print(sign({'kind':'session','sub':'e2e',"
                f"'email':'merchant@business.example.com','name':'E2E Merchant','exp':int(time.time()+{exp_offset})}}))")
        return subprocess.run([PY, "-c", code], cwd=BACKEND, env=self.env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def stop(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for p in self.procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)


@pytest.fixture(scope="session")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("ui"), 8001, 5174, "off")
    yield s
    s.stop()


@pytest.fixture(scope="session")
def auth_stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("auth"), 8002, 5175, "paypal")
    yield s
    s.stop()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and pg.errors.append(m.text))
    yield pg
    ctx.close()


@pytest.fixture
def phone(browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    yield pg
    ctx.close()


def overflow(pg) -> int:
    return pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")
