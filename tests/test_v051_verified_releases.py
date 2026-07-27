from __future__ import annotations
import base64, hashlib, http.client, json, shutil, tempfile, threading, unittest, zipfile
from pathlib import Path
from forgetrace.app import build_application
from forgetrace.errors import ForgeTraceError
from forgetrace.web import create_server
ROOT=Path(__file__).resolve().parents[1]

class VerifiedReleaseTest(unittest.TestCase):
 def setUp(self):
  self.root=Path(tempfile.mkdtemp(prefix='forgetrace-v051-'))
  self.app=build_application(ROOT,self.root/'data')
  record=self.app.registry.register_repository(path=str(self.root/'repo'),name='Release Repo',author='Owner',initialize=True,create_directory=True)
  self.rid=record['id']; self.repo=Path(record['path'])
 def tearDown(self): shutil.rmtree(self.root,ignore_errors=True)
 def _release(self,access=False):
  return self.app.releases.create(self.rid,name='ForgeTrace 1.0',version='v1.0.0',notes='# Notes\n<script>alert(1)</script>',tag_ref='v1.0.0',commit_ref='abcdef1',contributor_access=access)
 def test_draft_asset_publish_and_immutability(self):
  rel=self._release(); before=hashlib.sha256((self.repo/'README.md').read_bytes()).hexdigest()
  rel=self.app.releases.add_asset_base64(self.rid,rel['id'],filename='build.bin',content_base64=base64.b64encode(b'build').decode())
  asset=rel['assets'][0]; self.assertEqual(hashlib.sha256(b'build').hexdigest(),asset['sha256'])
  rel=self.app.releases.publish(self.rid,rel['id'],expected_version=rel['recordVersion']); self.assertEqual('published',rel['state'])
  with self.assertRaises(ForgeTraceError) as ctx:self.app.releases.update(self.rid,rel['id'],expected_version=rel['recordVersion'],name='changed')
  self.assertEqual('release_published_immutable',ctx.exception.code)
  self.assertNotIn('<script>',rel['notesHtml']); self.assertEqual(before,hashlib.sha256((self.repo/'README.md').read_bytes()).hexdigest())
 def test_tamper_blocks_download_export_and_health_reports(self):
  rel=self._release(); rel=self.app.releases.add_asset_base64(self.rid,rel['id'],filename='a.txt',content_base64=base64.b64encode(b'good').decode(),content_type='text/plain'); rel=self.app.releases.publish(self.rid,rel['id'],expected_version=rel['recordVersion'])
  with self.app.releases._connect() as db: row=db.execute('SELECT * FROM assets WHERE release_id=?',(rel['id'],)).fetchone()
  (self.app.releases.assets_root/row['storage_name']).write_bytes(b'evil')
  with self.assertRaises(ForgeTraceError): self.app.releases.asset_path(self.rid,rel['id'],row['id'])
  self.assertEqual('critical',self.app.releases.health_status(self.rid)['status'])
 def test_contributor_requires_permission_and_release_opt_in(self):
  rel=self._release(access=True); rel=self.app.releases.add_asset_base64(self.rid,rel['id'],filename='x.bin',content_base64=base64.b64encode(b'x').decode()); rel=self.app.releases.publish(self.rid,rel['id'],expected_version=rel['recordVersion'])
  denied=self.app.collaboration.create_invite(self.rid,allow_project_participation=False)
  with self.assertRaises(ForgeTraceError): self.app.releases.list_for_token(denied['token'])
  allowed=self.app.collaboration.create_invite(self.rid,allow_project_participation=True)
  listing=self.app.releases.list_for_token(allowed['token']); self.assertEqual(rel['id'],listing['items'][0]['id'])
  path,name,_=self.app.releases.asset_path(self.rid,rel['id'],rel['assets'][0]['id'],token=allowed['token']); self.assertEqual(b'x',path.read_bytes()); self.assertEqual('x.bin',name)
 def test_export_contains_verified_manifest_and_assets(self):
  rel=self._release(); rel=self.app.releases.add_asset_base64(self.rid,rel['id'],filename='artifact.txt',content_base64=base64.b64encode(b'artifact').decode()); rel=self.app.releases.publish(self.rid,rel['id'],expected_version=rel['recordVersion'])
  path,_=self.app.releases.export_release(self.rid,rel['id'])
  with zipfile.ZipFile(path) as z:
   self.assertEqual(b'artifact',z.read('assets/artifact.txt')); manifest=json.loads(z.read('release-manifest.json')); self.assertFalse(manifest['remotePublicationVerified'])
 def test_restart_persistence_read_only_and_registry_recovery_independence(self):
  self.app.registry.set_access_mode(self.rid,'read_only'); rel=self._release(); rel=self.app.releases.add_asset_base64(self.rid,rel['id'],filename='r.bin',content_base64=base64.b64encode(b'r').decode())
  restarted=build_application(ROOT,self.root/'data'); self.assertEqual(rel['id'],restarted.releases.list(self.rid)['items'][0]['id']); self.assertEqual('read_only',restarted.registry.repository_service(self.rid).access_policy()['effectiveMode'])
 def test_database_immutability_and_draft_retention(self):
  rel=self._release(); rel=self.app.releases.add_asset_base64(self.rid,rel['id'],filename='immutable.bin',content_base64=base64.b64encode(b'i').decode()); rel=self.app.releases.publish(self.rid,rel['id'],expected_version=rel['recordVersion'])
  with self.app.releases._connect() as db:
   with self.assertRaises(Exception): db.execute("DELETE FROM releases WHERE id=?",(rel['id'],))
  draft=self.app.releases.create(self.rid,name='Old draft',version='v-old')
  with self.app.releases.lock,self.app.releases._connect() as db:
   db.execute("UPDATE releases SET updated_at='2000-01-01T00:00:00Z' WHERE id=?",(draft['id'],));db.commit()
  result=self.app.releases.cleanup_retention(days=180);self.assertEqual(1,result['removedDrafts']);self.assertEqual(rel['id'],self.app.releases.list(self.rid)['items'][0]['id'])
 def test_http_owner_and_contributor_routes(self):
  server=create_server(self.app,'127.0.0.1',0); thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start(); port=server.server_address[1]
  try:
   c=http.client.HTTPConnection('127.0.0.1',port,timeout=10); h={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}'}
   c.request('POST',f'/api/v1/repositories/{self.rid}/releases',body=json.dumps({'name':'HTTP','version':'v2','contributorAccess':True}),headers=h);r=c.getresponse(); rel=json.loads(r.read());self.assertEqual(201,r.status)
   c.request('POST',f'/api/v1/repositories/{self.rid}/releases/{rel["id"]}/assets',body=json.dumps({'filename':'http.txt','contentBase64':base64.b64encode(b'http').decode(),'contentType':'text/plain'}),headers=h);r=c.getresponse();rel=json.loads(r.read());self.assertEqual(201,r.status)
   c.request('POST',f'/api/v1/repositories/{self.rid}/releases/{rel["id"]}/publish',body=json.dumps({'expectedVersion':rel['recordVersion']}),headers=h);r=c.getresponse();rel=json.loads(r.read());self.assertEqual(200,r.status)
   invite=self.app.collaboration.create_invite(self.rid,allow_project_participation=True)
   c.request('GET','/api/v1/collaboration/releases',headers={'X-ForgeTrace-Invite':invite['token']});r=c.getresponse();data=json.loads(r.read());self.assertEqual(rel['id'],data['items'][0]['id'])
   c.close()
  finally: server.shutdown();server.server_close();thread.join(timeout=5)

if __name__=='__main__': unittest.main()
