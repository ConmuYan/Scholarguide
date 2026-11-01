"""The public release bundle is an allow-list, not a copy of the developer tree."""
import hashlib, importlib.util, json, subprocess, sys, tarfile, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('build_release',ROOT/'scripts'/'build_release.py')
build_release=importlib.util.module_from_spec(spec);spec.loader.exec_module(build_release)

class ReleasePackageTests(unittest.TestCase):
    def test_release_is_runnable_source_and_excludes_local_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            out=Path(temporary)/'assets';assets=build_release.build('v0.1.0','example/ScholarGuide',out)
            archive=next(path for path in assets if path.suffix=='.gz')
            with tarfile.open(archive,'r:gz') as tar:
                names=tar.getnames();root='scholarguide-v0.1.0/'
                for expected in ('app.py','requirements.txt','web/index.html','web/theme.js','docs/API.md','LICENSE','NOTICE.md','vendor/gptr_arxiv.py','vendor/LICENSE-gpt-researcher'):
                    self.assertIn(root+expected,names)
                for forbidden in ('.env','runs','tests','evaluation','archive','.pi','.codex','cloudflare-worker','cloudflare-pages'):
                    self.assertFalse(any(name==root+forbidden or name.startswith(root+forbidden+'/') for name in names),forbidden)
                self.assertFalse(any(name.lower().endswith('.pdf') for name in names))
                app=tar.extractfile(root+'app.py').read()
                self.assertIn(b'RUNS_DIR',app)
            sums=(out/'SHA256SUMS').read_text(encoding='ascii')
            for asset in assets[:3]:
                self.assertIn(hashlib.sha256(asset.read_bytes()).hexdigest(),sums)
            self.assertIn('SHA256SUMS',assets[-1].name)
            install=(out/'install.sh').read_text(encoding='utf8')
            self.assertIn('example/ScholarGuide',install)
            self.assertIn('v0.1.0',install)
            self.assertIn('SHA256SUMS',install)

            extract=Path(temporary)/'extracted';extract.mkdir()
            with tarfile.open(archive,'r:gz') as tar:tar.extractall(extract)
            app=extract/'scholarguide-v0.1.0'/'app.py'
            demo=subprocess.run([sys.executable,str(app),'demo','--scenario','satisfied','--out',str(Path(temporary)/'demo-output')],cwd=app.parent,capture_output=True,text=True,timeout=40)
            self.assertEqual(demo.returncode,0,demo.stderr)
            self.assertIn('Saved:',demo.stderr)

    def test_build_requires_valid_repo_version_and_empty_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            for index,(version,repo) in enumerate([('latest','owner/repo'),('v1.0.0','../repo'),('v1.0','owner/repo')]):
                with self.assertRaises(ValueError):build_release.build(version,repo,Path(temporary)/f'bad-{index}')
            out=Path(temporary)/'full';build_release.build('v1.0.0','owner/repo',out)
            with self.assertRaises(FileExistsError):build_release.build('v1.0.0','owner/repo',out)

if __name__=='__main__':unittest.main()
