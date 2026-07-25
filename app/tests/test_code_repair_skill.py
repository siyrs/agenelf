from __future__ import annotations
import json, os, sys, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from skills import code_repair as skill

PATCH='''diff --git a/a.py b/a.py
index 2c985b1..17ec2d2 100644
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-x = 1
+x = 2
'''

class CodeRepairSkillTest(unittest.TestCase):
  def setUp(self):
    self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); local=self.root/'local'; local.mkdir()
    self.config=local/'repositories.yaml'
    self.config.write_text('''schema_version: 1
repositories:
  demo:
    description: Demo repo
    source_dir: hidden/demo
    language: python
    default_test_profile: compile
    allowed_test_profiles: [compile]
test_profiles:
  compile:
    commands:
      - [python, -m, compileall, -q, .]
''',encoding='utf-8')
    self.old={k:os.environ.get(k) for k in ('AGENELF_ROOT','AGENELF_REPOSITORIES_FILE')}
    os.environ['AGENELF_ROOT']=str(self.root); os.environ['AGENELF_REPOSITORIES_FILE']=str(self.config)
  def tearDown(self):
    for k,v in self.old.items():
      if v is None: os.environ.pop(k,None)
      else: os.environ[k]=v
    self.tmp.cleanup()
  def test_catalog_hides_coordinates(self):
    result=json.loads(skill.execute('list_code_repair_repositories',{}))
    self.assertEqual(result['repositories'][0]['alias'],'demo')
    self.assertNotIn('hidden/demo',json.dumps(result))
    self.assertNotIn('commands',json.dumps(result))
  def test_submit_alias_only_request(self):
    result=json.loads(skill.execute('submit_code_repair_patch',{'repository':'demo','unified_diff':PATCH,'wait_seconds':0}))
    self.assertEqual(result['status'],'queued')
    request_files=list((self.root/'data'/'repair-requests').glob('repair-*.json'))
    self.assertEqual(len(request_files),1)
  def test_unknown_alias_rejected(self):
    result=skill.execute('submit_code_repair_patch',{'repository':'ghost','unified_diff':PATCH})
    self.assertIn('未知代码仓库别名',result)

if __name__=='__main__': unittest.main(verbosity=2)
