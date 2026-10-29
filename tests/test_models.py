import json,tempfile,unittest
from pathlib import Path
from akubot.model_catalog import ModelCatalog,ModelSelectionError

class CatalogTests(unittest.TestCase):
 def test_only_registered_compatible_pairs_can_resolve(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/'catalog.json';catalog=ModelCatalog(path)
   base={'id':'hf:owner/base@revision','label':'Small base','model':'small:latest'}
   adapter={'id':'style-v1','base_id':base['id'],'label':'Style 1','model':'trained:latest'}
   catalog.register(base,adapter)
   catalog.register(base,adapter)
   installed=['small:latest','trained:latest','llama3.1:8b']
   options=catalog.options(installed,'trained:latest')
   self.assertEqual(len(options['bases']),2)
   self.assertEqual(options['selection'],{'base_id':base['id'],'adapter_id':adapter['id']})
   self.assertEqual(catalog.resolve(options['selection'],installed,'small:latest'),'trained:latest')
   for selection in [None,{}, {'base_id':[], 'adapter_id':None},
                     {'base_id':'ollama:llama3.1:8b','adapter_id':adapter['id']},
                     {'base_id':base['id'],'adapter_id':'missing'}]:
    with self.assertRaises(ModelSelectionError):catalog.resolve(selection,installed,'small:latest')
   with self.assertRaises(ModelSelectionError):catalog.resolve(options['selection'],['small:latest'],'small:latest')
   self.assertEqual(path.stat().st_mode&0o777,0o600)

 def test_corrupt_registry_fails_closed_and_offline_current_remains_editable(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/'catalog.json';catalog=ModelCatalog(path)
   selection={'base_id':'ollama:llama3.1:8b','adapter_id':None}
   self.assertEqual(catalog.resolve(selection,[],'llama3.1:8b'),'llama3.1:8b')
   for payload in [{'version':1,'bases':[],'adapters':[{'id':'orphan'}]}, {'version':99,'bases':[],'adapters':[]}]:
    path.write_text(json.dumps(payload))
    with self.assertRaises(ModelSelectionError):catalog.options(['llama3.1:8b'],'llama3.1:8b')

if __name__=='__main__':unittest.main()
