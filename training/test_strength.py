import importlib.util,unittest
from pathlib import Path
s=importlib.util.spec_from_file_location('strength',Path(__file__).with_name('strength.py'))
strength=importlib.util.module_from_spec(s);s.loader.exec_module(strength)

class StrengthTests(unittest.TestCase):
 def test_valid_strengths_are_relative_multipliers(self):
  for value in [0,0.5,1,1.5,2]:self.assertEqual(strength.checked_strength(value),float(value))
 def test_invalid_strengths_are_rejected(self):
  for value in [True,None,'1.5',-0.1,2.1,float('nan'),float('inf')]:
   with self.assertRaises(ValueError):strength.checked_strength(value)

if __name__=='__main__':unittest.main()
