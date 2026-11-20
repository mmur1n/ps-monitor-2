#!/usr/bin/env python3
"""Best-effort read-only PS5 stock watcher for Sony Germany."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

DATA = Path(__file__).resolve().parent / "docs" / "data.json"
PRODUCTS = [
    {"id":"disc-one", "name":"PS5 Disc 1 ТБ · 1 DualSense",
     "url":"https://www.playstation.com/de-de/ps5/1-tb-disc/",
     "title":"PlayStation5 Konsole - 1 TB", "kind":"disc-one", "notify":True},
    {"id":"disc-two", "name":"PS5 Disc 1 ТБ · 2 DualSense",
     "url":"https://www.playstation.com/de-de/ps5/1-tb-disc/product/two-dualsense-wireless-controllers-bundle/",
     "title":"Paket: PlayStation5 Konsole 1 TB - zwei DualSense Wireless Controller",
     "kind":"disc-two", "notify":True},
    {"id":"digital-two", "name":"PS5 Digital 825 ГБ · 2 DualSense",
     "url":"https://direct.playstation.com/de-de/buy-consoles/playstation5-digital-edition-console-825-gb-two-dualsense-wireless-controllers-bundle",
     "title":"Paket: PlayStation5 Digital Edition 825 GB - zwei DualSense Wireless Controller",
     "kind":"digital-two", "notify":False},
]
NEGATIVE = ("nicht zum kauf erhältlich","derzeit nicht vorrätig","derzeit nicht verfügbar","ausverkauft")
POSITIVE = ("in den warenkorb legen","zum warenkorb hinzufügen","in den warenkorb","jetzt kaufen")
CHALLENGE = ("captcha","access denied","verify you are human","unusual traffic")

def norm(s):
    return re.sub(r"\s+", " ", unescape(s).replace("®","").replace("™","")).strip().casefold()

class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip=0
        self.tokens=[]
        self.headings=[]
        self.h1=None
    def handle_starttag(self, tag, attrs):
        if tag in ("script","style","noscript","svg","template"): self.skip+=1
        if tag=="h1" and not self.skip: self.h1=[]
    def handle_endtag(self, tag):
        if tag=="h1" and self.h1 is not None:
            self.headings.append(norm(" ".join(self.h1)))
            self.h1=None
        if tag in ("script","style","noscript","svg","template"): self.skip=max(0,self.skip-1)
    def handle_data(self, text):
        if text.strip() and not self.skip:
            self.tokens.append(text.strip())
            if self.h1 is not None: self.h1.append(text.strip())

def html_excerpt(source, term):
    m=re.search(re.escape(term),source,re.I)
    if not m: return "(phrase across HTML nodes; raw exact substring absent)"
    return source[max(0,m.start()-120):min(len(source),m.end()+180)]

def classify(source, product):
    p=Page()
    p.feed(source)
    body=norm(" ".join(p.tokens))
    r={"status":"UNKNOWN","reason":"Недостаточно данных","title_verified":False,"identity_verified":False,"signals":[]}
    if len(body)<300: return {**r,"status":"ERROR","reason":"Слишком короткий HTML"}
    if any(x in body[:2200] for x in CHALLENGE):
        return {**r,"status":"ERROR","reason":"Sony требует дополнительную проверку"}
    title=norm(product["title"])
    if title not in p.headings:
        return {**r,"reason":"Точный H1 выбранной комплектации не найден"}
    r["title_verified"]=True
    if product["kind"].startswith("disc"):
        identity=("1-tb-ssd" in body and "diese konsole hat ein laufwerk" in body
                  and (product["kind"]=="disc-one" or "2 dualsense" in body))
    else:
        identity=(("825-gb-ssd" in body or "825 gb" in body) and
                  ("ohne laufwerk" in body or "ohne disc-laufwerk" in body) and "2 dualsense" in body)
    if not identity: return {**r,"reason":"Не подтверждены SSD, дисковод или контроллеры"}
    r["identity_verified"]=True
    start=body.find(title)
    vicinity=body[start:start+650]
    banner=body[:start]
    signals=[]
    for word in NEGATIVE:
        area=vicinity if word in vicinity else (banner if word in ("derzeit nicht vorrätig","derzeit nicht verfügbar") else "")
        if word not in area: continue
        i=area.find(word)
        signals.append({"kind":"absent","term":word,"visible":area[max(0,i-90):i+len(word)+90],"html":html_excerpt(source,word)})
    for word in POSITIVE:
        if word in vicinity:
            i=vicinity.find(word)
            signals.append({"kind":"purchase","term":word,"visible":vicinity[max(0,i-90):i+len(word)+90],"html":html_excerpt(source,word)})
    r["signals"]=signals[:10]
    negative=next((s for s in signals if s["kind"]=="absent"),None)
    positive=next((s for s in signals if s["kind"]=="purchase"),None)
    if negative: return {**r,"status":"OUT_OF_STOCK","reason":"Sony: "+negative["term"]}
    if positive: return {**r,"status":"IN_STOCK","reason":"Кнопка покупки: "+positive["term"]}
    return {**r,"reason":"Нет подтверждённого сигнала наличия"}

def fetch(url):
    req=Request(url,headers={"User-Agent":"Mozilla/5.0 (compatible; PersonalPS5StockMonitor/1.0)",
                              "Accept":"text/html,application/xhtml+xml","Accept-Language":"de-DE,de;q=0.9"})
    with urlopen(req,timeout=20) as resp:
        final=resp.geturl()
        parsed=urlsplit(final)
        if parsed.scheme!="https" or parsed.hostname not in ("www.playstation.com","direct.playstation.com") or not parsed.path.startswith("/de-de/"):
            raise ValueError("Unexpected redirect: "+final[:160])
        if "html" not in resp.headers.get("Content-Type","").lower():
            raise ValueError("Not an HTML document")
        raw=resp.read(2500001)
        if len(raw)>2500000: raise ValueError("HTML exceeds 2.5MB")
        return raw.decode(resp.headers.get_content_charset() or "utf-8",errors="replace"),{
          "http_status":resp.status,"final_url":final,"content_bytes":len(raw),
          "sha256":hashlib.sha256(raw).hexdigest()}

def telegram(message):
    token=os.environ.get("TELEGRAM_BOT_TOKEN","")
    chat=os.environ.get("TELEGRAM_CHAT_ID","")
    if not token or not chat: return "not_configured"
    req=Request("https://api.telegram.org/bot"+token+"/sendMessage",
        data=urlencode({"chat_id":chat,"text":message,"disable_web_page_preview":"true"}).encode(),method="POST")
    try:
        with urlopen(req,timeout=15) as r: return "sent" if json.load(r).get("ok") else "failed"
    except (HTTPError,URLError,ValueError,TimeoutError): return "failed"

def load():
    try:
        d=json.loads(DATA.read_text(encoding="utf-8"))
        if isinstance(d,dict): return d
    except (OSError,ValueError): pass
    return {"latest":{},"history":[],"notifications":{}}

def once(pause=5):
    state=load()
    for key,default in (("latest",{}),("history",[]),("notifications",{})):
        state.setdefault(key,default)
    state["products"]=PRODUCTS
    for i,product in enumerate(PRODUCTS):
        if i: time.sleep(pause)
        at=datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            source,meta=fetch(product["url"])
            result=classify(source,product)
            old=state["latest"].get(product["id"],{}).get("status")
            if os.getenv("SAVE_HTML","").lower() in ("true","1") or result["status"]!=old:
                folder=DATA.parent.parent/"evidence"
                folder.mkdir(exist_ok=True)
                with gzip.open(folder/(product["id"]+".html.gz"),"wt",encoding="utf-8") as f: f.write(source)
        except (HTTPError,URLError,ValueError,OSError,TimeoutError) as e:
            result={"status":"ERROR","reason":type(e).__name__+": "+str(e)[:120],"signals":[],
                    "title_verified":False,"identity_verified":False}
            meta={"http_status":getattr(e,"code",None),"final_url":None,"content_bytes":None,"sha256":None}
        result.update(meta)
        result.update({"product_id":product["id"],"checked_at":at})
        sent=state["notifications"].setdefault(product["id"],{"sent":False})
        if result["status"]=="OUT_OF_STOCK": sent["sent"]=False
        if product["notify"] and result["status"]=="IN_STOCK" and not sent["sent"]:
            outcome=telegram("🎮 PS5 в наличии: "+product["name"]+"\n"+result["reason"]+"\n"+product["url"])
            sent["last_attempt"]=at
            sent["last_outcome"]=outcome
            if outcome=="sent": sent["sent"]=True
            print("Telegram "+product["id"]+": "+outcome,flush=True)
        state["latest"][product["id"]]=result
        state["history"].append(result.copy())
        print("["+at+"] "+product["id"]+": "+result["status"]+" ("+result["reason"]+")",flush=True)
    state["history"]=state["history"][-900:]
    state["last_run_at"]=datetime.now(timezone.utc).isoformat(timespec="seconds")
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
    summary=os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary,"a",encoding="utf-8") as f:
            f.write("## Sony stock check\n\n| Model | Status | Decision |\n|---|---|---|\n")
            for p in PRODUCTS:
                r=state["latest"][p["id"]]
                f.write("| "+p["name"]+" | "+r["status"]+" | "+r["reason"].replace("|","/")+" |\n")
    return state

def main():
    cli=argparse.ArgumentParser()
    cli.add_argument("--once",action="store_true")
    cli.add_argument("--interval",type=int,default=300)
    cli.add_argument("--test-telegram",action="store_true")
    args=cli.parse_args()
    if args.test_telegram:
        print("Telegram test: "+telegram("✅ PS5 stock monitor: тестовое уведомление"),flush=True)
        return
    if args.interval<60: cli.error("Minimum 60 seconds")
    while True:
        start=time.monotonic()
        once()
        if args.once: return
        try: time.sleep(max(0,args.interval-(time.monotonic()-start)))
        except KeyboardInterrupt: return
if __name__=="__main__": main()
