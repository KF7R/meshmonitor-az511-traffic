import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
import install_traffic as installer

class InstallerTests(unittest.TestCase):
    def test_prepare_only_cli(self):
        with tempfile.TemporaryDirectory() as d:
            output=Path(d)/'setup'
            answer='d12d8309-13e9-46d2-95ca-36b173b8c520\n2\ninherit\n'+str(output)+'\n'
            result=subprocess.run([sys.executable,installer.__file__,'--prepare-only'],input=answer,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(len(list(output.glob('*.json'))),6)
            self.assertFalse((output/'compose.traffic.json').exists())
            self.assertEqual(output.stat().st_mode & 0o777,0o700)

    def test_configure_all_examples(self):
        root=Path(installer.__file__).parent
        for p in (root/'examples').glob('*.json'):
            original=json.loads(p.read_text())
            result=installer.configure(original,'test-source',2,None)
            self.assertFalse(result['enabled'])
            self.assertNotIn('test-source',json.dumps(original))
            self.assertNotIn('REPLACE_WITH_SOURCE_UUID',json.dumps(result))
            for n in result['config']['nodes']:
                if n['type'] in ('action.sendMessage','action.broadcastWaypoint'):
                    self.assertNotIn('hopLimit',n['params'])
                    if 'channel' in n['params']: self.assertEqual(n['params']['channel'],2)
            forced=installer.configure(original,'test-source',2,4)
            for n in forced['config']['nodes']:
                if n['type'] in ('action.sendMessage','action.broadcastWaypoint'):
                    self.assertEqual(n['params']['hopLimit'],4)

    def test_private_file_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config'
            installer.private_write(p,'placeholder')
            self.assertEqual(p.stat().st_mode & 0o777,0o600)
            link=Path(d)/'link'; link.symlink_to(p)
            with self.assertRaises(ValueError): installer.private_write(link,'overwrite')
            self.assertEqual(p.read_text(),'placeholder')

    def test_preserve_existing_automation_and_variables(self):
        docs=[{'name':'Traffic','enabled':False,'config':{}}]
        variables=[{'name':n,'type':'json','scope':'global','readonly':False} for n in ('traffic','traffic_scheduled')]
        with patch.object(installer,'api',side_effect=[[{'name':'Traffic'}],variables]) as api:
            installer.setup_api('http://localhost:8080','fake',docs)
            self.assertEqual(api.call_count,2)

    def test_create_disabled_only_and_transport_validation(self):
        docs=[{'name':'Traffic','enabled':False,'config':{}}]
        with patch.object(installer,'api',return_value=[]) as api:
            installer.setup_api('http://localhost:8080','fake',docs)
            posts=[c for c in api.call_args_list if c.args[2]=='POST']
            self.assertEqual(len(posts),3)
            self.assertFalse(posts[-1].args[4]['enabled'])
        with self.assertRaises(ValueError): installer.setup_api('http://remote-host','fake',docs)

if __name__=='__main__': unittest.main()
