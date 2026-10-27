"""No live credentials, parent .env reads, or network access are needed."""
import importlib.util
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from check_secrets import scan_blob

ROOT=Path(__file__).resolve().parents[1]

class ArchiveSafetyTests(unittest.TestCase):
    def test_provider_key_is_rejected_without_echo(self):
        fake=('sk-'+'ant-'+'A'*50).encode()
        self.assertIn('provider-key',scan_blob('config.txt',fake))
        self.assertNotIn(fake.decode(),repr(scan_blob('config.txt',fake)))

    def test_forbidden_paths_and_placeholder(self):
        self.assertIn('forbidden-config-or-database',scan_blob('.env',b''))
        self.assertIn('runtime-data',scan_blob('ver_1/run_full/work/1.json',b'{}'))
        self.assertIn('unapproved-data-or-evidence',scan_blob('examples/full.xlsx',b''))
        self.assertEqual(scan_blob('.env.example',b'ANTHROPIC_API_KEY=your_API_key\n'),[])
        self.assertIn('example-key-must-be-placeholder',scan_blob('.env.example',b'ANTHROPIC_API_KEY=unexpected\n'))

    def test_sample_is_limited_and_value_only(self):
        self.assertEqual(scan_blob('examples/registry/registry_sample.xlsx',(ROOT/'examples/registry/registry_sample.xlsx').read_bytes()),[])

    def test_contact_details_and_local_paths_are_rejected(self):
        email=('person'+'@'+'private-company.com').encode()
        phone=('010'+'-'+'9876'+'-'+'5432').encode()
        home=('C:'+'/'+'Users'+'/'+'private_user'+'/notes').encode()
        self.assertIn('non-example-email',scan_blob('notes.md',email))
        self.assertIn('unapproved-phone-number',scan_blob('notes.md',phone))
        self.assertIn('personal-home-path',scan_blob('notes.md',home))
        self.assertEqual(scan_blob('notes.md',b'news@example.invalid'),[])

    def test_synthetic_phones_are_allowed_only_in_fixture_file(self):
        from check_secrets import FIXTURE_PATH
        value=('033'+'-'+'123'+'-'+'4567').encode()
        self.assertEqual(scan_blob(FIXTURE_PATH,value),[])
        self.assertIn('unapproved-phone-number',scan_blob('notes.md',value))

    def test_homepage_urls_allow_public_hosts_without_credentials(self):
        from check_secrets import homepage_url_allowed
        self.assertTrue(homepage_url_allowed('www.example.org'))
        self.assertTrue(homepage_url_allowed('https://example.org/news'))
        for value in ['https://user:password@example.org','https://example.org?token=example','mailto:person@example.invalid','http://127.0.0.1','http://localhost']:
            self.assertFalse(homepage_url_allowed(value))

    def test_staged_key_is_blocked_even_if_working_file_is_clean(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'ver_1') as tmp:
            def git(*args):
                subprocess.run(['git',*args],cwd=tmp,check=True,capture_output=True)
            git('init','-q')
            fake='sk-'+'ant-'+'B'*50
            p=Path(tmp)/'config.txt'
            p.write_text(fake,encoding='utf-8')
            git('add','config.txt')
            p.write_text('clean working copy',encoding='utf-8')
            result=subprocess.run([sys.executable,str(ROOT/'tools/check_secrets.py'),'--staged'],cwd=tmp,capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            self.assertIn('provider-key',result.stderr)
            self.assertNotIn(fake,result.stdout+result.stderr)

    def test_config_loader_is_bounded_and_rejects_placeholder(self):
        # Patch the environment before importing; the module must not consult this task's parent config.
        path=ROOT/'ver_1/skill/scripts/llm_extract.py'
        with patch.dict(os.environ,{},clear=True), patch.object(Path,'is_file',return_value=False):
            spec=importlib.util.spec_from_file_location('archive_llm_safety',path)
            module=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory(dir=ROOT/'ver_1') as tmp:
                repository=Path(tmp)/'repo'
                script=repository/'ver_1/skill/scripts/llm_extract.py'
                script.parent.mkdir(parents=True)
                (repository.parent/'.env').write_text('ANTHROPIC_API_KEY=parent_sentinel\n',encoding='utf-8')
                with patch.object(module,'__file__',str(script)):
                    module._load_dotenv()
                    self.assertNotIn('ANTHROPIC_API_KEY',os.environ)
                    (repository/'.env').write_text('ANTHROPIC_API_KEY=your_API_key\n',encoding='utf-8')
                    # is_file was disabled for import; allow it for this synthetic repository only.
                    with patch.object(Path,'is_file',lambda p: p.exists() and not p.is_dir()):
                        with self.assertRaises(SystemExit) as caught:module.load_env_key()
                    self.assertNotIn('parent_sentinel',str(caught.exception))
                    self.assertNotIn('your_API_key',str(caught.exception))

if __name__=='__main__':unittest.main()
