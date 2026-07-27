from __future__ import annotations
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from core import code_repair
spec=importlib.util.spec_from_file_location('repair_runner',ROOT/'scripts'/'repair_runner.py')
mod=importlib.util.module_from_spec(spec); assert spec and spec.loader; sys.modules[spec.name]=mod; spec.loader.exec_module(mod)

class RepairRunnerTest(unittest.TestCase):
  def setUp(self):
    self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
    self.source_root=self.root/'code-workspaces'; self.repo=self.source_root/'demo'; self.repo.mkdir(parents=True)
    subprocess.run(['git','init','-q'],cwd=self.repo,check=True)
    subprocess.run(['git','config','user.email','test@example.com'],cwd=self.repo,check=True)
    subprocess.run(['git','config','user.name','Test'],cwd=self.repo,check=True)
    (self.repo/'calc.py').write_text('def answer():\n    return 41\n',encoding='utf-8')
    (self.repo/'test_calc.py').write_text('import unittest\nfrom calc import answer\nclass T(unittest.TestCase):\n    def test_answer(self): self.assertEqual(answer(), 42)\n',encoding='utf-8')
    subprocess.run(['git','add','.'],cwd=self.repo,check=True)
    subprocess.run(['git','commit','-qm','init'],cwd=self.repo,check=True)
    self.config=self.root/'repositories.yaml'
    self.config.write_text('''schema_version: 1
repositories:
  demo:
    source_dir: demo
    description: demo
    default_test_profile: python
    allowed_test_profiles: [python]
    protected_paths: [policy/]
    max_patch_files: 5
    max_patch_bytes: 50000
test_profiles:
  python:
    commands:
      - [python, -m, unittest, discover, -s, ., -p, test_*.py, -v]
    timeout_seconds: 30
''',encoding='utf-8')
    self.runner=mod.RepairRunner(root=self.root,config_file=self.config,source_root=self.source_root,repair_root=self.root/'repair-space')
  def tearDown(self): self.tmp.cleanup()
  def patch(self):
    return '''diff --git a/calc.py b/calc.py
index 3dbca17..9cb39fe 100644
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def answer():
-    return 41
+    return 42
'''
  def test_success(self):
    req=code_repair.submit_repair('demo',self.patch(),'python','fix',root=self.root)
    self.assertEqual(self.runner.run_once().get('succeeded'),1)
    state=code_repair.get_repair(req['id'],root=self.root)
    self.assertEqual(state['status'],'succeeded',state)
    self.assertFalse(state['result']['source_repository_modified'])
    self.assertEqual((self.repo/'calc.py').read_text(), 'def answer():\n    return 41\n')
  def test_tamper_blocked(self):
    req=code_repair.submit_repair('demo',self.patch(),'python','fix',root=self.root)
    p=self.root/'data'/'repair-requests'/f"{req['id']}.json"
    data=json.loads(p.read_text()); data['patch']=data['patch'].replace('42','43'); p.write_text(json.dumps(data))
    self.assertEqual(self.runner.run_once().get('blocked'),1)
    self.assertEqual(code_repair.get_repair(req['id'],root=self.root)['status'],'blocked')
  def test_protected_path_blocked(self):
    patch='''diff --git a/policy/x.txt b/policy/x.txt
new file mode 100644
index 0000000..257cc56
--- /dev/null
+++ b/policy/x.txt
@@ -0,0 +1 @@
+x
'''
    req=code_repair.submit_repair('demo',patch,'python','bad',root=self.root)
    self.assertEqual(self.runner.run_once().get('blocked'),1)

if __name__=='__main__': unittest.main(verbosity=2)
