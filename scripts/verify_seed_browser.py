# ruff: noqa: E501
"""Real local browser CRUD using the existing seeded development account.

Reads the ignored local credentials file. Starts only temporary test servers.
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
        CASHLY_SEED_CREDENTIALS_PATH=str(ROOT / ".cashly-dev-account.json"),
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
const {chromium,expect}=require("@playwright/test");
const {randomUUID}=require("node:crypto");
(async()=>{
 let credentials;
 try {credentials=JSON.parse(require("node:fs").readFileSync(process.env.CASHLY_SEED_CREDENTIALS_PATH,"utf8"));}
 catch {throw new Error("Local seed credentials JSON invalid; check .cashly-dev-account.json.");}
 const browser=await chromium.launch({channel:"chrome",headless:true});
 const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:"reduce"});
 const page=await context.newPage();
 const errors=[];page.on("pageerror",e=>errors.push(e.message));
 const site=process.env.CASHLY_SMOKE_SITE;
 const merchant="Browser QA "+randomUUID().slice(0,8);
 let identifier;
 let stage="login";
 try {
  await page.goto(site+"/login");
  const form=page.locator(".login-form:visible");
  await form.getByLabel("Email",{exact:true}).fill(credentials.email);
  await form.getByLabel("Kata sandi",{exact:true}).fill(credentials.password);
  await page.getByRole("button",{name:"Masuk",exact:true}).click();
  await page.waitForURL("**/beranda",{timeout:30000});
  const baseline=(await (await context.request.get(site+"/api/cashly/gamification/overview")).json()).data;
  await expect(page.getByText(`${baseline.current_streak} hari streak`,{exact:false})).toBeVisible();
  stage="environment-week-and-background-session";
  await expect(page.locator(".environment-indicator")).toContainText("Development");
  await expect(page.locator(".environment-indicator")).toContainText("Database lokal");
  expect(await page.locator(".streak-calendar li > span:first-child").allTextContents()).toEqual(["Sen","Sel","Rab","Kam","Jum","Sab","Min"]);
  await page.evaluate(()=>{window.cashlyDocumentProbe="same-document";});
  const sessionCheck=page.waitForResponse(r=>r.url().endsWith("/auth/session"));
  const refreshed=page.waitForResponse(r=>r.url().endsWith("/api/cashly/gamification/overview"));
  await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent("pageshow",{persisted:true})));
  if((await sessionCheck).status()!==200) throw new Error("background-session-failed");
  await refreshed;
  expect(await page.evaluate(()=>window.cashlyDocumentProbe)).toBe("same-document");
  // UI-only response fixtures exercise both states without resetting real user XP.
  stage="checkin-ui-transition";
  const checkedGame={...baseline,today_checked_in:true,calendar:baseline.calendar.map(d=>d.date===baseline.today_local_date?{...d,status:"active"}:d)};
  let displayGame={...baseline,today_checked_in:false,
   calendar:baseline.calendar.map(d=>d.date===baseline.today_local_date?{...d,status:"missed"}:d)};
  await page.route("**/api/cashly/gamification/overview",route=>route.fulfill({
   status:200,contentType:"application/json",body:JSON.stringify({data:displayGame})}));
  await page.route("**/api/cashly/gamification/checkins/review",route=>{
   displayGame=checkedGame;
   return route.fulfill({status:200,contentType:"application/json",body:JSON.stringify({data:displayGame})});
  });
  const simulated=page.waitForResponse(r=>r.url().endsWith("/api/cashly/gamification/overview"));
  await page.evaluate(()=>window.dispatchEvent(new Event("cashly-session-validated")));
  await simulated;
  await expect(page.locator(".checkin-status")).toHaveText("Belum check-in");
  await expect(page.locator('.streak-calendar li[aria-current="date"] .day-marker')).toHaveClass(/is-dim/);
  expect(await page.locator(".streak-calendar button").count()).toBe(1);
  await page.locator('.streak-calendar li[aria-current="date"] button').click();
  await expect(page.locator(".checkin-status")).toHaveText("Sudah check-in");
  await expect(page.locator('.streak-calendar li[aria-current="date"] .day-marker')).toHaveClass(/is-active/);
  require("node:fs").mkdirSync("test-results",{recursive:true});
  await page.evaluate(()=>document.documentElement.dataset.theme="dark");
  await page.locator(".streak-card").screenshot({path:"test-results/streak-card-dark.png"});
  await page.evaluate(()=>document.documentElement.dataset.theme="light");
  await page.locator(".streak-card").screenshot({path:"test-results/streak-card-light.png"});
  stage="weekly-weekday-gate";
  displayGame={...checkedGame,missions:checkedGame.missions.map(m=>m.code==="weekly"?{...m,completed:false,can_complete:false,test_override:false}:m)};
  const lockedRefresh=page.waitForResponse(r=>r.url().endsWith("/api/cashly/gamification/overview"));
  await page.evaluate(()=>window.dispatchEvent(new Event("cashly-session-validated")));
  await lockedRefresh;
  await expect(page.locator(".weekly-mission-link")).toContainText("Sabtu");
  await page.locator(".weekly-mission-link").click();
  await expect(page.getByRole("dialog").getByRole("button",{name:/Selesai review/})).toBeDisabled();
  await expect(page.getByRole("dialog").getByText(/Review tersedia Sabtu/)).toBeVisible();
  await page.getByRole("dialog").getByRole("button",{name:"Tutup dialog",exact:true}).click();
  stage="weekly-review-placement";
  displayGame={...checkedGame,missions:checkedGame.missions.map(m=>m.code==="weekly"?{...m,completed:false,can_complete:true,test_override:true}:m)};
  await page.route("**/api/cashly/reports/weekly-story/complete",route=>{
   displayGame={...displayGame,missions:displayGame.missions.map(m=>m.code==="weekly"?{...m,completed:true}:m)};
   return route.fulfill({status:200,contentType:"application/json",body:JSON.stringify({data:displayGame})});
  });
  const weeklyRefresh=page.waitForResponse(r=>r.url().endsWith("/api/cashly/gamification/overview"));
  await page.evaluate(()=>window.dispatchEvent(new Event("cashly-session-validated")));
  await weeklyRefresh;
  await expect(page.locator(".weekly-mission-link")).toContainText("Belum selesai");
  await expect(page.getByRole("heading",{name:"Misi mingguan",exact:true})).toHaveCount(0);
  await page.locator(".weekly-mission-link").click();
  const story=page.getByRole("dialog");
  await expect(story.getByText("Seminggu, satu cerita kecil.",{exact:true})).toBeVisible();
  const weeklyButton=story.getByRole("button",{name:/Selesai review/});
  const buttonBounds=await weeklyButton.boundingBox();
  const controlsBounds=await story.locator(".weekly-review-controls").boundingBox();
  if(!buttonBounds || !controlsBounds || buttonBounds.height>40 ||
      Math.abs(buttonBounds.x+buttonBounds.width/2-controlsBounds.x-controlsBounds.width/2)>3)
    throw new Error("weekly-button-not-compact-and-centered");
  await story.locator(".weekly-review-controls").screenshot({path:"test-results/weekly-review-button-compact.png"});
  await weeklyButton.click();
  await expect(story.locator(".weekly-reviewed-status")).toHaveText("Sudah direview minggu ini");
  await story.getByRole("button",{name:"Tutup dialog",exact:true}).click();
  await expect(page.locator(".weekly-mission-link")).toContainText("Selesai");
  stage="weekly-status-sync-between-pages";
  await page.locator(".bottom-nav").getByRole("link",{name:"Laporan",exact:true}).click();
  await page.waitForURL("**/laporan");
  await page.locator(".report-story-card button").click();
  await expect(page.getByRole("dialog").locator(".weekly-reviewed-status")).toHaveText("Sudah direview minggu ini");
  await page.getByRole("dialog").getByRole("button",{name:"Tutup dialog",exact:true}).click();
  await page.locator(".bottom-nav").getByRole("link",{name:"Beranda",exact:true}).click();
  await page.waitForURL("**/beranda");
  await expect(page.locator(".weekly-mission-link")).toContainText("Selesai");
  // Reverse direction: complete in Reports, then return to the existing Home tab.
  displayGame={...displayGame,missions:displayGame.missions.map(m=>m.code==="weekly"?{...m,completed:false}:m)};
  await page.locator(".bottom-nav").getByRole("link",{name:"Laporan",exact:true}).click();
  await page.waitForURL("**/laporan");
  await page.locator(".report-story-card button").click();
  const reportReview=page.getByRole("dialog").getByRole("button",{name:/Selesai review/});
  await expect(reportReview).toBeEnabled();
  await reportReview.click();
  await expect(page.getByRole("dialog").locator(".weekly-reviewed-status")).toHaveText("Sudah direview minggu ini");
  await page.getByRole("dialog").getByRole("button",{name:"Tutup dialog",exact:true}).click();
  await page.locator(".bottom-nav").getByRole("link",{name:"Beranda",exact:true}).click();
  await page.waitForURL("**/beranda");
  await expect(page.locator(".weekly-mission-link")).toContainText("Selesai");
  await page.locator(".missions-area").screenshot({path:"test-results/weekly-mission-compact.png"});
  await page.unroute("**/api/cashly/reports/weekly-story/complete");
  await page.unroute("**/api/cashly/gamification/overview");
  await page.unroute("**/api/cashly/gamification/checkins/review");
  stage="quick-add-menus";
  for(const path of ["/beranda","/aktivitas","/laporan","/profil"]){
   await page.goto(site+path);
   await page.locator(".bottom-nav .quick-add").click();
   const menu=page.getByRole("dialog",{name:"Catat pengeluaran baru"});
   for(const title of ["Scan struk otomatis","Voice note kilat","Chat manual"])
    await expect(menu.getByText(title,{exact:true})).toBeVisible();
   await expect(menu.getByRole("button",{name:"Form manual",exact:true})).toHaveCount(0);
   await menu.getByRole("button",{name:"Batal",exact:true}).click();
  }
  stage="api-create-fixture";
  const created=await context.request.post(site+"/api/cashly/expenses",{
   headers:{Origin:site,"Idempotency-Key":randomUUID()},
   data:{merchant,description:"Browser test item",category_code:"transport",
    occurred_at:new Date().toISOString(),payment_method:"qris",amount_idr:"24690",
    discount_idr:"0",tax_idr:"0",items:[{label:"Browser test item",quantity:2,unit_price_idr:"12345"}]}
  });
  if(created.status()!==201) throw new Error("create-status-"+created.status());
  identifier=(await created.json()).data.id;
  stage="read-persisted";
  await page.goto(site+"/aktivitas");
  const row=page.locator(".transaction-row").filter({hasText:merchant});
  await expect(row).toBeVisible();
  await row.click();
  let detail=page.getByRole("dialog");
  await detail.getByRole("button",{name:"Ubah detail",exact:true}).click();
  stage="edit";
  await page.locator('dialog[open] [id="scan-merchant"]').fill(merchant+" edited");
  const edited=page.waitForResponse(r=>r.url().endsWith("/api/cashly/expenses/"+identifier)&&r.request().method()==="PATCH");
  await page.getByRole("button",{name:"Simpan perubahan",exact:true}).click();
  if((await edited).status()!==200) throw new Error("edit-failed");
  await page.getByRole("button",{name:"Selesai melihat detail"}).click();
  await page.reload();
  await page.locator(".transaction-row").filter({hasText:merchant+" edited"}).click();
  stage="delete-undo";
  await page.getByRole("dialog").getByRole("button",{name:"Hapus",exact:true}).click();
  const deleted=page.waitForResponse(r=>r.request().method()==="DELETE"&&r.url().includes(identifier));
  await page.getByRole("button",{name:"Hapus pengeluaran",exact:true}).click();
  if((await deleted).status()!==200) throw new Error("delete-failed");
  await expect(page.locator(".transaction-row").filter({hasText:merchant})).toHaveCount(0);
  const restored=page.waitForResponse(r=>r.url().endsWith(identifier+"/restore"));
  await page.locator(".activity-undo button").click();
  if((await restored).status()!==200) throw new Error("undo-failed");
  await expect(page.locator(".transaction-row").filter({hasText:merchant})).toBeVisible();
  stage="report-game";
  for(const path of ["categories","reports/summary","reports/trend","reports/weekly-story","gamification/overview","gamification/missions","gamification/streak/calendar"]){
   const response=await context.request.get(site+"/api/cashly/"+path);
   if(!response.ok()) throw new Error(path+"-failed");
  }
  const currentGame=(await (await context.request.get(site+"/api/cashly/gamification/overview")).json()).data;
  await page.goto(site+"/profil");
  await expect(page.getByText(String(currentGame.total_xp),{exact:false}).first()).toBeVisible();
  await page.goto(site+"/laporan");
  await expect(page.locator("#reports-main")).toBeVisible();
  if(errors.length) throw new Error("page-error-"+errors[0]);
  require("node:fs").mkdirSync("test-results",{recursive:true});
  await page.screenshot({path:"test-results/seed-api-browser-mobile.png",fullPage:true});
  console.log("PASS: dynamic dev database badge, Monday-Sunday streak, check-in UI states (fixtures), background session without reload, compact weekly review and Home/Reports two-way sync (fixtures), original OCR/VN/chat menus, seeded login, API create and UI read/edit/delete/Undo, persistence after reload, report/game endpoints and profile XP.");
 } catch(error){
  require("node:fs").mkdirSync("test-results",{recursive:true});
  await page.screenshot({path:"test-results/seed-api-browser-failure.png",fullPage:true});
  console.log("Failed browser stage: "+stage);
  throw error;
 } finally {
  if(identifier) await context.request.delete(site+"/api/cashly/expenses/"+identifier,{headers:{Origin:site,"Idempotency-Key":randomUUID()}}).catch(()=>{});
  await context.request.post(site+"/auth/logout",{headers:{Origin:site}}).catch(()=>{});
  await browser.close();
 }
})().catch(e=>{console.error("Browser verification failed: "+e.message);process.exitCode=1;});
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
