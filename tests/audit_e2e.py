import sys, tempfile, pathlib, json, os, unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from clients import decision_log as d
class Audit(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  d.BASE_DIR=self.tmp.name+'/logs'; d.MAX_BYTES=20*1024*1024
 def rows(self):
  return [json.loads(x) for x in pathlib.Path(d._current_path()).read_text().splitlines()]
 def test_event_mixed_token(self):
  token='Abcdef123456789'
  self.assertTrue(d.event(token)); self.assertNotIn(token,pathlib.Path(d._current_path()).read_text())
 def test_error_cannot_leak(self):
  with patch.object(d,'_ensure_dir',side_effect=RuntimeError('Bearer FAKE_SECRET_123')):
   self.assertFalse(d.event('test'))
  self.assertNotIn('FAKE_SECRET_123',d.LAST_ERROR)
 def test_symlink_target_untouched(self):
  d._ensure_dir(); target=pathlib.Path(self.tmp.name)/'victim'; target.write_text('KEEP'); target.chmod(0o644)
  os.symlink(target,d._current_path())
  self.assertFalse(d.event('test')); self.assertEqual(target.read_text(),'KEEP'); self.assertEqual(target.stat().st_mode&0o777,0o644)
 def test_hardlink_target_untouched(self):
  d._ensure_dir(); target=pathlib.Path(self.tmp.name)/'victim'; target.write_text('KEEP'); target.chmod(0o644)
  os.link(target,d._current_path())
  self.assertFalse(d.event('test')); self.assertEqual(target.read_text(),'KEEP'); self.assertEqual(target.stat().st_mode&0o777,0o644)
 def test_symlink_directory(self):
  target=pathlib.Path(self.tmp.name)/'target'; target.mkdir(); target.chmod(0o755)
  os.symlink(target,d.BASE_DIR)
  self.assertFalse(d.event('test')); self.assertEqual(list(target.iterdir()),[])
  self.assertEqual(target.stat().st_mode&0o777,0o755)
 def test_rotation_symlink_untouched(self):
  self.assertTrue(d.event('seed')); d.MAX_BYTES=1
  target=pathlib.Path(self.tmp.name)/'victim'; target.write_text('KEEP')
  os.symlink(target,d._current_path()+'.1')
  self.assertFalse(d.event('test')); self.assertEqual(target.read_text(),'KEEP')
  self.assertTrue(os.path.islink(d._current_path()+'.1'))
 def test_nonfinite_not_json(self):
  self.assertFalse(d.event('test',value=float('nan')))
 def test_collision_and_permissions_and_recovery(self):
  self.assertTrue(d.event('test',payload={'sk-FAKE123456':1,'sk-FAKE654321':2},apiSecret='x'))
  row=self.rows()[0]; self.assertEqual(len(row['payload']),2); self.assertNotIn('x',row['apiSecret'])
  self.assertEqual(os.stat(d._current_path()).st_mode&0o777,0o600)
  d.MAX_BYTES=1; self.assertTrue(d.event('rot')); self.assertEqual(os.stat(d._current_path()+'.1').st_mode&0o777,0o600)
  with patch.object(d,'_ensure_dir',side_effect=PermissionError('test')): self.assertFalse(d.event('bad'))
  self.assertTrue(d.event('good')); self.assertEqual(d.LAST_ERROR,'')
if __name__=='__main__': unittest.main(verbosity=2)
