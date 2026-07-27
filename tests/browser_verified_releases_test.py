from __future__ import annotations
import base64, hashlib, json, shutil, tempfile, threading
from pathlib import Path
from playwright.sync_api import sync_playwright
from forgetrace.app import build_application
from forgetrace.web import ForgeTraceHTTPServer, create_server, make_handler
ROOT=Path(__file__).resolve().parents[1]
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
 chromium=shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome')
 if not chromium: raise SystemExit('Chromium required')
 temp=Path(tempfile.mkdtemp(prefix='forgetrace-v051-release-browser-')); app=build_application(ROOT,temp/'data')
 rec=app.registry.register_repository(path=str(temp/'repo'),name='Release Browser',author='Rooke Poole',initialize=True,create_directory=True);rid=rec['id'];repo=Path(rec['path'])
 before_readme=sha(repo/'README.md');before_state=sha(repo/'.forgetrace/state.json')
 invite=app.collaboration.create_invite(rid,allow_project_participation=True,max_uses=2)
 owner_handler=make_handler(app);owner_handler.enforce_owner_request_origin=lambda self,path:self.require_local_owner()
 owner_server=ForgeTraceHTTPServer(('127.0.0.1',0),owner_handler);owner_server.forgetrace_surface='owner'; gateway=create_server(app,'127.0.0.1',0,surface='gateway')
 ot=threading.Thread(target=owner_server.serve_forever,daemon=True);gt=threading.Thread(target=gateway.serve_forever,daemon=True);ot.start();gt.start()
 owner_base=f'http://127.0.0.1:{owner_server.server_address[1]}';gateway_base=f'http://127.0.0.1:{gateway.server_address[1]}'
 try:
  with sync_playwright() as pw:
   browser=pw.chromium.launch(headless=True,executable_path=chromium,args=['--no-sandbox','--disable-dev-shm-usage','--disable-web-security'])
   context=browser.new_context(viewport={'width':1600,'height':1200});owner=context.new_page();contrib=context.new_page();errors=[]
   owner.on('pageerror',lambda e:errors.append(f'owner:{e}'));contrib.on('pageerror',lambda e:errors.append(f'contrib:{e}'))
   prompts=iter(['v1.0.0','ForgeTrace 1.0','# Release notes\n<script>alert(1)</script>'])
   owner.on('dialog',lambda d:d.accept(next(prompts,'')) if d.type=='prompt' else d.accept())
   html=(ROOT/'index.html').read_text();bridge=("<script>(()=>{const f=window.fetch.bind(window);"+f"window.fetch=(i,n)=>f(new URL(typeof i==='string'?i:i.url,'{owner_base}/').href,n);"+"})();</script>")
   html=html.replace('<head>',f'<head><base href="{owner_base}/">',1).replace('<script>',bridge+'<script>',1);owner.set_content(html,wait_until='domcontentloaded');owner.wait_for_function("document.querySelector('#repoTitle')?.textContent.includes('Release Browser')")
   owner.click('[data-tab="releases"]');owner.click('#releaseNewBtn');owner.wait_for_function('appState.releases.items.length===1');release_id=owner.evaluate('appState.releases.items[0].id')
   # Use actual HTTP asset route from page context to avoid native file chooser dependency.
   payload=base64.b64encode(b'verified artifact').decode()
   owner.evaluate("""async ({rid,releaseId,payload})=>{appState.releases.selected=await api(`/api/v1/repositories/${rid}/releases/${releaseId}/assets`,{method:'POST',body:{filename:'artifact.bin',contentType:'application/octet-stream',contentBase64:payload}});await loadReleases();await selectRelease(releaseId)}""",{'rid':rid,'releaseId':release_id,'payload':payload})
   owner.wait_for_function('appState.releases.selected?.assets?.length===1');owner.click('[data-release-publish]');owner.wait_for_function("appState.releases.selected?.state==='published'")
   assert '<script>' not in owner.locator('#releaseDetail').inner_html();assert 'SHA-256' in owner.locator('#releaseDetail').inner_text()
   chtml=(ROOT/'contribute.html').read_text().replace("let token=(location.hash||'').slice(1).trim();\n    if(token){sessionStorage.setItem('forgetrace-invite-token',token);history.replaceState(null,'',location.pathname+location.search)}\n    else token=sessionStorage.getItem('forgetrace-invite-token')||'';",f"let token={json.dumps(invite['token'])};",1)
   cbridge=("<script>(()=>{const f=window.fetch.bind(window);"+f"window.fetch=(i,n)=>f(new URL(typeof i==='string'?i:i.url,'{gateway_base}/').href,n);"+"})();</script>")
   chtml=chtml.replace('<head>',f'<head><base href="{gateway_base}/">',1).replace('<script>',cbridge+'<script>',1);contrib.set_content(chtml,wait_until='domcontentloaded');contrib.wait_for_function("document.querySelector('#repoCard')?.textContent.includes('Release Browser')")
   contrib.wait_for_function('state.releases.items.length===1');assert 'artifact.bin' in contrib.locator('#releaseList').inner_text();assert '<script>' not in contrib.locator('#releaseList').inner_html()
   assert sha(repo/'README.md')==before_readme;assert sha(repo/'.forgetrace/state.json')==before_state;assert not (repo/'.git').exists();assert not errors,errors
   actions={e['action'] for e in app.security_events.query(repository_id=rid,limit=200)['events']};assert {'release_created','release_asset_added','release_published'}.issubset(actions),actions
   browser.close()
  print('ForgeTrace Verified Releases and Artifacts browser workflow: PASS')
 finally:
  owner_server.shutdown();gateway.shutdown();ot.join(timeout=5);gt.join(timeout=5);owner_server.server_close();gateway.server_close();shutil.rmtree(temp,ignore_errors=True)
if __name__=='__main__':main()
