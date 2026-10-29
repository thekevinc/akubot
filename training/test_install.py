import importlib.util,tempfile,unittest
from pathlib import Path
s=importlib.util.spec_from_file_location('install',Path(__file__).with_name('install_model.py'))
install=importlib.util.module_from_spec(s);s.loader.exec_module(install)

class InstallationTests(unittest.TestCase):
 def test_legacy_and_v2_manifests_are_both_detected(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)
   for folder,registry,name,tag in [('manifests','registry.ollama.ai','llama3.1','8b'),('manifests-v2','ollama.com','aku-style-base','latest')]:
    path=root/'models'/folder/registry/'library'/name/tag
    path.parent.mkdir(parents=True);path.write_text('{}')
    self.assertTrue(install.is_installed(root,name+':'+tag))
   self.assertTrue(install.is_installed(root,'aku-style-base'))
   self.assertFalse(install.is_installed(root,'missing'))
 def test_model_templates_keep_distinct_turn_boundaries(self):
  self.assertIn('<|im_start|>',install.TEMPLATE)
  self.assertIn('<|start_header_id|>',install.LLAMA_TEMPLATE)
  self.assertIn('<|eot_id|>',install.LLAMA_TEMPLATE)

if __name__=='__main__':unittest.main()
