from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    import websocket
except ImportError as exc:  # pragma: no cover
    raise SystemExit("browser smoke test requires websocket-client") from exc

ROOT = Path(__file__).resolve().parents[1]


class CDP:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=10, origin="http://localhost")
        self.next_id = 1
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None):
        request_id = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})
            self.events.append(message)

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def close(self):
        self.ws.close()


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


def wait_for(predicate, timeout=10):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for browser condition: {last_error}")


def mock_transport_script() -> str:
    return r'''
    (() => {
      try { void window.localStorage; } catch (_) {
        const memory = new Map();
        Object.defineProperty(window, 'localStorage', {value: {
          getItem: key => memory.has(key) ? memory.get(key) : null,
          setItem: (key, value) => memory.set(key, String(value)),
          removeItem: key => memory.delete(key),
          clear: () => memory.clear()
        }});
      }
      const records = [
        {id:'alpha-id',name:'Alpha',description:'First isolated repository',path:'/repos/alpha',metadataMode:'embedded',metadataPath:'',defaultAuthor:'Rooke Poole',uploadLimitBytes:262144000,favorite:true,tags:[],collections:[],collection:'',capabilities:{exists:true,directory:true,readable:true,writable:true,metadataWritable:true,freeBytes:1000000000,pathKind:'local-or-mounted'},createdAt:'2026-07-24T20:00:00Z',updatedAt:'2026-07-24T20:00:00Z',lastOpenedAt:'2026-07-24T20:00:00Z',status:'online',statusMessage:'',active:true},
        {id:'beta-id',name:'Beta',description:'Second isolated repository',path:'/other-drive/beta',metadataMode:'embedded',metadataPath:'',defaultAuthor:'Rooke Poole',uploadLimitBytes:262144000,favorite:false,tags:[],collections:[],collection:'',capabilities:{exists:true,directory:true,readable:true,writable:true,metadataWritable:true,freeBytes:1000000000,pathKind:'local-or-mounted'},createdAt:'2026-07-24T20:01:00Z',updatedAt:'2026-07-24T20:01:00Z',lastOpenedAt:'2026-07-24T20:01:00Z',status:'online',statusMessage:'',active:false}
      ];
      let active = 'alpha-id';
      let sharing = {enabled:false,mode:'quarantined-pull-requests',bindHost:'0.0.0.0',port:null,addresses:['192.168.1.50'],baseUrls:[],publicBaseUrl:'',startedAt:''};
      const invites = [];
      const library = {collections:[{id:'work-id',name:'Work',description:'Active work',color:'',repositoryCount:0,created_at:'2026-07-24T20:00:00Z',updated_at:'2026-07-24T20:00:00Z'}],tags:[],savedFilters:[]};
      const state = {
        'alpha-id': {summary:{initialized:true,id:'alpha-id',path:'/repos/alpha',repository:{id:'alpha-id',name:'Alpha',description:'First isolated repository',defaultAuthor:'Rooke Poole'},stats:{files:2,folders:0,bytes:28,commits:0,contributions:2,contributors:1,dirtyFiles:2},dirty:{added:['README.md','alpha.txt'],modified:[],deleted:[]},contributors:[],latestCommit:null},tree:[{path:'README.md',name:'README.md',type:'file',size:15,mime:'text/markdown',text:true,modified:'2026-07-24T20:00:00Z'},{path:'alpha.txt',name:'alpha.txt',type:'file',size:13,mime:'text/plain',text:true,modified:'2026-07-24T20:02:00Z'}],contributions:[{id:'c2',action:'file_uploaded',title:'Uploaded alpha.txt',description:'Uploaded alpha.txt into Alpha.',author:'Rooke Poole',path:'alpha.txt',timestamp:'2026-07-24T20:02:00Z'},{id:'c1',action:'repository_created',title:'Created repository',description:'Initialized Alpha.',author:'Rooke Poole',path:'README.md',timestamp:'2026-07-24T20:00:00Z'}],commits:[]},
        'beta-id': {summary:{initialized:true,id:'beta-id',path:'/other-drive/beta',repository:{id:'beta-id',name:'Beta',description:'Second isolated repository',defaultAuthor:'Rooke Poole'},stats:{files:1,folders:0,bytes:12,commits:1,contributions:2,contributors:1,dirtyFiles:0},dirty:{added:[],modified:[],deleted:[]},contributors:[],latestCommit:{id:'b123',message:'Beta baseline'}},tree:[{path:'README.md',name:'README.md',type:'file',size:12,mime:'text/markdown',text:true,modified:'2026-07-24T20:01:00Z'}],contributions:[{id:'b2',action:'commit_created',title:'Created repository snapshot',description:'Beta baseline',author:'Rooke Poole',timestamp:'2026-07-24T20:03:00Z'},{id:'b1',action:'repository_created',title:'Created repository',description:'Initialized Beta.',author:'Rooke Poole',path:'README.md',timestamp:'2026-07-24T20:01:00Z'}],commits:[{id:'b123',parent:null,message:'Beta baseline',author:'Rooke Poole',timestamp:'2026-07-24T20:03:00Z',changes:{added:['README.md'],modified:[],deleted:[]},fileCount:1,totalBytes:12}]}
      };
      const contents = {'alpha-id':{'README.md':'# Alpha\n','alpha.txt':'alpha content'},'beta-id':{'README.md':'# Beta\n'}};
      const pullRequest = {id:'pr-browser',repositoryId:'alpha-id',number:7,title:'Add secure contribution notes',description:'Documents the quarantine boundary and adds a contributor checklist.',authorName:'Outside Contributor',status:'open',effectiveStatus:'open',baseCommitId:'base7aa92',revision:3,createdAt:'2026-07-24T20:10:00Z',updatedAt:'2026-07-24T20:15:00Z',submittedAt:'2026-07-24T20:15:00Z',mergedAt:'',mergedBy:'',mergeCommitId:'',closedAt:'',changeCount:2,fileCount:1,deletionCount:1,totalBytes:62,riskyFileCount:0,reviews:[],conflicts:[],files:[{path:'SECURITY_NOTES.md',size:62,sha256:'29b4f04b88a4f3093dbadd1341dbea7d9831e0cf7bdc0cffaba5e59c49218aa0',baseHash:'',risky:false,diff:'--- /dev/null\n+++ b/SECURITY_NOTES.md\n@@ -0,0 +1,3 @@\n+# Collaboration\n+Review every quarantined change.\n+Merge locally.\n',diffTruncated:false}],deletions:[{path:'obsolete.txt',baseHash:'118c',createdAt:'2026-07-24T20:14:00Z'}]};
      const response = (payload,status=200) => Promise.resolve(new Response(JSON.stringify(payload),{status,headers:{'Content-Type':'application/json'}}));
      window.fetch = async (input, init={}) => {
        const url = new URL(typeof input === 'string' ? input : input.url, 'http://forgetrace.local');
        const method = (init.method || 'GET').toUpperCase();
        if (url.pathname === '/api/v1/version') return response({name:'ForgeTrace',version:'0.4.2',applicationSchemaVersion:3});
        if (url.pathname === '/api/v1/sharing' && method === 'GET') return response(sharing);
        if (url.pathname === '/api/v1/sharing/start' && method === 'POST') { const body=JSON.parse(init.body||'{}'); const port=Number(body.port||8766); sharing={...sharing,enabled:true,port,baseUrls:[`http://192.168.1.50:${port}`],publicBaseUrl:`http://192.168.1.50:${port}`,startedAt:'2026-07-24T21:00:00Z'}; return response(sharing); }
        if (url.pathname === '/api/v1/sharing/stop' && method === 'POST') { sharing={...sharing,enabled:false,port:null,baseUrls:[],publicBaseUrl:'',startedAt:''}; return response(sharing); }
        if (url.pathname === '/api/v1/repositories' && method === 'GET') {
          records.forEach(r => r.active = r.id === active);
          return response({activeRepositoryId:active,repositories:records});
        }
        if (url.pathname === '/api/v1/repositories/managed' && method === 'POST') {
          const body=JSON.parse(init.body||'{}'); const id=`managed-${records.length+1}`; const name=body.name||'Imported repository';
          const record={id,name,description:body.description||'',path:`/app-data/managed-repositories/${name.replace(/[^a-z0-9]+/gi,'-')}`,metadataMode:'embedded',metadataPath:'',defaultAuthor:body.author||'Rooke Poole',uploadLimitBytes:262144000,favorite:false,tags:[],collections:[],collection:'',capabilities:{exists:true,directory:true,readable:true,writable:true,metadataWritable:true,freeBytes:1000000000,pathKind:'local-or-mounted'},createdAt:'2026-07-25T16:00:00Z',updatedAt:'2026-07-25T16:00:00Z',lastOpenedAt:'2026-07-25T16:00:00Z',status:'online',statusMessage:'',active:true};
          records.push(record); active=id; contents[id]={'README.md':`# ${name}\n`}; state[id]={summary:{initialized:true,id,path:record.path,repository:{id,name,description:record.description,defaultAuthor:record.defaultAuthor},stats:{files:1,folders:0,bytes:name.length+3,commits:0,contributions:1,contributors:1,dirtyFiles:1},dirty:{added:['README.md'],modified:[],deleted:[]},contributors:[],latestCommit:null},tree:[{path:'README.md',name:'README.md',type:'file',size:name.length+3,mime:'text/markdown',text:true,modified:'2026-07-25T16:00:00Z'}],contributions:[{id:`created-${id}`,action:'repository_created',title:'Created repository',description:`Initialized ${name}.`,author:record.defaultAuthor,path:'README.md',timestamp:'2026-07-25T16:00:00Z'}],commits:[]};
          return response(record,201);
        }
        if (url.pathname === '/api/v1/library' && method === 'GET') return response(library);
        if (url.pathname === '/api/v1/registry/backups' && method === 'GET') return response({backups:[{name:'registry-20260725T160000Z-ui-1234abcd.sqlite3',path:'/backups/registry-20260725T160000Z-ui-1234abcd.sqlite3',bytes:4096,modified:1785000000}]});
        if (url.pathname === '/api/v1/registry/restores' && method === 'GET') return response({restores:[]});
        if (url.pathname === '/api/v1/registry/backup' && method === 'POST') return response({name:'registry-ui.sqlite3',path:'/backups/registry-ui.sqlite3'},201);
        if (url.pathname === '/api/v1/registry/export' && method === 'GET') return response({format:'forgetrace-registry-export',version:1,repositories:records,collections:library.collections,savedFilters:[]});
        if (url.pathname === '/api/v1/doctor' && method === 'POST') return response({healthy:true,integrity:'ok',repositoryCount:2,issues:[],actions:[],summary:{critical:0,errors:0,warnings:0,total:0},backup:null});
        if (url.pathname === '/api/v1/active-repository' && method === 'POST') {
          active = JSON.parse(init.body).repositoryId;
          return response(records.find(r => r.id === active));
        }
        const prMatch = url.pathname.match(/^\/api\/v1\/repositories\/([^/]+)\/pull-requests(?:\/([^/]+))?(?:\/(review|merge|close))?$/);
        if (prMatch && method === 'GET' && !prMatch[2]) return response({pullRequests:prMatch[1]==='alpha-id'?[pullRequest]:[]});
        if (prMatch && method === 'GET' && prMatch[2]) return response(pullRequest);
        const inviteMatch = url.pathname.match(/^\/api\/v1\/repositories\/([^/]+)\/collaboration\/invites(?:\/([^/]+))?$/);
        if (inviteMatch && method === 'GET' && !inviteMatch[2]) return response({invites});
        if (inviteMatch && method === 'POST' && !inviteMatch[2]) { const body=JSON.parse(init.body||'{}'); const invite={id:'invite-browser',label:body.label||'Browser invite',active:true,revoked:false,expired:false,uses:0,maxUses:body.maxUses||1,expiresAt:'2026-07-27T21:00:00Z',maxFileBytes:body.maxFileBytes||26214400,allowSourceDownload:body.allowSourceDownload!==false}; invites.unshift(invite); return response({invite,token:'browser-secret-token',sharePath:'/contribute.html#browser-secret-token'},201); }
        const settingsMatch = url.pathname.match(/^\/api\/v1\/repositories\/([^/]+)\/(settings|organization)$/);
        if (settingsMatch && method === 'POST') {
          const body=JSON.parse(init.body); const record=records.find(r=>r.id===settingsMatch[1]);
          if(settingsMatch[2]==='settings'){record.name=body.name;record.description=body.description;record.defaultAuthor=body.defaultAuthor;record.uploadLimitBytes=body.uploadLimitBytes;state[record.id].summary.repository.name=body.name;state[record.id].summary.repository.description=body.description;state[record.id].summary.repository.defaultAuthor=body.defaultAuthor;}
          else {record.tags=body.tags;record.collections=library.collections.filter(item=>body.collectionIds.includes(item.id));library.tags=body.tags.map(tag=>({tag,repositoryCount:1}));library.collections[0].repositoryCount=record.collections.length?1:0;}
          return response(record);
        }
        const uploadMatch = url.pathname.match(/^\/api\/v1\/repositories\/([^/]+)\/upload$/);
        if(uploadMatch&&method==='POST'){
          const id=uploadMatch[1]; const path=url.searchParams.get('path'); const content=init.body&&typeof init.body.text==='function'?await init.body.text():String(init.body||''); contents[id][path]=content;
          const item={path,name:path.split('/').pop(),type:'file',size:content.length,mime:'text/plain',text:true,modified:'2026-07-25T16:01:00Z'}; const existing=state[id].tree.findIndex(entry=>entry.path===path); if(existing>=0)state[id].tree[existing]=item;else state[id].tree.push(item);
          const parts=path.split('/'); for(let index=1;index<parts.length;index++){const folderPath=parts.slice(0,index).join('/');if(!state[id].tree.some(entry=>entry.path===folderPath))state[id].tree.push({path:folderPath,name:parts[index-1],type:'folder',modified:'2026-07-25T16:01:00Z'});}
          state[id].tree.sort((a,b)=>a.path.localeCompare(b.path)); const folders=state[id].tree.filter(entry=>entry.type==='folder'); state[id].summary.stats.files=state[id].tree.filter(entry=>entry.type==='file').length; state[id].summary.stats.folders=folders.length; state[id].summary.stats.bytes=Object.values(contents[id]).reduce((sum,value)=>sum+value.length,0); state[id].summary.stats.contributions+=1; state[id].summary.stats.dirtyFiles=state[id].tree.filter(entry=>entry.type==='file').length; state[id].summary.dirty.added=[...new Set([...state[id].summary.dirty.added,path])];
          state[id].contributions.unshift({id:`upload-${id}-${path}`,action:'file_uploaded',title:`Uploaded ${path}`,description:'Imported from browser.',author:'Rooke Poole',path,timestamp:'2026-07-25T16:01:00Z'}); return response({path,created:existing<0,contribution:{id:`upload-${id}-${path}`}},201);
        }
        const match = url.pathname.match(/^\/api\/v1\/repositories\/([^/]+)\/(state|file)$/);
        if (match && match[2] === 'state') return response(state[match[1]]);
        if (match && match[2] === 'file' && method === 'GET') {
          const path = url.searchParams.get('path'); const content = contents[match[1]][path];
          return response({path,name:path.split('/').pop(),type:'file',size:content.length,mime:'text/plain',text:true,editable:true,content,downloadUrl:'#download',rawUrl:'#raw'});
        }
        if (match && match[2] === 'file' && method === 'PUT') {
          const body = JSON.parse(init.body); contents[match[1]][body.path] = body.content;
          return response({path:body.path,created:false,contribution:{id:'saved'}});
        }
        return response({error:'Mock route not found: '+method+' '+url.pathname},404);
      };
    })();
    '''


def main() -> None:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chromium:
        raise SystemExit("Chromium is required for browser smoke test")
    temp = Path(tempfile.mkdtemp(prefix="forgetrace-browser-"))
    chrome = None
    cdp = None
    try:
        debug_port = 9333
        chrome = subprocess.Popen([
            chromium, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--remote-allow-origins=*", f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={temp / 'chrome'}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        targets = wait_for(lambda: get_json(f"http://127.0.0.1:{debug_port}/json/list"), timeout=15)
        page = next(target for target in targets if target.get("type") == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        cdp.call("Log.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})

        html = (ROOT / "index.html").read_text(encoding="utf-8")
        html = html.replace("<script>", f"<script>{mock_transport_script()}</script><script>", 1)
        frame_id = cdp.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        cdp.call("Page.setDocumentContent", {"frameId": frame_id, "html": html})

        wait_for(lambda: cdp.evaluate("document.readyState === 'complete' && document.querySelector('#repoTitle')?.textContent === 'Alpha'"))
        assert cdp.evaluate("[...document.querySelectorAll('[data-file-path]')].some(x => x.dataset.filePath === 'alpha.txt')")
        cdp.evaluate("document.querySelector('[data-file-path=\"alpha.txt\"]').click()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#code').value === 'alpha content'"))
        cdp.evaluate("document.querySelector('#code').value = 'edited through browser'; document.querySelector('#saveBtn').click()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#code').value === 'edited through browser'"))

        cdp.evaluate("document.querySelector('#organizeBtn').click()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#organizeModalBackdrop').classList.contains('open')"))
        cdp.evaluate("document.querySelector('#tagsInput').value='browser, local'; document.querySelector('#collectionOptions input').checked=true; document.querySelector('#organizeForm').requestSubmit();")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoOrganization').textContent.includes('#browser') && document.querySelector('#repoOrganization').textContent.includes('Work')"))

        cdp.evaluate("document.querySelector('#settingsBtn').click(); document.querySelector('#settingsName').value='Alpha Renamed'; document.querySelector('#settingsUploadLimit').value='12'; document.querySelector('#settingsForm').requestSubmit();")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoTitle').textContent === 'Alpha Renamed'"))

        cdp.evaluate("document.querySelector('#registryToolsBtn').click(); document.querySelector('#doctorCheckBtn').click();")
        wait_for(lambda: cdp.evaluate("document.querySelector('#doctorOutput').textContent.includes('HEALTHY')"))
        cdp.evaluate("document.querySelector('[data-close-modal=\"registryToolsBackdrop\"]').click()")

        cdp.evaluate("document.querySelector('#collaborationBtn').click()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#collaborationModalBackdrop').classList.contains('open') && document.querySelector('#gatewayStatusPill').textContent === 'Off'"))
        cdp.evaluate("document.querySelector('#inviteLabel').value='Browser collaborator'; document.querySelector('#inviteForm').requestSubmit();")
        wait_for(lambda: cdp.evaluate("document.querySelector('#gatewayStatusPill').textContent === 'On' && document.querySelector('#createdInviteLink').value.includes('#browser-secret-token')"))
        cdp.evaluate("document.querySelector('#toastRegion').innerHTML=''")
        screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        (ROOT / "assets" / "preview-unified-sharing.png").write_bytes(base64.b64decode(screenshot["data"]))
        cdp.evaluate("document.querySelector('[data-close-modal=\"collaborationModalBackdrop\"]').click()")

        cdp.evaluate("(() => { const select = document.querySelector('#activeRepoSelect'); select.value='beta-id'; select.dispatchEvent(new Event('change',{bubbles:true})); })()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoTitle')?.textContent === 'Beta'"))
        assert not cdp.evaluate("[...document.querySelectorAll('[data-file-path]')].some(x => x.dataset.filePath === 'alpha.txt')")
        cdp.evaluate("(() => { const select = document.querySelector('#activeRepoSelect'); select.value='alpha-id'; select.dispatchEvent(new Event('change',{bubbles:true})); })()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoTitle')?.textContent === 'Alpha Renamed'"))
        cdp.evaluate("document.querySelector('#toastRegion').innerHTML=''")

        screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        (ROOT / "assets" / "preview-multi-repository.png").write_bytes(base64.b64decode(screenshot["data"]))

        cdp.evaluate("document.querySelector('[data-tab=\"pullrequests\"]').click()")
        wait_for(lambda: cdp.evaluate("document.querySelector('[data-pr-id=\"pr-browser\"]') !== null"))
        cdp.evaluate("document.querySelector('[data-pr-id=\"pr-browser\"]').click()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#pullRequestDetail')?.textContent.includes('Review every quarantined change')"))
        screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        (ROOT / "assets" / "preview-secure-pull-request.png").write_bytes(base64.b64decode(screenshot["data"]))

        cdp.evaluate("document.querySelector('#addRepoBtn').click()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoModalBackdrop').classList.contains('open') && !document.querySelector('#repoPathField').classList.contains('hidden')"))
        assert cdp.evaluate("document.querySelector('#repoUploadFilesChoice') !== null && document.querySelector('#repoUploadFolderChoice') !== null && document.querySelector('#repoPathChoice') !== null")
        cdp.evaluate("""(() => { const file=new File(['single import'],'hello.txt',{type:'text/plain'}); const input=document.querySelector('#newRepoFilesInput'); Object.defineProperty(input,'files',{configurable:true,value:[file]}); input.dispatchEvent(new Event('change',{bubbles:true})); document.querySelector('#repoNameInput').value='Single File Import'; document.querySelector('#repoForm').requestSubmit(); })()""")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoTitle')?.textContent === 'Single File Import' && [...document.querySelectorAll('[data-file-path]')].some(x => x.dataset.filePath === 'hello.txt')"))

        cdp.evaluate("document.querySelector('#addRepoBtn').click()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoModalBackdrop').classList.contains('open')"))
        cdp.evaluate("""(() => { const file=new File(['folder import'],'main.js',{type:'text/javascript'}); Object.defineProperty(file,'webkitRelativePath',{configurable:true,value:'FolderProject/src/main.js'}); const input=document.querySelector('#newRepoFolderInput'); Object.defineProperty(input,'files',{configurable:true,value:[file]}); input.dispatchEvent(new Event('change',{bubbles:true})); })()""")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoUploadFolderChoice').classList.contains('selected') && document.querySelector('#repoImportSummary').textContent.includes('src/main.js') && document.querySelector('#repoNameInput').value === 'FolderProject'"))
        screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        (ROOT / "assets" / "preview-repository-onboarding.png").write_bytes(base64.b64decode(screenshot["data"]))
        cdp.evaluate("document.querySelector('#repoForm').requestSubmit()")
        wait_for(lambda: cdp.evaluate("document.querySelector('#repoTitle')?.textContent === 'FolderProject' && [...document.querySelectorAll('[data-file-path]')].some(x => x.dataset.filePath === 'src/main.js') && document.querySelector('[data-file-path=\"src\"]')?.getAttribute('aria-expanded') === 'true'"))
        cdp.evaluate("document.querySelector('[data-file-path=\"src\"]').click()")
        wait_for(lambda: cdp.evaluate("![...document.querySelectorAll('[data-file-path]')].some(x => x.dataset.filePath === 'src/main.js')"))
        cdp.evaluate("document.querySelector('[data-file-path=\"src\"]').click()")
        wait_for(lambda: cdp.evaluate("[...document.querySelectorAll('[data-file-path]')].some(x => x.dataset.filePath === 'src/main.js') && document.querySelector('[data-file-path=\"src\"]').getAttribute('aria-expanded') === 'true'"))

        cdp.evaluate("document.querySelector('#addRepoBtn').click(); document.querySelector('#repoForkChoice').click()")
        wait_for(lambda: cdp.evaluate("!document.querySelector('#repoShareLinkField').classList.contains('hidden') && document.querySelector('#repoShareLinkInput').required"))
        cdp.evaluate("document.querySelector('#repoShareLinkInput').value='http://192.168.1.50:8766/contribute.html#example-secure-invite-token-1234567890'; document.querySelector('#repoNameInput').value='Shared Team Project Fork';")
        screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        (ROOT / "assets" / "preview-team-onboarding.png").write_bytes(base64.b64decode(screenshot["data"]))
        cdp.evaluate("document.querySelector('#cancelRepoModalBtn').click()")

        cdp.evaluate("document.querySelector('#addRepoBtn').click(); document.querySelector('#repoPathChoice').click()")
        wait_for(lambda: cdp.evaluate("!document.querySelector('#repoPathField').classList.contains('hidden') && document.querySelector('#repoPathInput').required"))
        cdp.evaluate("document.querySelector('#cancelRepoModalBtn').click()")

        errors = []
        for event in cdp.events:
            if event.get("method") == "Runtime.exceptionThrown":
                errors.append(event.get("params", {}))
            if event.get("method") == "Log.entryAdded" and event.get("params", {}).get("entry", {}).get("level") == "error":
                errors.append(event.get("params", {}))
        assert not errors, errors
        print("ForgeTrace Chromium UI smoke test: PASS")
    finally:
        if cdp:
            cdp.close()
        if chrome:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    main()
