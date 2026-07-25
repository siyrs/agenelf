from __future__ import annotations
import tempfile, unittest, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from skills import code_writer

class CodeWriterSafetyTest(unittest.TestCase):
  def setUp(self): self.tmp=tempfile.TemporaryDirectory(); code_writer.set_project_root(self.tmp.name)
  def tearDown(self): code_writer.set_project_root(None); self.tmp.cleanup()
  def test_writes_only_scratch_text(self):
    result=code_writer.execute('write_code_file',{'path':'drafts/fix.py','content':'x = 1\n'})
    path=Path(result); self.assertTrue(path.is_file()); self.assertTrue(path.is_relative_to(Path(self.tmp.name)))
  def test_path_escape_and_binary_rejected(self):
    self.assertIn('逃逸',code_writer.execute('write_code_file',{'path':'../evil.py','content':'x'}))
    self.assertIn('扩展名',code_writer.execute('write_code_file',{'path':'evil.bin','content':'x'}))
  def test_python_execution_is_permanently_disabled(self):
    marker=Path(self.tmp.name)/'owned'
    result=code_writer.execute('run_python',{'code':f"open({str(marker)!r},'w').write('x')"})
    self.assertIn('已永久禁用',result); self.assertFalse(marker.exists())

if __name__=='__main__': unittest.main(verbosity=2)
