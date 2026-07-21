"""Local REST control server for the Jitsi recorder.

Endpoints
  POST /start    {"url": "...", "name": "..."}  -> begin recording
  POST /stop                                     -> stop current session
  GET  /status                                   -> current session status
  GET  /recordings                               -> list finished mp3 files
  GET  /download/{filename}                      -> download an mp3

Bind to 127.0.0.1 by default. A shared token (RECORDER_TOKEN env var) is
required in the `X-Token` header when set.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import threading
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from recorder import OUTPUT_DIR, RecorderSession
from version import VERSION

app = FastAPI(title="Jitsi Recorder", version=VERSION)
TOKEN = os.environ.get("RECORDER_TOKEN")

TRANSCRIPT_DIR = OUTPUT_DIR / "transcripts"
TRANSCRIPT_DIR.mkdir(exist_ok=True)

# whisperx options (overridable via env, defaults match the user's command).
WHISPER = {
    "bin": os.environ.get("WHISPER_BIN", "whisperx"),
    "model": os.environ.get("WHISPER_MODEL", "large-v3"),
    "language": os.environ.get("WHISPER_LANGUAGE", "ru"),
    "device": os.environ.get("WHISPER_DEVICE", "cuda"),
    "compute_type": os.environ.get("WHISPER_COMPUTE", "float16"),
}

# Active + recent recording sessions, keyed by short id.
_sessions: dict[str, RecorderSession] = {}
_sessions_lock = threading.Lock()

# Transcription jobs keyed by mp3 filename.
# state: queued|running|done|error
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _transcript_outputs(stem: str) -> list[str]:
    """Names of whisperx output files for a given input stem."""
    return sorted(p.name for p in TRANSCRIPT_DIR.glob(stem + ".*"))


def _prune_jobs() -> None:
    """Cap the finished-jobs dict so it can't grow without bound."""
    with _jobs_lock:
        finished = [k for k, v in _jobs.items() if v["state"] in ("done", "error")]
        # keep the 50 most recent finished jobs (dict preserves insertion order)
        for k in finished[:-50]:
            _jobs.pop(k, None)


def _run_transcription(mp3_name: str) -> None:
    mp3_path = OUTPUT_DIR / mp3_name
    stem = mp3_path.stem
    cmd = [
        WHISPER["bin"], str(mp3_path),
        "--output_dir", str(TRANSCRIPT_DIR),
        "--model", WHISPER["model"],
        "--language", WHISPER["language"],
        "--no_align", "--diarize",
        "--device", WHISPER["device"],
        "--compute_type", WHISPER["compute_type"],
    ]
    with _jobs_lock:
        _jobs[mp3_name] = {"state": "running", "error": None, "outputs": []}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
            raise RuntimeError("\n".join(tail) or f"whisperx exit {proc.returncode}")
        outs = _transcript_outputs(stem)
        with _jobs_lock:
            _jobs[mp3_name] = {"state": "done", "error": None, "outputs": outs}
    except FileNotFoundError:
        with _jobs_lock:
            _jobs[mp3_name] = {
                "state": "error", "outputs": [],
                "error": f"Не найден '{WHISPER['bin']}'. Установите whisperx или задайте WHISPER_BIN.",
            }
    except Exception as exc:  # noqa: BLE001
        with _jobs_lock:
            _jobs[mp3_name] = {"state": "error", "error": str(exc), "outputs": []}


@app.get("/version")
def version():
    return {"version": VERSION, "name": "Jitsi Recorder"}


@app.get("/", response_class=HTMLResponse)
def index():
    # Token is entered in the browser and only sent from here — never baked in.
    html = INDEX_HTML.replace("__TOKEN_REQUIRED__", "true" if TOKEN else "false")
    html = html.replace("__VERSION__", VERSION)
    return HTMLResponse(html)


def auth(x_token: str | None = Header(default=None)) -> None:
    if TOKEN and x_token != TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


class StartRequest(BaseModel):
    url: str
    name: str = "Recorder"


class StopRequest(BaseModel):
    id: str


def _prune_sessions() -> None:
    """Drop finished sessions once there are many, keeping recent ones."""
    with _sessions_lock:
        finished = [s for s in _sessions.values()
                    if s.state in ("done", "error")]
        if len(finished) > 20:
            finished.sort(key=lambda s: s.stopped_at or dt.datetime.min)
            for s in finished[:-20]:
                _sessions.pop(s.id, None)


@app.post("/start", dependencies=[Depends(auth)])
def start(req: StartRequest):
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must be http(s)")
    sid = uuid.uuid4().hex[:8]
    sess = RecorderSession(req.url, req.name, sid=sid)
    with _sessions_lock:
        _sessions[sid] = sess
    sess.start()
    _prune_sessions()
    return {"ok": True, "id": sid, "status": sess.status()}


@app.post("/stop", dependencies=[Depends(auth)])
def stop(req: StopRequest):
    with _sessions_lock:
        sess = _sessions.get(req.id)
    if not sess:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    if sess.state not in ("starting", "recording"):
        raise HTTPException(status_code=409, detail="Сессия не активна")
    sess.request_stop()
    return {"ok": True, "status": sess.status()}


@app.get("/sessions", dependencies=[Depends(auth)])
def sessions():
    with _sessions_lock:
        items = [s.status() for s in _sessions.values()]
    # Newest first: active before finished.
    order = {"recording": 0, "starting": 0, "stopping": 1, "done": 2, "error": 2}
    items.sort(key=lambda s: (order.get(s["state"], 3), -(s["duration_sec"] or 0)))
    return items


@app.get("/recordings", dependencies=[Depends(auth)])
def recordings():
    files = sorted(OUTPUT_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"filename": p.name, "size": p.stat().st_size} for p in files]


@app.get("/download/{filename}", dependencies=[Depends(auth)])
def download(filename: str):
    safe = Path(filename).name
    path = OUTPUT_DIR / safe
    if not path.exists() or path.suffix != ".mp3":
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="audio/mpeg", filename=safe)


@app.delete("/recordings/{filename}", dependencies=[Depends(auth)])
def delete_recording(filename: str):
    safe = Path(filename).name
    path = OUTPUT_DIR / safe
    if not safe.endswith(".mp3") or not path.exists():
        raise HTTPException(status_code=404, detail="Запись не найдена")
    with _jobs_lock:
        job = _jobs.get(safe)
        if job and job["state"] in ("queued", "running"):
            raise HTTPException(status_code=409, detail="Идёт транскрибация — сначала дождитесь её завершения")
    # Remove the mp3 and any transcript files derived from it.
    removed = []
    try:
        path.unlink()
        removed.append(safe)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Не удалось удалить: {exc}")
    for tf in TRANSCRIPT_DIR.glob(path.stem + ".*"):
        try:
            tf.unlink()
            removed.append(tf.name)
        except OSError:
            pass
    with _jobs_lock:
        _jobs.pop(safe, None)
    return {"ok": True, "removed": removed}


class TranscribeRequest(BaseModel):
    filename: str


@app.post("/transcribe", dependencies=[Depends(auth)])
def transcribe(req: TranscribeRequest):
    safe = Path(req.filename).name
    if not (OUTPUT_DIR / safe).exists() or not safe.endswith(".mp3"):
        raise HTTPException(status_code=404, detail="Запись не найдена")
    with _jobs_lock:
        job = _jobs.get(safe)
        if job and job["state"] in ("queued", "running"):
            raise HTTPException(status_code=409, detail="Транскрибация уже идёт")
        _jobs[safe] = {"state": "queued", "error": None, "outputs": []}
    threading.Thread(target=_run_transcription, args=(safe,), daemon=True).start()
    _prune_jobs()
    return {"ok": True}


@app.get("/transcribe/status", dependencies=[Depends(auth)])
def transcribe_status():
    # Merge live job state with transcripts already present on disk.
    result: dict[str, dict] = {}
    with _jobs_lock:
        result.update({k: dict(v) for k, v in _jobs.items()})
    for mp3 in OUTPUT_DIR.glob("*.mp3"):
        outs = _transcript_outputs(mp3.stem)
        if outs and mp3.name not in result:
            result[mp3.name] = {"state": "done", "error": None, "outputs": outs}
    return result


@app.get("/transcripts/{filename}", dependencies=[Depends(auth)])
def download_transcript(filename: str):
    safe = Path(filename).name
    path = TRANSCRIPT_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="application/octet-stream", filename=safe)


INDEX_HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jitsi Recorder</title>
<style>
  :root{--bg:#0f1420;--card:#1a2130;--fg:#e7ecf3;--mut:#8b96a8;--acc:#3b82f6;
        --red:#ef4444;--green:#22c55e;--line:#2a3348}
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
  .wrap{max-width:640px;margin:0 auto;padding:24px 16px}
  h1{font-size:20px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
  .sub{color:var(--mut);font-size:13px;margin-bottom:20px}
  .ver{font-size:12px;font-weight:600;color:var(--mut);background:#0d1220;
       border:1px solid var(--line);border-radius:6px;padding:2px 7px;margin-left:4px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
  label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
  input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--line);
        background:#0d1220;color:var(--fg);font-size:14px}
  input:focus{outline:none;border-color:var(--acc)}
  .row{display:flex;gap:16px}.row>div{flex:1}
  .btns{display:flex;gap:10px;margin-top:16px}
  button{flex:1;padding:11px;border:0;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;color:#fff}
  button:disabled{opacity:.45;cursor:not-allowed}
  .start{background:var(--acc)}.stop{background:var(--red)}
  .status{display:flex;align-items:center;gap:10px;font-size:14px}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--mut)}
  .dot.rec{background:var(--red);animation:p 1s infinite}
  .dot.done{background:var(--green)}
  @keyframes p{50%{opacity:.3}}
  .meta{color:var(--mut);font-size:13px;margin-top:6px}
  .rec-list a{color:var(--acc);text-decoration:none}
  .rec-item{display:flex;justify-content:space-between;align-items:center;gap:10px;
            padding:10px 0;border-top:1px solid var(--line);font-size:14px}
  .rec-main{min-width:0}.rec-main a{word-break:break-all}
  .rec-size{margin-left:10px}
  .rec-actions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;flex-shrink:0}
  .tbtn{font-size:12px;padding:5px 9px;border-radius:6px;background:#0d1220;
        border:1px solid var(--line);color:var(--fg);text-decoration:none;white-space:nowrap;cursor:pointer}
  .tbtn.ok{border-color:#1f6f3f;color:#4ade80}
  .tbtn.wait{color:var(--mut);cursor:default}
  .tbtn.err2{border-color:#7f1d1d;color:#f87171}
  .tbtn.del{border-color:#7f1d1d;color:#f87171}
  .tbtn.del:hover{background:#7f1d1d;color:#fff}
  .err{color:var(--red);font-size:13px;margin-top:8px;white-space:pre-wrap}
  .meter{height:10px;background:#0d1220;border:1px solid var(--line);border-radius:6px;
         overflow:hidden;margin:12px 0 4px;display:none}
  .meter.on{display:block}
  .sess{padding:12px 0;border-top:1px solid var(--line)}
  .sess:first-child{border-top:0}
  .sess-top{display:flex;justify-content:space-between;align-items:center;gap:12px}
  .sess-name{display:flex;align-items:center;gap:8px;min-width:0;flex:1 1 auto}
  .sess-name b{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0}
  .sess-name .meta{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sbtn{font-size:13px;font-weight:600;padding:8px 16px;border-radius:6px;border:0;color:#fff;
        background:var(--red);cursor:pointer;flex:0 0 auto;width:104px;text-align:center}
  .sbtn:disabled{opacity:.45;cursor:default}
  .meter-fill{height:100%;width:0%;border-radius:6px;
              background:linear-gradient(90deg,#22c55e 0%,#eab308 70%,#ef4444 100%);
              transition:width .12s linear}
  .hide{display:none}
</style></head>
<body><div class="wrap">
  <h1>🎙️ Jitsi Recorder <span class="ver">v__VERSION__</span></h1>
  <div class="sub">Запись аудио звонка Jitsi Meet в MP3</div>

  <div class="card">
    <label>Ссылка на комнату</label>
    <input id="url" placeholder="https://your-jitsi-server/МояКомната" autocomplete="off">
    <div class="row">
      <div><label>Имя бота</label><input id="name" value="Recorder"></div>
      <div id="tokWrap" class="hide"><label>Токен</label><input id="token" type="password"></div>
    </div>
    <div class="btns">
      <button class="start" id="startBtn" onclick="start()">▶ Начать запись</button>
    </div>
    <div class="err" id="err"></div>
  </div>

  <div class="card">
    <b style="font-size:14px">Активные записи <span class="ver" id="activeCount">0</span></b>
    <div id="sessions"><div class="meta">нет активных записей</div></div>
  </div>

  <div class="card">
    <b style="font-size:14px">Записи</b>
    <div class="rec-list" id="recs"><div class="meta">пока пусто</div></div>
  </div>
</div>
<script>
const NEED_TOKEN = __TOKEN_REQUIRED__;
if(NEED_TOKEN) document.getElementById('tokWrap').classList.remove('hide');
const $=id=>document.getElementById(id);
function hdr(){const h={'Content-Type':'application/json'};if(NEED_TOKEN)h['X-Token']=$('token').value;return h}
async function api(path,method='GET',body){
  const o={method,headers:hdr()};if(body)o.body=JSON.stringify(body);
  const r=await fetch(path,o);const t=await r.text();
  let d={};try{d=JSON.parse(t)}catch(e){}
  if(!r.ok)throw new Error(d.detail||t||('HTTP '+r.status));return d;
}
const SMAP={starting:'Подключение…',recording:'Идёт запись',
            stopping:'Остановка…',done:'Готово',error:'Ошибка'};
async function start(){
  $('err').textContent='';
  const url=$('url').value.trim();
  if(!url){$('err').textContent='Укажите ссылку на комнату';return}
  localStorage.setItem('lastUrl',url);
  try{await api('/start','POST',{url,name:$('name').value||'Recorder'})}
  catch(e){$('err').textContent=e.message}
  poll();
}
async function stopSession(id){
  $('err').textContent='';
  try{await api('/stop','POST',{id})}catch(e){$('err').textContent=e.message}
  poll();
}
async function poll(){
  let list;try{list=await api('/sessions')}catch(e){
    $('sessions').innerHTML='<div class="meta">нет связи с сервером</div>';return}
  const active=list.filter(s=>['starting','recording','stopping'].includes(s.state));
  $('activeCount').textContent=active.length;
  const box=$('sessions');
  if(!list.length){box.innerHTML='<div class="meta">нет активных записей</div>';}
  else{
    box.innerHTML=list.map(s=>{
      const st=s.state, on=(st==='recording');
      const dot='<span class="dot'+(on?' rec':st==='done'?' done':'')+'"></span>';
      const canStop=['starting','recording'].includes(st);
      const btn=canStop
        ? `<button class="sbtn" onclick="stopSession('${s.id}')">■ Стоп</button>`
        : (st==='stopping'?'<button class="sbtn" disabled>…</button>':'');
      let meta=[];if(s.duration_sec!=null)meta.push(s.duration_sec+' сек');
      if(s.bytes_captured)meta.push((s.bytes_captured/1024|0)+' КБ');
      if(st==='error'&&s.error)meta.push('⚠ '+s.error);
      const meter=on?`<div class="meter on"><div class="meter-fill" id="m_${s.id}" style="width:${s.level||0}%"></div></div>`:'';
      return `<div class="sess"><div class="sess-top">`
        +`<div class="sess-name">${dot}<b>${s.name||s.room}</b>`
        +`<span class="meta">${SMAP[st]||st} · ${s.room}</span></div>${btn}</div>`
        +meter
        +(meta.length?`<div class="meta">${meta.join('  •  ')}</div>`:'')
        +`</div>`;
    }).join('');
  }
  loadRecs();
}
async function loadRecs(){
  let list,jobs;
  try{list=await api('/recordings')}catch(e){return}
  try{jobs=await api('/transcribe/status')}catch(e){jobs={}}
  const box=$('recs');
  if(!list.length){box.innerHTML='<div class="meta">пока пусто</div>';return}
  box.innerHTML=list.map(f=>{
    const j=jobs[f.filename];
    let tr='';
    if(j&&j.state==='running'){
      tr=`<span class="tbtn wait">⏳ транскрибация…</span>`;
    }else if(j&&j.state==='queued'){
      tr=`<span class="tbtn wait">в очереди…</span>`;
    }else if(j&&j.state==='done'&&j.outputs.length){
      tr=j.outputs.map(o=>`<a class="tbtn ok" href="#" onclick="dlT('${o}');return false">⬇ ${o.split('.').pop()}</a>`).join('');
    }else if(j&&j.state==='error'){
      tr=`<a class="tbtn err2" href="#" onclick="transcribe('${f.filename}');return false" title="${(j.error||'').replace(/"/g,'')}">⚠ повторить</a>`;
    }else{
      tr=`<a class="tbtn" href="#" onclick="transcribe('${f.filename}');return false" title="транскрибировать">📝</a>`;
    }
    const del=`<a class="tbtn del" href="#" onclick="delRec('${f.filename}');return false" title="Удалить запись">🗑</a>`;
    return `<div class="rec-item"><div class="rec-main">`
      +`<a href="#" onclick="dl('${f.filename}');return false">${f.filename}</a>`
      +`<span class="meta rec-size">${(f.size/1024|0)} КБ</span></div>`
      +`<div class="rec-actions">${tr}${del}</div></div>`;
  }).join('');
}
async function delRec(fn){
  if(!confirm('Удалить запись и её транскрипты?\n'+fn))return;
  try{await api('/recordings/'+encodeURIComponent(fn),'DELETE')}
  catch(e){alert(e.message)}
  loadRecs();
}
async function transcribe(fn){
  try{await api('/transcribe','POST',{filename:fn})}catch(e){alert(e.message)}
  loadRecs();
}
async function blobDl(path,fn){
  const o=NEED_TOKEN?{headers:{'X-Token':$('token').value}}:{};
  const r=await fetch(path,o);const b=await r.blob();const a=document.createElement('a');
  a.href=URL.createObjectURL(b);a.download=fn;a.click();URL.revokeObjectURL(a.href);
}
async function dl(fn){await blobDl('/download/'+encodeURIComponent(fn),fn)}
async function dlT(fn){await blobDl('/transcripts/'+encodeURIComponent(fn),fn)}
async function levelPoll(){
  let list;try{list=await api('/sessions')}catch(e){return}
  list.forEach(s=>{
    const el=$('m_'+s.id);
    if(el)el.style.width=(s.level||0)+'%';
  });
}
$('url').value=localStorage.getItem('lastUrl')||'';
poll();setInterval(poll,2500);setInterval(levelPoll,300);
</script>
</body></html>"""


if __name__ == "__main__":
    import socket

    import uvicorn

    # Bind to all interfaces by default so the panel is reachable on the LAN
    # (e.g. http://10.10.20.20:9999). Override with RECORDER_HOST/PORT.
    host = os.environ.get("RECORDER_HOST", "0.0.0.0")
    port = int(os.environ.get("RECORDER_PORT", "9999"))

    # Best-effort LAN address for the startup banner.
    lan_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:  # noqa: BLE001
        pass

    print("=" * 52)
    print(f"  Jitsi Recorder v{VERSION} запущен")
    print(f"  Панель:   http://{lan_ip}:{port}")
    print(f"  Локально: http://127.0.0.1:{port}")
    print("  Токен:   ", "задан (X-Token)" if TOKEN else "НЕ задан (открытый доступ!)")
    print("=" * 52)

    # access_log=False: the panel polls a few times per second, so per-request
    # logging would flood the console endlessly. Errors/warnings still print.
    uvicorn.run(app, host=host, port=port, access_log=False)
