from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from core import code_repair

PATCH='''diff --git a/a.py b/a.py
index 2c985b1..17ec2d2 100644
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-x = 1
+x = 2
'''

class CodeRepairQueueTest(unittest.TestCase):
  def test_submit_is_fingerprint_bound_and_public_view_hides_patch(self):
    with tempfile.TemporaryDirectory() as tmp:
      root=Path(tmp)
      request=code_repair.submit_repair('demo',PATCH,'compile','fix',expected_base='abcdef1',root=root)
      self.assertEqual(request['capability'],'code.repair')
      state=code_repair.get_repair(request['id'],root=root)
      self.assertEqual(state['status'],'queued')
      self.assertNotIn('patch',state['request'])
      self.assertEqual(len(request['fingerprint']),64)
  def test_secret_like_patch_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      bad=PATCH.replace('x = 2','api_key = sk-abcdefghijk')
      with self.assertRaises(ValueError):
        code_repair.submit_repair('demo',bad,'compile','bad',root=Path(tmp))
  def test_catalog_hides_source_and_commands(self):
    config={'repositories':{'demo':{'description':'d','source_dir':'private/path','default_test_profile':'compile','allowed_test_profiles':['compile'],'language':'python'}},'test_profiles':{'compile':{'commands':[['python','-m','compileall','.']]}}}
    result=code_repair.safe_catalog(config)
    text=str(result)
    self.assertIn('demo',text)
    self.assertNotIn('private/path',text)
    self.assertNotIn('compileall',text)

if __name__=='__main__': unittest.main(verbosity=2)
