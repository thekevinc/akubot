import importlib.util,json,tempfile,unittest,zipfile
from datetime import datetime,timedelta
from pathlib import Path

spec=importlib.util.spec_from_file_location('trial_data',Path(__file__).with_name('data.py'))
data=importlib.util.module_from_spec(spec);spec.loader.exec_module(data)

class DatasetTests(unittest.TestCase):
 def test_exact_message_strings_not_json_wrappers(self):
  for text in ['https://example.com/a?b=1 😄\nnext line','{"actual_message": "json I posted"}','```python\nprint(1)\n```','<@123> @everyone']:
   self.assertEqual(data.clean_message(text),text)
  for value in [None,{},'', '  \n']:
   self.assertIsNone(data.clean_message(value))

 def test_secret_exclusion_provenance_and_split(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);archive=root/'export.zip'
   rows=[{'ID':str(i),'Timestamp':(datetime(2024,1,1)+timedelta(days=i)).isoformat(),'Contents':f'original message {i} https://example.com/{i} 😄\nnext line'} for i in range(1000)]
   with zipfile.ZipFile(archive,'w') as z:
    for cid,guild in [('123',{'id':'1','name':'Secret'}),('456',{'id':'2','name':'Other'}),('789',None)]:
     z.writestr(f'Messages/c{cid}/channel.json',json.dumps({'id':cid,'guild':guild,'name':'general'}))
     z.writestr(f'Messages/c{cid}/messages.json',json.dumps(rows))
   with self.assertRaises(ValueError):data.prepare(archive,root/'blocked',{'1'},allow_unknown=False,size=128)
   result=data.prepare(archive,root/'dataset',{'1'},allow_unknown=True,size=128)
   selected=[]
   for split in ['train','validation']:
    values=[json.loads(line) for line in (root/'dataset'/f'{split}.jsonl').read_text().splitlines()]
    self.assertTrue(values)
    for row in values:
     self.assertEqual(row['source']['guild_id'],'2')
     self.assertEqual(row['source']['channel_id'],'456')
     self.assertEqual(row['text'],rows[int(row['source']['message_id'])]['Contents'])
    selected.append({row['text'] for row in values})
   self.assertFalse(selected[0]&selected[1])
   self.assertEqual(result['counts']['excluded_secret_channels'],1)
   self.assertEqual(result['counts']['excluded_dm_channels'],1)
   full=data.prepare(archive,root/'full',{'1'},allow_unknown=True,size=128,all_messages=True)
   self.assertEqual(full['counts']['train_examples']+full['counts']['validation_examples'],1000)
   with self.assertRaises(ValueError):data.prepare(archive,root/'dataset',{'1'},allow_unknown=True,size=128)

if __name__=='__main__':unittest.main()
