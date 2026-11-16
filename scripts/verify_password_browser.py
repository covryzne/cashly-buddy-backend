"""Real FE ? FastAPI ? local Supabase smoke. Creates one fictional local QA account.

No private env files are opened. Uses temporary app ports; does not stop operator servers.
"""

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import uvicorn
from pydantic import SecretStr

from cashly.core.settings import Settings
from cashly.main import create_app

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT.parent / "cashly-buddy-frontend"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    status = subprocess.run(
        ["node", "node_modules/supabase/dist/supabase.js", "status", "-o", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    values = json.loads(status.stdout)
    if values.get("API_URL") != "http://127.0.0.1:54321":
        raise RuntimeError("Expected the local Supabase project")
    key = values["PUBLISHABLE_KEY"]
    api_port, web_port = free_port(), free_port()
    settings = Settings(
        environment="dev",
        log_level="ERROR",
        supabase_url=values["API_URL"],
        supabase_publishable_key=SecretStr(key),
        dev_sample_data=True,
        database_url=SecretStr(
            "postgresql+psycopg://cashly_app:cashly-local-only@127.0.0.1:54322/postgres?sslmode=disable"
        ),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=api_port,
            access_log=False,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    site = f"http://127.0.0.1:{web_port}"
    env = {
        k: os.environ[k]
        for k in (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "LOCALAPPDATA",
            "USERPROFILE",
        )
        if k in os.environ
    }
    env.update(
        CASHLY_ENV="dev",
        CASHLY_ENV_SOURCE="system",
        APP_ORIGIN=site,
        CASHLY_API_ORIGIN=f"http://127.0.0.1:{api_port}",
        CASHLY_SMOKE_SITE=site,
    )
    # Prevent legacy dotenv loading; Next does not load branch-specific .env.dev.
    for filename in (".env", ".env.local", ".env.production", ".env.production.local"):
        if (FRONTEND / filename).exists():
            raise RuntimeError("Legacy dotenv file present; refusing to read private files")
    child = None
    try:
        child = subprocess.Popen(
            [
                "node",
                "node_modules/next/dist/bin/next",
                "start",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(web_port),
            ],
            cwd=FRONTEND,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 25
        while True:
            try:
                with urlopen(site + "/login", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Temporary frontend failed to start")
            time.sleep(0.1)
        javascript = r"""
const { chromium } = require("@playwright/test");
const { randomUUID } = require("node:crypto");
(async () => {
 const browser=await chromium.launch({channel:"chrome",headless:true});
 const context=await browser.newContext({viewport:{width:390,height:844},colorScheme:"dark"});
 const page=await context.newPage();
 const form=page.locator(".login-form:visible");
 const site=process.env.CASHLY_SMOKE_SITE;
 const email=`cashly-browser-${randomUUID().slice(0,12)}@example.com`;
 const password=`Qa-${randomUUID().slice(0,8)}-9!`;
 let stage="register";
 try {
  let safeRegisterBody = false;
  await page.route("**/auth/email/register", async route => {
   const response = await route.fetch();
   const body = await response.json();
   safeRegisterBody = !Object.keys(body).some(k=>/token|session/.test(k));
   await route.fulfill({response});
  });
  await page.goto(site+"/register");
  await form.getByLabel("Nama panggilan").fill("Flow Browser QA");
  await form.getByLabel("Email",{exact:true}).fill(email);
  await form.getByLabel("Kata sandi",{exact:true}).fill(password);
  await form.getByLabel("Konfirmasi kata sandi",{exact:true}).fill(password);
  const registered=page.waitForResponse(r=>r.url().endsWith("/auth/email/register"));
  await page.getByRole("button",{name:"Buat akun",exact:true}).click();
  const result=await registered;
  if (!result.ok()) throw new Error("register-failed-"+result.status());
  if (!safeRegisterBody) throw new Error("token-exposed");
  const dialog=page.getByRole("dialog",{name:"Akun berhasil dibuat"});
  await dialog.waitFor();
  require("node:fs").mkdirSync("test-results",{recursive:true});
  await page.screenshot({path:"test-results/registration-success-mobile.png"});
  const signupCookies = (await context.cookies()).filter(c=>c.name.startsWith("sb-") && c.value);
  if (signupCookies.length) throw new Error("signup-auto-login");
  await dialog.getByRole("button",{name:"Ke halaman login"}).click();
  await page.waitForURL("**/login");
  stage="login-after-dialog";
  await form.getByLabel("Email",{exact:true}).fill(email);
  await form.getByLabel("Kata sandi",{exact:true}).fill(password);
  await page.getByRole("button",{name:"Masuk",exact:true}).click();
  await page.waitForURL("**/beranda",{timeout:30000});
  const cookies=(await context.cookies()).filter(c=>c.name.startsWith("sb-"));
  if (!cookies.length || cookies.some(c=>!c.httpOnly)) throw new Error("cookies-invalid");
  const logout=await context.request.post(site+"/auth/logout",{headers:{Origin:site}});
  if (logout.status()!==204) throw new Error("logout-failed");
  await page.goto(site+"/login");
  stage="login-after-logout";
  await form.getByLabel("Email",{exact:true}).fill(email);
  await form.getByLabel("Kata sandi",{exact:true}).fill("Wrong-Pass999!");
  await page.getByRole("button",{name:"Masuk",exact:true}).click();
  await page.getByRole("alert").filter({hasText:"Email atau kata sandi tidak sesuai."}).waitFor();
  await form.getByLabel("Kata sandi",{exact:true}).fill(password);
  await page.getByRole("button",{name:"Masuk",exact:true}).click();
  await page.waitForURL("**/beranda",{timeout:30000});
  await context.request.post(site+"/auth/logout",{headers:{Origin:site}});
  console.log("PASS: real signup success dialog, no auto-login, explicit login, " +
              "HttpOnly cookies, logout and password rejection through FE/API/local Supabase.");
 } catch(error) {
  await page.screenshot({path:"test-results/registration-flow-failure.png"});
  console.log("Failed stage: "+stage+" path: "+new URL(page.url()).pathname);
  throw error;
 } finally {await browser.close();}
})().catch(error=>{console.error("Browser flow failed: "+error.message);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-"],
            input=javascript,
            text=True,
            cwd=FRONTEND,
            env=env,
            timeout=100,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Browser smoke failed")
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    main()
