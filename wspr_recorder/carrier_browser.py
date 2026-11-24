"""A read-only web page for wspr-recorder's carrier archive (carrier_archive.py).

    python -m wspr_recorder.carrier_browser --root DIR [--bind 127.0.0.1] [--port 8089]

DIR is the archive: YYYYMMDD/RECEIVER/BAND/HHMM_MODE_CALL_RFHZ.wav, each a
2-channel float32 IQ .wav at 15 Hz with the decode's particulars as JSON in
its LIST/INFO/ICMT chunk (ubersdr's carrier_iq.h).  The page lists dates, the
cycles of a date and the spots of a cycle, band by band; a spot's file is
plotted in the browser (spectrogram, Doppler and SNR over the transmission)
and can be downloaded.  Standard library only.

Ported from wsprdaemon's carrier-browser.py (WD c1835bf), hardened:
  * read-only: GET only, nothing under DIR is created or written;
  * only DIR is served: /file resolves symlinks and refuses anything whose
    real path is not under DIR's; listings skip symlinks and entries whose
    real path leaves DIR, so a link cannot expose a file's name or comment;
  * binds 127.0.0.1 by default (reach it with ssh -L 8089:127.0.0.1:8089),
    and while bound to loopback refuses requests whose Host header is not a
    loopback name (DNS rebinding);
  * text from file names and WAV comments reaches the page's HTML escaped.

API (JSON): /api/dates, /api/cycles?date=YYYYMMDD, /api/spots?date=YYYYMMDD&hhmm=HHMM;
files: /file?path=REL.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import struct
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

NAME = re.compile(r"^(\d{4})_([A-Z]\d+)_(.+)_(\d+\.\d+)\.wav$")
_DATE = re.compile(r"\d{8}")
_HHMM = re.compile(r"\d{4}")
# Largest carrier file served: an F30 carrier is 1800 s x 15 Hz x 8 B, about
# 216 KB; anything far bigger is not one of ours.
MAX_FILE_BYTES = 16 * 1024 * 1024
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _under(root: str, path: str) -> bool:
    """True iff `path`'s real path is `root` (already real) or inside it."""
    real = os.path.realpath(path)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def _subdirs(root: str, base: str) -> List[str]:
    """Names of `base`'s subdirectories that are real directories inside
    `root` (no symlinks)."""
    try:
        entries = list(os.scandir(base))
    except OSError:
        return []
    out = []
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False) and _under(root, e.path):
                out.append(e.name)
        except OSError:
            continue
    return sorted(out)


def icmt(path: str) -> Tuple[dict, int]:
    """The JSON in a carrier .wav's LIST/INFO/ICMT chunk, and its sample count; ({}, 0) if unreadable."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return {}, 0
        i, meta, n = 12, {}, 0
        while i + 8 <= len(head):
            cid, size = head[i:i + 4], struct.unpack("<I", head[i + 4:i + 8])[0]
            body = head[i + 8:i + 8 + size]
            if cid == b"LIST" and body[:4] == b"INFO":
                j = 4
                while j + 8 <= len(body):
                    sid, m = body[j:j + 4], struct.unpack("<I", body[j + 4:j + 8])[0]
                    if sid == b"ICMT":
                        meta = json.loads(body[j + 8:j + 8 + m].split(b"\0")[0] or b"{}")
                    j += 8 + m + (m & 1)
            elif cid == b"data":
                n = size // 8
                break
            i += 8 + size + (size & 1)
        return (meta if isinstance(meta, dict) else {}), n
    except (OSError, ValueError, struct.error):
        return {}, 0


def dates(root: str) -> List[str]:
    return sorted((d for d in _subdirs(root, root) if _DATE.fullmatch(d)), reverse=True)


def walk_date(root: str, date: str) -> Iterator[Tuple[str, str, str]]:
    """(receiver, band, filename) for every carrier file of a date: regular
    files only, never through a symlink, never outside root."""
    if not _DATE.fullmatch(date or ""):
        return
    base = os.path.join(root, date)
    if date not in _subdirs(root, root):
        return
    for rx in _subdirs(root, base):
        rxd = os.path.join(base, rx)
        for band in _subdirs(root, rxd):
            bd = os.path.join(rxd, band)
            try:
                entries = list(os.scandir(bd))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.name.endswith(".wav") and e.is_file(follow_symlinks=False):
                        yield rx, band, e.name
                except OSError:
                    continue


def cycles(root: str, date: str) -> List[dict]:
    count: dict = {}
    for rx, band, fn in walk_date(root, date):
        m = NAME.match(fn)
        if m:
            count[m.group(1)] = count.get(m.group(1), 0) + 1
    return [{"hhmm": h, "spots": count[h]} for h in sorted(count, reverse=True)]


def spots(root: str, date: str, hhmm: str) -> List[dict]:
    out = []
    for rx, band, fn in walk_date(root, date):
        m = NAME.match(fn)
        if not m or m.group(1) != hhmm:
            continue
        rel = f"{date}/{rx}/{band}/{fn}"
        meta, n = icmt(os.path.join(root, rel))
        out.append({"receiver": rx, "band": band, "mode": m.group(2), "call": m.group(3), "rf_hz": float(m.group(4)),
                    "path": rel, "samples": n, "meta": meta})
    out.sort(key=lambda s: (s["receiver"], s["rf_hz"]))
    return out


def resolve_file(root: str, rel: str) -> Optional[str]:
    """The real path of carrier file `rel` under `root`, or None: not a
    .wav, not a regular file, or its real path is not inside root."""
    if not rel or "\0" in rel:
        return None
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(root.rstrip(os.sep) + os.sep):
        return None
    if not full.endswith(".wav") or not os.path.isfile(full):
        return None
    return full


def _is_loopback(bind: str) -> bool:
    if bind == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def _host_name(header: str) -> str:
    h = (header or "").strip().lower()
    if h.startswith("["):                       # [::1]:8089
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


class Handler(BaseHTTPRequestHandler):
    server_version = "carrier-browser"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src 'self' data:; "
                         "frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        root = self.server.root
        if self.server.loopback_only and _host_name(self.headers.get("Host", "")) not in LOOPBACK_HOSTS:
            return self.send(403, json.dumps({"error": "host"}))
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self.send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/dates":
                return self.send(200, json.dumps(dates(root)))
            if u.path == "/api/cycles":
                return self.send(200, json.dumps(cycles(root, q.get("date", ""))))
            if u.path == "/api/spots":
                if not _HHMM.fullmatch(q.get("hhmm", "")):
                    return self.send(400, json.dumps({"error": "hhmm"}))
                return self.send(200, json.dumps(spots(root, q.get("date", ""), q["hhmm"])))
            if u.path == "/file":
                full = resolve_file(root, q.get("path", ""))
                if full is None or os.path.getsize(full) > MAX_FILE_BYTES:
                    return self.send(404, json.dumps({"error": "no such file"}))
                with open(full, "rb") as f:
                    data = f.read(MAX_FILE_BYTES + 1)
                name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(full))
                return self.send(200, data, "audio/wav",
                                 {"Content-Disposition": f'attachment; filename="{name}"'})
            return self.send(404, json.dumps({"error": "not found"}))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            return self.send(404, json.dumps({"error": "no such file"}))


def build_server(root, bind: str = "127.0.0.1", port: int = 8089) -> ThreadingHTTPServer:
    """The server for archive `root` (not created if missing: read-only).
    Refuses / as the root: that would serve every .wav on the host."""
    if os.path.realpath(str(root)) == os.sep:
        raise ValueError("refusing to serve / as the carrier archive root")
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    srv.root = os.path.realpath(str(root))
    srv.loopback_only = _is_loopback(bind)
    return srv


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m wspr_recorder.carrier_browser",
                                 description="Browse wspr-recorder's carrier archive (read-only).")
    ap.add_argument("--root", required=True, help="the carrier archive ([carrier_archive] root)")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1; use ssh -L to reach it)")
    ap.add_argument("--port", type=int, default=8089)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    try:
        srv = build_server(a.root, a.bind, a.port)
    except ValueError as exc:
        print(f"carrier-browser: {exc}", file=sys.stderr, flush=True)
        sys.exit(2)
    if not srv.loopback_only:
        print(f"carrier-browser: WARNING: listening on {a.bind}, not loopback: "
              f"anyone who can reach it can read the archive", file=sys.stderr, flush=True)
    print(f"carrier-browser: {srv.root} at http://{a.bind}:{a.port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spot Carriers</title>
<style>
:root{--bg:#fbfbfa;--fg:#1d1f21;--muted:#6b6f75;--line:#dcdfe3;--card:#fff;--acc:#1f6fb5;--a2:#d9730d;--sel:#e8f1fa}
@media (prefers-color-scheme: dark){:root{--bg:#16181b;--fg:#e6e6e6;--muted:#9aa0a6;--line:#2c3036;--card:#1e2125;--acc:#6fb3f2;--a2:#f2a35e;--sel:#203040}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
h1{font-size:16px;margin:0 8px 0 0}label{color:var(--muted);font-size:12px;margin-right:4px}
select,button{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
button{cursor:pointer}main{display:grid;grid-template-columns:minmax(320px,1fr) minmax(360px,1.4fr);gap:16px;padding:16px}
@media (max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600}tr.spot{cursor:pointer}tr.spot:hover{background:var(--sel)}tr.on{background:var(--sel)}
tr.band td{font-weight:600;padding-top:10px;color:var(--muted)}.num{text-align:right}.muted{color:var(--muted)}
canvas{width:100%;display:block;margin-top:8px}.legend span{margin-right:12px;font-size:12px}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
#info{font-size:13px}#info b{font-weight:600}a{color:var(--acc)}
.tablewrap{overflow-x:auto;max-height:75vh;overflow-y:auto}
</style></head><body>
<header><h1>Spot carriers</h1>
<span><label for="date">Date (UTC)</label><select id="date"></select></span>
<span><label for="cycle">Cycle</label><button id="prev" title="Earlier cycle">&#9664;</button> <select id="cycle"></select> <button id="next" title="Later cycle">&#9654;</button></span>
<span><label for="rx">Receiver</label><select id="rx"><option value="">all</option></select></span>
<span><label for="band">Band</label><select id="band"><option value="">all</option></select></span>
<span><label><input type="checkbox" id="overlay"> overlay spots (Doppler in ppb)</label></span>
</header>
<main>
<section class="card"><div class="tablewrap"><table><thead><tr><th>Call</th><th>Message</th><th class="num">SNR</th><th class="num">Frequency, Hz</th><th class="num">Spread, Hz</th><th>Mode</th></tr></thead><tbody id="spots"></tbody></table></div><p id="count" class="muted"></p></section>
<section class="card"><div id="info" class="muted">Pick a spot to plot its carrier.</div>
<canvas id="spec" height="220" aria-label="Spectrogram"></canvas>
<canvas id="dop" height="220" aria-label="Doppler"></canvas><div class="legend" id="legend"></div>
<canvas id="snr" height="140" aria-label="SNR"></canvas></section>
</main>
<script>
"use strict";
const $=id=>document.getElementById(id);
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}
function num(v,d){const x=Number(v);return v==null||!isFinite(x)?"":x.toFixed(d);}
const COLORS=["#1f6fb5","#d9730d","#2a9d55","#b5341f","#7a4fb5","#8a6d1f","#1f8f9a"];
let spotsData=[], overlay=[];
async function j(url){const r=await fetch(url);if(!r.ok)throw new Error(url+": "+r.status);return r.json();}
function opt(sel,v,t){const o=document.createElement("option");o.value=v;o.textContent=t??v;sel.appendChild(o);}
function hhmm(h){return h.slice(0,2)+":"+h.slice(2);}
async function loadDates(){const d=await j("/api/dates");$("date").innerHTML="";d.forEach(x=>opt($("date"),x,x.slice(0,4)+"-"+x.slice(4,6)+"-"+x.slice(6)));
 if(!d.length){$("count").textContent="No carrier files yet. They appear after the decoders (ubersdr wsprd -I, jt9 --carrier-dir) have run a cycle.";return;}
 const p=new URLSearchParams(location.hash.slice(1));if(p.get("date")&&d.includes(p.get("date")))$("date").value=p.get("date");await loadCycles(p.get("hhmm"));}
async function loadCycles(want){const c=await j("/api/cycles?date="+$("date").value);$("cycle").innerHTML="";c.forEach(x=>opt($("cycle"),x.hhmm,hhmm(x.hhmm)+"  ("+x.spots+")"));
 if(want&&c.some(x=>x.hhmm===want))$("cycle").value=want;await loadSpots();}
async function loadSpots(){if(!$("cycle").value){$("spots").innerHTML="";return;}
 location.hash="date="+$("date").value+"&hhmm="+$("cycle").value;
 spotsData=await j("/api/spots?date="+$("date").value+"&hhmm="+$("cycle").value);
 for(const [id,key] of [["rx","receiver"],["band","band"]]){const cur=$(id).value;$(id).innerHTML='<option value="">all</option>';[...new Set(spotsData.map(s=>s[key]))].sort((a,b)=>isNaN(a)?a.localeCompare(b):b-a).forEach(v=>opt($(id),v));if([...$(id).options].some(o=>o.value===cur))$(id).value=cur;}
 render();}
function render(){const tb=$("spots");tb.innerHTML="";const rx=$("rx").value,band=$("band").value;
 const list=spotsData.filter(s=>(!rx||s.receiver===rx)&&(!band||s.band===band));let lastKey="";
 for(const s of list){const key=s.receiver+" · "+s.band+" m";if(key!==lastKey){const tr=document.createElement("tr");tr.className="band";tr.innerHTML='<td colspan="6">'+esc(key)+"</td>";tb.appendChild(tr);lastKey=key;}
  const m=s.meta||{};const tr=document.createElement("tr");tr.className="spot";tr.tabIndex=0;
  const fine=s.rf_hz-(Number(m.ref_hz)||0)+(m.fine_hz!=null?Number(m.fine_hz):(Number(m.ref_hz)||0));  // RF: the file name's (dial + ref) moved to the fine frequency
  tr.innerHTML="<td>"+esc(s.call)+'</td><td class="muted">'+esc(m.message)+'</td><td class="num">'+esc(m.snr)+'</td><td class="num">'+num(fine,3)+'</td><td class="num">'+num(m.w50_hz,3)+"</td><td>"+esc(s.mode)+"</td>";
  tr.onclick=()=>pick(s,tr);tr.onkeydown=e=>{if(e.key==="Enter")pick(s,tr);};tb.appendChild(tr);}
 $("count").textContent=list.length+" spot"+(list.length===1?"":"s")+" in this cycle";}
function parseWav(buf){const v=new DataView(buf);let i=12,rate=15,meta={},iq=null;
 while(i+8<=v.byteLength){const id=String.fromCharCode(...new Uint8Array(buf,i,4)),n=v.getUint32(i+4,true);
  if(id==="fmt ")rate=v.getUint32(i+12,true);
  else if(id==="LIST"){const t=new TextDecoder().decode(new Uint8Array(buf,i+20,v.getUint32(i+16,true)));try{meta=JSON.parse(t.split("\0")[0]);}catch(e){}}
  else if(id==="data"){iq=new Float32Array(buf.slice(i+8,i+8+n));break;}
  i+=8+n+(n&1);}
 return {rate,meta,iq};}
function fft(re,im){const n=re.length;for(let i=1,j=0;i<n;i++){let b=n>>1;for(;j&b;b>>=1)j^=b;j^=b;if(i<j){[re[i],re[j]]=[re[j],re[i]];[im[i],im[j]]=[im[j],im[i]];}}
 for(let len=2;len<=n;len<<=1){const a=-2*Math.PI/len,wr=Math.cos(a),wi=Math.sin(a);for(let i=0;i<n;i+=len){let cr=1,ci=0;for(let k=0;k<len/2;k++){const p=i+k,q=p+len/2,tr=re[q]*cr-im[q]*ci,ti=re[q]*ci+im[q]*cr;re[q]=re[p]-tr;im[q]=im[p]-ti;re[p]+=tr;im[p]+=ti;const t=cr*wr-ci*wi;ci=cr*wi+ci*wr;cr=t;}}}}
function spectrum(iq,s,n,nfft,win){const re=new Float64Array(nfft),im=new Float64Array(nfft);for(let k=0;k<n;k++){const w=win?0.5-0.5*Math.cos(2*Math.PI*k/(n-1)):1;re[k]=iq[2*(s+k)]*w;im[k]=iq[2*(s+k)+1]*w;}
 fft(re,im);const p=new Float64Array(nfft);for(let k=0;k<nfft;k++)p[k]=re[k]*re[k]+im[k]*im[k];return p;}
function fq(k,nfft,rate){return (k>=nfft/2?k-nfft:k)*rate/nfft;}
function peak(iq,s,n,rate,centre,span){const nfft=1<<Math.ceil(Math.log2(n*64)),p=spectrum(iq,s,n,nfft,false);let best=-1,bk=0;
 for(let k=0;k<nfft;k++){const f=fq(k,nfft,rate);if(centre!=null&&Math.abs(f-centre)>span)continue;if(p[k]>best){best=p[k];bk=k;}}
 const a=p[(bk-1+nfft)%nfft],b=p[bk],c=p[(bk+1)%nfft],den=a-2*b+c,d=den?0.5*(a-c)/den:0;return fq(bk,nfft,rate)+d*rate/nfft;}
function snr(iq,s,n,rate){const nfft=1<<Math.ceil(Math.log2(n)),p=spectrum(iq,s,n,nfft,true);let bk=0;for(let k=1;k<nfft;k++)if(p[k]>p[bk])bk=k;
 const fk=fq(bk,nfft,rate);let sig=0,ns=0,noise=0,nn=0;for(let k=0;k<nfft;k++){const d=Math.abs(fq(k,nfft,rate)-fk);if(d<0.5){sig+=p[k];ns++;}else if(d>3){noise+=p[k];nn++;}}
 noise/=Math.max(nn,1);return 10*Math.log10(Math.max(sig-noise*ns,1e-30)/(noise/(rate/n)*2500));}
function track(c){const {iq,rate,meta}=c,N=iq.length/2,win=(meta.snr!=null&&meta.snr<=-15)?30:10,nw=Math.min(N,Math.round(win*rate)),ns=Math.round(2*rate);
 const whole=peak(iq,0,N,rate),pts=[];for(let s=0;s+nw<=N;s+=ns)pts.push({t:(meta.t0_s||0)+(s+nw/2)/rate,f:peak(iq,s,nw,rate,whole,2),snr:snr(iq,s,nw,rate)});
 return {whole,win,pts};}
function axes(cv,xr,yr,xl,yl){const dpr=window.devicePixelRatio||1,W=cv.clientWidth;if(!cv.dataset.h)cv.dataset.h=cv.getAttribute("height");const H=+cv.dataset.h;
 cv.width=W*dpr;cv.height=H*dpr;cv.style.height=H+"px";
 const g=cv.getContext("2d");g.setTransform(dpr,0,0,dpr,0,0);const cs=getComputedStyle(document.body),fg=cs.getPropertyValue("--fg"),mu=cs.getPropertyValue("--muted"),ln=cs.getPropertyValue("--line");
 const L=58,R=10,T=18,B=30,w=W-L-R,h=H-T-B;g.font="11px system-ui";g.strokeStyle=ln;g.fillStyle=mu;
 const X=x=>L+(x-xr[0])/(xr[1]-xr[0])*w,Y=y=>T+h-(y-yr[0])/(yr[1]-yr[0])*h;
 for(let i=0;i<=5;i++){const y=yr[0]+(yr[1]-yr[0])*i/5;g.beginPath();g.moveTo(L,Y(y));g.lineTo(L+w,Y(y));g.stroke();g.fillText(+y.toPrecision(3)+"",4,Y(y)+4);}
 for(let i=0;i<=6;i++){const x=xr[0]+(xr[1]-xr[0])*i/6;g.fillText(Math.round(x)+"",X(x)-8,T+h+14);}
 g.fillText(xl,L+w/2-40,T+h+27);g.fillStyle=fg;g.fillText(yl,L,12);
 return {g,X,Y,L,T,w,h};}
function drawSpec(c){const {iq,rate,meta}=c,N=iq.length/2,n=Math.round(10*rate),hop=Math.round(rate),nfft=1024,span=3;const cols=[];
 for(let s=0;s+n<=N;s+=hop){const p=spectrum(iq,s,n,nfft,true),col=[];for(let k=0;k<nfft;k++){const f=fq(k,nfft,rate);if(Math.abs(f)<=span)col.push([f,10*Math.log10(p[k]+1e-30)]);}col.sort((a,b)=>a[0]-b[0]);cols.push(col);}
 const t0=(meta.t0_s||0)+5,t1=t0+(cols.length-1);const A=axes($("spec"),[t0-5,t1+5],[-span,span],"seconds into the cycle","Hz from "+(c.ref).toFixed(3)+" Hz (decoder's frequency)");
 let mx=-1e9;cols.forEach(col=>col.forEach(v=>{if(v[1]>mx)mx=v[1];}));const img=A.g;const cw=Math.max(1,A.w/cols.length),rh=A.h/cols[0].length;
 cols.forEach((col,i)=>col.forEach((v,k)=>{const z=Math.max(0,Math.min(1,(v[1]-(mx-40))/40));img.fillStyle="hsl("+(240-240*z)+",80%,"+(15+45*z)+"%)";img.fillRect(A.X(t0+i)-cw/2,A.Y(v[0])-rh,cw+0.5,rh+0.5);}));}
function plotLines(cv,series,yl,key){let xs=[],ys=[];series.forEach(s=>s.pts.forEach(p=>{xs.push(p.t);ys.push(p[key]);}));if(!xs.length)return;
 let y0=Math.min(...ys),y1=Math.max(...ys);if(y1-y0<1e-6){y0-=1;y1+=1;}const pad=(y1-y0)*0.1;const A=axes(cv,[Math.min(...xs)-5,Math.max(...xs)+5],[y0-pad,y1+pad],"seconds into the cycle",yl);
 series.forEach(s=>{A.g.strokeStyle=s.color;A.g.fillStyle=s.color;A.g.lineWidth=1.5;A.g.beginPath();s.pts.forEach((p,i)=>{const x=A.X(p.t),y=A.Y(p[key]);i?A.g.lineTo(x,y):A.g.moveTo(x,y);});A.g.stroke();s.pts.forEach(p=>A.g.fillRect(A.X(p.t)-1.5,A.Y(p[key])-1.5,3,3));});}
async function pick(s,tr){document.querySelectorAll("tr.on").forEach(r=>{if(!$("overlay").checked)r.classList.remove("on");});tr.classList.add("on");
 $("info").textContent="Loading "+s.call+" ...";const buf=await (await fetch("/file?path="+encodeURIComponent(s.path))).arrayBuffer();const c=parseWav(buf);
 c.ref=s.rf_hz;const t=track(c);
 const label=s.call+" "+s.band+" m "+s.receiver;const mean=s.rf_hz+t.whole;  // the file name's RF is the carrier's reference
 const entry={label,color:COLORS[overlay.length%COLORS.length],pts:t.pts.map(p=>({t:p.t,hz:(p.f-t.whole)*1000,ppb:(p.f-t.whole)/mean*1e9,snr:p.snr}))};
 overlay=$("overlay").checked?overlay.concat([entry]):[entry];
 const m=c.meta||{};$("info").innerHTML="<b>"+esc(s.call)+"</b> "+esc(m.message)+" &middot; "+esc(s.receiver)+" "+esc(s.band)+" m &middot; "+esc(s.mode)+" &middot; SNR "+(esc(m.snr)||"?")+" dB &middot; spread "+(num(m.w50_hz,3)||"?")+" Hz<br>frequency over the transmission "+num(mean,4)+" Hz &middot; "+esc(t.win)+" s windows &middot; <a href=\"/file?path="+encodeURIComponent(s.path)+"\">download .wav</a>";
 drawSpec(c);
 if($("overlay").checked)plotLines($("dop"),overlay,"Doppler, ppb from each spot's mean","ppb");else plotLines($("dop"),overlay,"mHz from the transmission's mean","hz");
 plotLines($("snr"),overlay,"SNR, dB in 2500 Hz","snr");
 $("legend").innerHTML=overlay.map(o=>'<span><i class="sw" style="background:'+esc(o.color)+'"></i>'+esc(o.label)+"</span>").join("");}
$("date").onchange=()=>loadCycles();$("cycle").onchange=loadSpots;$("rx").onchange=render;$("band").onchange=render;
$("prev").onclick=()=>{const s=$("cycle");if(s.selectedIndex<s.options.length-1){s.selectedIndex++;loadSpots();}};
$("next").onclick=()=>{const s=$("cycle");if(s.selectedIndex>0){s.selectedIndex--;loadSpots();}};
$("overlay").onchange=()=>{overlay=[];document.querySelectorAll("tr.on").forEach(r=>r.classList.remove("on"));};
loadDates().catch(e=>{$("count").textContent=String(e);});
</script></body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
