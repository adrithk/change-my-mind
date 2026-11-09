"""Local, read-only run inspector. It never initializes jobs or model providers."""

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import Settings
from .store import Store


def create_developer_app(settings=None):
    settings = settings or Settings()
    store = Store(settings.data_dir)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await store.close()

    app = FastAPI(title="Change My Mind developer trace", lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host", "").split(":")[0] not in {"127.0.0.1", "localhost", "testserver"}:
            return Response("Local access only", status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return Response("Cross-origin access denied", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"detail": "Run not found"}, status_code=404)

    @app.get("/api/runs")
    async def runs():
        return [{"id": r.id, "question": r.question, "status": r.status} for r in await store.list_runs()]

    @app.get("/api/runs/{run_id}/trace")
    async def trace(run_id: str):
        run = await store.get_run(run_id, records=False)
        # Human developer access is separate from all agent/observer retrieval paths.
        records = await store.records(
            run_id,
            audience="controller",
            kinds=[
                "invocation",
                "research_plan",
                "research_agenda",
                "briefing",
                "submission",
                "claim",
                "argument",
                "objection",
                "response",
                "check",
                "investigation",
                "judgment",
                "finding",
                "recommendation",
                "limitation",
                "consistency",
            ],
        )
        return {"run": run, "calls": await store.usage(run_id), "records": records}

    @app.get("/", response_class=HTMLResponse)
    async def page():
        return PAGE

    return app


PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Developer trace · Change My Mind</title>
<style>
:root{color-scheme:dark;font:14px/1.6 system-ui;background:#080f20;color:#e9eef9}body{max-width:1100px;margin:36px auto;padding:0 24px}h1{font-size:25px}h2{font-size:19px}p{color:#a9b7ce}a{color:#c6b6f1}select,button,input{font:inherit;padding:8px 12px;border:1px solid #34445f;background:#111f38;color:inherit;border-radius:5px}select{max-width:100%}nav{display:flex;gap:12px;flex-wrap:wrap;align-items:center}input{min-width:240px;flex:1}details{border-top:1px solid #293650;padding:14px 0}summary{cursor:pointer;overflow-wrap:anywhere}.badge{color:#c6b6f1;margin-right:12px}.error{color:#f3a7b6}.muted{color:#98abc6;font-size:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 ui-monospace,monospace;background:#101b31;padding:16px;border-radius:5px}.controls{margin:24px 0}#question{overflow-wrap:anywhere}#notice{color:#f3a7b6}#status{padding:16px 0;border-bottom:1px solid #293650}
</style><h1>Developer trace</h1>
<p>Live call status and submitted outputs, including private drafts. This page never feeds developer-visible records to opponents. Output appears when a call completes; private chain-of-thought is not available.</p>
<nav><select id="runs" aria-label="Select debate"></select><a id="app" href="http://127.0.0.1:8000/" target="_blank" rel="noreferrer">Open debate ↗</a><span class="muted">Refreshes every 2 seconds · read only</span></nav>
<h2 id="question"></h2><div id="status">Loading…</div><p id="notice" role="alert"></p>
<nav class="controls"><input id="filter" aria-label="Filter trace" placeholder="Filter by role, search query, or text"><button id="expand">Expand outputs</button><button id="collapse">Collapse</button></nav>
<h2>Model calls · newest first</h2><div id="calls"></div>
<details id="artifacts"><summary>Registered arguments, objections, corrections & findings</summary><div id="records"></div></details>
<script>
const el=id=>document.getElementById(id);let snapshot=null,loading=false;const openRows=new Set();
function details(key,label,value){const row=document.createElement('details');row.dataset.key=key;row.open=openRows.has(key);const summary=document.createElement('summary');summary.textContent=label;row.append(summary);const text=document.createElement('pre');text.textContent=typeof value==='string'?value:JSON.stringify(value,null,2);row.append(text);return row}
function remember(){for(const row of document.querySelectorAll('details[data-key]')){if(row.open)openRows.add(row.dataset.key);else openRows.delete(row.dataset.key)}}
function render(){if(!snapshot)return;remember();const {run,calls,records}=snapshot;el('question').textContent=run.question;const pending=calls.filter(c=>c.state==='reserved').length;const complete=calls.filter(c=>c.state==='settled'&&!c.details.error).length;el('status').textContent=run.status+' · '+run.stage+' · '+pending+' calls in progress · '+complete+' completed · '+calls.length+' attempts';el('notice').textContent=run.error||'';el('app').href='http://127.0.0.1:8000/#'+encodeURIComponent(run.id);const filter=el('filter').value.toLowerCase();const outputs=new Map(records.filter(r=>r.kind==='invocation').map(r=>[r.key,r]));const fragment=document.createDocumentFragment();for(const call of [...calls].reverse()){const meta=call.details||{};const key=call.operation.split(':attempt:')[0];const record=outputs.get(key);const output=record&&meta.response_id===record.data.response_id?record.data:null;const entry={...(output?{submitted_output:output.output,query:output.query}:{message:meta.error||'No completed output yet.'}),operation:call.operation,team:call.team,status:call.state,usage:meta,...(output?{prompt_version:output.prompt_version,input_record_ids:output.input_record_ids}:{})};if(filter&&!JSON.stringify(entry).toLowerCase().includes(filter))continue;const state=meta.error?'error':call.state==='reserved'?'RUNNING':call.state;const label=[state,call.team?'Team '+call.team:'neutral',meta.role,meta.model,meta.reasoning,call.operation].filter(Boolean).join(' · ');fragment.append(details('call:'+call.operation,label,entry))}el('calls').replaceChildren(fragment);const artifacts=document.createDocumentFragment();for(const r of [...records].reverse()){if(r.kind==='invocation')continue;if(filter&&!JSON.stringify(r).toLowerCase().includes(filter))continue;artifacts.append(details('record:'+r.id,[r.kind,r.team?'Team '+r.team:'neutral',r.stage,r.visibility,r.stale?'STALE':'',r.superseded?'SUPERSEDED':'',r.id].filter(Boolean).join(' · '),r.data))}el('records').replaceChildren(artifacts)}
async function refresh(){if(loading)return;const id=location.hash.slice(1);if(!id){el('status').textContent='Choose a debate above.';return}loading=true;try{const response=await fetch('/api/runs/'+encodeURIComponent(id)+'/trace');if(!response.ok)throw new Error('Trace unavailable (HTTP '+response.status+')');snapshot=await response.json();render()}catch(error){el('notice').textContent=error.message}finally{loading=false}}
async function start(){try{const response=await fetch('/api/runs');if(!response.ok)throw new Error('Could not list debates');const runs=await response.json();for(const run of runs){const option=document.createElement('option');option.value=run.id;option.textContent=run.question+' · '+run.status;el('runs').append(option)}if(!location.hash&&runs.length)location.hash=runs[0].id;el('runs').value=location.hash.slice(1);await refresh()}catch(error){el('notice').textContent=error.message}}
el('runs').onchange=()=>{location.hash=el('runs').value;snapshot=null;openRows.clear();refresh()};window.onhashchange=()=>{el('runs').value=location.hash.slice(1);refresh()};el('filter').oninput=render;el('expand').onclick=()=>document.querySelectorAll('#calls details').forEach(row=>row.open=true);el('collapse').onclick=()=>document.querySelectorAll('details[data-key]').forEach(row=>row.open=false);start();setInterval(refresh,2000);
</script></html>"""


def serve(port=8002):
    uvicorn.run(create_developer_app(), host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    serve()
