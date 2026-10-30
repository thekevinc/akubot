from pathlib import Path
import copy,json,tempfile,unittest
from akubot.model_catalog import ModelCatalog,ModelSelectionError


class StrengthGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.catalog=ModelCatalog(Path(self.temp.name)/'model-catalog.json')
        self.base={'id':'llama','label':'Synthetic Llama','model':'base:latest'}
        for name in ('one','one-half','two'):
            self.catalog.register(self.base,{'id':name,'label':name,'model':name+':latest','base_id':'llama'})
        self.group={'id':'full-archive','label':'Full archive','variants':[
            {'adapter_id':'one','strength':1.0},{'adapter_id':'one-half','strength':1.5},{'adapter_id':'two','strength':2.0}]}
    def tearDown(self):self.temp.cleanup()
    def test_legacy_catalog_and_valid_group_resolve_real_models(self):
        self.assertEqual(self.catalog.options(['base:latest'],'base:latest')['strength_groups'],[])
        self.catalog.register_strength_group(self.group)
        self.catalog.register_strength_group(self.group)
        options=self.catalog.options(['base:latest','one:latest','one-half:latest','two:latest'],'one:latest')
        self.assertEqual(len(options['strength_groups']),1)
        self.assertEqual(options['strength_groups'][0]['base_id'],'llama')
        self.assertEqual([x['strength'] for x in options['strength_groups'][0]['variants']],[1,1.5,2])
        self.assertEqual(self.catalog.resolve({'base_id':'llama','adapter_id':'two'},['two:latest'],'one:latest'),'two:latest')
        with self.assertRaises(ModelSelectionError):
            self.catalog.resolve({'base_id':'llama','adapter_id':'full-archive'},['two:latest'],'one:latest')
    def test_invalid_metadata_cannot_replace_a_good_registry(self):
        self.catalog.register_strength_group(self.group)
        original=self.catalog.strengths_path.read_bytes()
        for value in (True,float('nan'),float('inf'),0,2.1):
            bad=copy.deepcopy(self.group);bad['id']='invalid-group';bad['variants'][0]['strength']=value
            with self.assertRaises(ModelSelectionError):self.catalog.register_strength_group(bad)
            self.assertEqual(self.catalog.strengths_path.read_bytes(),original)
        bad=copy.deepcopy(self.group);bad['variants'][1]['strength']=1
        with self.assertRaises(ModelSelectionError):self.catalog.strength_groups({'version':1,'groups':[bad]})
        bad=copy.deepcopy(self.group);bad['variants'][1]['adapter_id']='missing'
        with self.assertRaises(ModelSelectionError):self.catalog.strength_groups({'version':1,'groups':[bad]})
    def test_mixed_base_groups_duplicate_members_and_id_collisions_rejected(self):
        self.catalog.register({'id':'smol','label':'Other base','model':'smol:latest'},
                              {'id':'other','label':'Other style','model':'other:latest','base_id':'smol'})
        bad=copy.deepcopy(self.group);bad['variants'][1]['adapter_id']='other'
        with self.assertRaises(ModelSelectionError):self.catalog.register_strength_group(bad)
        bad=copy.deepcopy(self.group);bad['id']='one'
        with self.assertRaises(ModelSelectionError):self.catalog.register_strength_group(bad)
        duplicate=copy.deepcopy(self.group);duplicate['id']='another-group'
        with self.assertRaises(ModelSelectionError):self.catalog.strength_groups({'version':1,'groups':[self.group,duplicate]})
    def test_uninstalled_variants_remain_unavailable_to_selection(self):
        self.catalog.register_strength_group(self.group)
        options=self.catalog.options(['base:latest','one:latest'],'one:latest')
        self.assertFalse(next(x for x in options['adapters'] if x['id']=='two')['available'])
        with self.assertRaises(ModelSelectionError):self.catalog.resolve({'base_id':'llama','adapter_id':'two'},['one:latest'],'one:latest')
